#!/usr/bin/env python3
"""
Forschungsbuch – baut aus dem Dlf-Podcast „Forschung aktuell" ein wachsendes Wissensbuch.

Ablauf pro Lauf:
  1. RSS-Feed abrufen (URL aus config, sonst automatisch über die iTunes-Suche ermittelt)
  2. Neue Folgen erkennen (bereits verarbeitete stehen in der SQLite-DB)
  3. Audio laden -> lokal mit faster-whisper transkribieren
  4. Zusammenfassung + Fachgebiet + Schlagworte per LLM (GitHub Models, Claude-API oder Ollama)
  5. Buch neu erzeugen: buch/forschungsbuch.pdf (+ .html und .md), chronologisch mit Fakten-Kapitel

Aufruf:
  python forschungsbuch.py                 # neue Folgen verarbeiten (Standard: max. 10)
  python forschungsbuch.py --limit 50      # mehr Folgen auf einmal (Nachholen)
  python forschungsbuch.py --ab 2026-01-01 --limit 0   # rückwirkend alles ab 1.1.2026
  python forschungsbuch.py --nur-buch      # nur Buch aus vorhandener DB neu bauen
  python forschungsbuch.py --liste         # zeigt Folgen im Feed + Status
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import feedparser
import requests

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

BASIS = Path(__file__).resolve().parent
UA = {"User-Agent": "Forschungsbuch/1.0 (privater Gebrauch)"}

FACHGEBIETE = [
    "Medizin & Gesundheit", "Biologie & Evolution", "Ökologie & Artenvielfalt",
    "Klima & Atmosphäre", "Geowissenschaften", "Astronomie & Raumfahrt",
    "Physik", "Chemie & Materialien", "Energie", "Technik & Informatik",
    "Künstliche Intelligenz", "Psychologie & Neurowissenschaft",
    "Archäologie & Anthropologie", "Landwirtschaft & Ernährung",
    "Gesellschaft & Politik", "Sonstiges",
]


# --------------------------------------------------------------------------- Konfiguration
def lade_config() -> dict:
    pfad = BASIS / "config.toml"
    if not pfad.exists():
        sys.exit("config.toml fehlt – bitte config.example.toml kopieren und anpassen.")
    with open(pfad, "rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------------- Datenbank
def db_oeffnen() -> sqlite3.Connection:
    con = sqlite3.connect(BASIS / "forschungsbuch.db")
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS folgen (
            guid TEXT PRIMARY KEY,
            titel TEXT,
            datum TEXT,
            link TEXT,
            audio_url TEXT,
            beschreibung TEXT,
            transkript TEXT,
            kernaussage TEXT,
            zusammenfassung TEXT,
            fachgebiet TEXT,
            schlagworte TEXT,
            quelle TEXT,
            status TEXT DEFAULT 'neu',
            fehler TEXT,
            verarbeitet_am TEXT
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS themen (
            guid TEXT,
            nr INTEGER,
            titel TEXT,
            kernaussage TEXT,
            zusammenfassung TEXT,
            fachgebiet TEXT,
            schlagworte TEXT,
            PRIMARY KEY (guid, nr)
        )""")
    spalten = {r[1] for r in con.execute("PRAGMA table_info(themen)")}
    if "fakten" not in spalten:
        con.execute("ALTER TABLE themen ADD COLUMN fakten TEXT")
    if "ausblenden" not in spalten:
        con.execute("ALTER TABLE themen ADD COLUMN ausblenden INTEGER DEFAULT 0")
    con.execute("UPDATE themen SET fachgebiet='Gesellschaft & Politik' WHERE fachgebiet='Wissenschaftspolitik'")
    con.commit()
    return con


# --------------------------------------------------------------------------- Feed
_apple: dict | None = None


def apple_podcast() -> dict:
    """Podcast im Apple-Verzeichnis suchen (liefert collectionId und feedUrl)."""
    global _apple
    if _apple is None:
        _apple = {}
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": "Forschung aktuell Deutschlandfunk", "entity": "podcast", "country": "DE"},
            headers=UA, timeout=20,
        )
        r.raise_for_status()
        for t in r.json().get("results", []):
            if "forschung aktuell" in t.get("collectionName", "").lower() and t.get("feedUrl"):
                _apple = t
                break
    return _apple


def feed_url_ermitteln(cfg: dict) -> str:
    url = cfg.get("feed", {}).get("url", "").strip()
    if url:
        return url
    t = apple_podcast()
    if t.get("feedUrl"):
        print(f"Feed gefunden: {t['feedUrl']}")
        return t["feedUrl"]
    sys.exit("Feed-URL nicht gefunden – bitte in config.toml unter [feed] url eintragen.")


def apple_folgen() -> list[dict]:
    """Ältere Folgen aus dem Apple-Katalog (enthält oft mehr Folgen als der Feed)."""
    try:
        cid = apple_podcast().get("collectionId")
        if not cid:
            return []
        r = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": cid, "entity": "podcastEpisode", "limit": 200, "country": "DE"},
            headers=UA, timeout=30,
        )
        r.raise_for_status()
    except Exception as ex:
        print(f"Apple-Katalog nicht erreichbar: {ex}")
        return []
    folgen = []
    for t in r.json().get("results", []):
        if t.get("wrapperType") != "podcastEpisode" or not t.get("episodeUrl"):
            continue
        datum = ""
        if t.get("releaseDate"):
            datum = datetime.fromisoformat(t["releaseDate"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        folgen.append({
            "guid": t.get("episodeGuid") or t["episodeUrl"],
            "titel": (t.get("trackName") or "").strip(),
            "datum": datum,
            "link": t.get("trackViewUrl", ""),
            "audio_url": t["episodeUrl"],
            "beschreibung": re.sub(r"\s+", " ", t.get("description") or "").strip(),
        })
    return folgen


def alle_folgen(cfg: dict) -> list[dict]:
    """Feed + Apple-Katalog zusammenführen, Dubletten entfernen."""
    folgen = feed_lesen(feed_url_ermitteln(cfg))
    gesehen = {f["guid"] for f in folgen} | {f["audio_url"].split("?")[0] for f in folgen}
    gesehen |= {(f["datum"][:10], f["titel"]) for f in folgen}
    extra = 0
    for f in apple_folgen():
        if (f["guid"] in gesehen or f["audio_url"].split("?")[0] in gesehen
                or (f["datum"][:10], f["titel"]) in gesehen):
            continue
        folgen.append(f)
        gesehen.add(f["guid"])
        extra += 1
    print(f"{len(folgen) - extra} Folgen aus dem Feed, {extra} zusätzliche aus dem Apple-Katalog.")
    return folgen


def feed_lesen(url: str) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    folgen = []
    for e in feed.entries:
        audio = next((l.href for l in e.get("links", []) if l.get("rel") == "enclosure"), None)
        if not audio and e.get("enclosures"):
            audio = e.enclosures[0].get("href")
        if not audio:
            continue
        datum = ""
        if e.get("published_parsed"):
            datum = datetime(*e.published_parsed[:6]).strftime("%Y-%m-%d %H:%M")
        beschreibung = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "")
        folgen.append({
            "guid": e.get("id") or audio,
            "titel": (e.get("title") or "").strip(),
            "datum": datum,
            "link": e.get("link", ""),
            "audio_url": audio,
            "beschreibung": re.sub(r"\s+", " ", html.unescape(beschreibung)).strip(),
        })
    return folgen


# --------------------------------------------------------------------------- Transkription
_whisper = None


def transkribieren(audio_url: str, cfg: dict) -> str:
    global _whisper
    from faster_whisper import WhisperModel

    w = cfg.get("whisper", {})
    if _whisper is None:
        _whisper = WhisperModel(
            w.get("modell", "small"),
            device=w.get("device", "auto"),
            compute_type=w.get("compute_type", "int8"),
        )
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
        with requests.get(audio_url, headers=UA, stream=True, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 16):
                tmp.write(chunk)
        tmp.flush()
        begriffe = lade_glossar().get("whisper", {}).get("begriffe", [])
        hinweis = ("Forschung aktuell im Deutschlandfunk. " + ", ".join(begriffe) + ".") if begriffe else None
        segmente, _ = _whisper.transcribe(tmp.name, language="de", vad_filter=True, initial_prompt=hinweis)
        return " ".join(s.text.strip() for s in segmente)


# --------------------------------------------------------------------------- Zusammenfassung
class TageslimitErreicht(Exception):
    """Das kostenlose Kontingent von GitHub Models ist für heute aufgebraucht."""


PROMPT = """Du erstellst Einträge für ein persönliches Wissensbuch über aktuelle Forschung.
Hier ist das Transkript einer Sendung „Forschung aktuell" (Deutschlandfunk). Eine Sendung enthält
meist mehrere Beiträge zu verschiedenen Themen.

Sendung: {titel}
Datum: {datum}
Transkript:
\"\"\"{text}\"\"\"

Erstelle für JEDEN Beitrag mit wissenschaftlichem Inhalt einen eigenen Eintrag. Lass Nachrichten-
überblicke ohne Forschungsbezug, Moderation und Programmhinweise weg.
Antworte NUR mit JSON in genau diesem Format:
{{
  "themen": [
    {{
      "titel": "Kurzer, sachlicher Titel des Beitrags",
      "kernaussage": "Ein Satz: die wichtigste wissenschaftliche Erkenntnis.",
      "zusammenfassung": "3–6 Sätze in eigenen Worten: Was wurde herausgefunden, von wem (Institution), wie (Methode), warum ist es relevant, welche Einschränkungen gibt es?",
      "fachgebiet": "genau eines aus: {gebiete}",
      "schlagworte": ["3 bis 6 kurze Schlagworte"],
      "fakten": ["0 bis 2 kurze, merkfähige Fakten (höchstens 15 Wörter), z. B. 'Sehr heiße Getränke erhöhen das Risiko für Speiseröhrenkrebs.' – nur eindeutig belegte Aussagen, sonst leere Liste"]
    }}
  ]
}}
Fasse jeden Beitrag nur EINMAL zusammen – auch wenn er im Transkript mehrfach angesprochen wird.
Die Zusammenfassung enthält nur den Inhalt – Fachgebiet und Schlagworte gehören ausschließlich in
ihre eigenen Felder. Keine wörtlichen Zitate, nichts erfinden, was nicht im Transkript steht."""


def zusammenfassen(folge: dict, text: str, cfg: dict) -> dict:
    llm = cfg.get("llm", {})
    prompt = PROMPT.format(
        titel=folge["titel"], datum=folge["datum"],
        text=text[: llm.get("max_zeichen", 18000)], gebiete=", ".join(FACHGEBIETE),
    )
    anbieter = llm.get("anbieter", "ollama")
    if anbieter == "github":
        import os
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GITHUB_TOKEN fehlt (läuft nur in GitHub Actions oder mit eigenem Token).")
        for versuch in range(3):
            r = requests.post(
                "https://models.github.ai/inference/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"model": llm.get("modell", "openai/gpt-4o-mini"),
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2,
                      "max_tokens": 3000},
                timeout=120,
            )
            if r.status_code == 429:
                warten = int(r.headers.get("retry-after", "60") or 60)
                if warten > 120:  # langes Warten = Tageslimit
                    raise TageslimitErreicht(f"Limit erreicht, wieder frei in ca. {warten // 60} min")
                time.sleep(warten)
                continue
            r.raise_for_status()
            break
        else:
            raise TageslimitErreicht("Anfragen werden dauerhaft begrenzt (429)")
        try:
            antwort = r.json()["choices"][0]["message"].get("content") or ""
        except Exception:
            raise RuntimeError(f"Unerwartete Antwort (HTTP {r.status_code}): {r.text[:300]!r}")
        if not antwort.strip():
            raise RuntimeError(f"Leere Antwort vom Modell: {r.text[:300]!r}")
        time.sleep(llm.get("pause_sekunden", 7))  # Minutenlimit einhalten
    elif anbieter == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=llm.get("api_key") or None)  # sonst ANTHROPIC_API_KEY
        msg = client.messages.create(
            model=llm.get("modell", "claude-haiku-5-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        antwort = msg.content[0].text
    elif anbieter == "ollama":
        r = requests.post(
            llm.get("ollama_url", "http://localhost:11434") + "/api/generate",
            json={"model": llm.get("modell", "qwen2.5:7b"), "prompt": prompt,
                  "stream": False, "format": "json",
                  "options": {"num_ctx": llm.get("kontext", 8192), "temperature": 0.2}},
            timeout=1800,
        )
        r.raise_for_status()
        antwort = r.json()["response"]
    else:
        raise ValueError(f"Unbekannter LLM-Anbieter: {anbieter}")

    m = re.search(r"\{.*\}", antwort, re.S)
    try:
        daten = json.loads(m.group(0) if m else antwort)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"Kein gültiges JSON ({ex}): {antwort[:300]!r}")
    themen = daten.get("themen", [daten] if "kernaussage" in daten else [])
    # Doppelte Themen (kleine Modelle wiederholen sich gern) entfernen
    eindeutig = []
    for t in themen:
        if not any(difflib.SequenceMatcher(None, t.get("zusammenfassung", ""), u.get("zusammenfassung", "")).ratio() > 0.8
                   for u in eindeutig):
            eindeutig.append(t)
    themen = eindeutig
    for t in themen:
        t["fakten"] = [f.strip() for f in t.get("fakten", []) if isinstance(f, str) and 10 < len(f.strip()) < 160]
        # Kleine Modelle hängen gern „Das Fachgebiet ist … Schlagworte: …“ an – entfernen
        t["zusammenfassung"] = re.sub(r"\s*(Das )?Fachgebiet( ist)?:?\s.*$", "",
                                      t.get("zusammenfassung", ""), flags=re.S).strip()
        if t.get("fachgebiet") not in FACHGEBIETE:
            t["fachgebiet"] = "Sonstiges"
    return themen


# --------------------------------------------------------------------------- Verarbeitung
def verarbeiten(con: sqlite3.Connection, cfg: dict, limit: int, ab: str = "", max_minuten: int = 0) -> int:
    start = time.monotonic()
    transkripte_behalten = cfg.get("speicher", {}).get("transkripte", True)
    folgen = alle_folgen(cfg)
    if folgen:
        aelteste = min(f["datum"] for f in folgen)
        print(f"Älteste verfügbare Folge: {aelteste[:10]}")
        if ab and aelteste[:10] > ab:
            print(f"Hinweis: Feed und Apple-Katalog reichen nicht bis {ab} zurück – ältere Folgen sind nicht abrufbar.")
    if ab:
        folgen = [f for f in folgen if f["datum"][:10] >= ab]
    bekannt = {r["guid"]: r["status"] for r in con.execute("SELECT guid, status FROM folgen")}
    offen = [f for f in folgen if bekannt.get(f["guid"]) != "fertig"]
    offen.sort(key=lambda f: f["datum"])  # älteste zuerst -> Buch wächst chronologisch
    offen = offen[-limit:] if limit else offen
    print(f"{len(folgen)} Folgen verfügbar, {len(offen)} werden jetzt verarbeitet.")

    neu = 0
    for i, f in enumerate(offen, 1):
        if max_minuten and time.monotonic() - start > max_minuten * 60:
            print(f"Zeitbudget von {max_minuten} min erreicht – Rest folgt beim nächsten Lauf.")
            break
        print(f"[{i}/{len(offen)}] {f['datum']}  {f['titel']}")
        con.execute(
            "INSERT OR IGNORE INTO folgen (guid, titel, datum, link, audio_url, beschreibung) "
            "VALUES (:guid, :titel, :datum, :link, :audio_url, :beschreibung)", f)
        try:
            row = con.execute("SELECT transkript FROM folgen WHERE guid=?", (f["guid"],)).fetchone()
            text = row["transkript"]
            if not text:
                text = transkribieren(f["audio_url"], cfg)
                con.execute("UPDATE folgen SET transkript=? WHERE guid=?", (text, f["guid"]))
                con.commit()
            themen = zusammenfassen(f, text, cfg)
            con.execute("DELETE FROM themen WHERE guid=?", (f["guid"],))
            for nr, t in enumerate(themen, 1):
                con.execute(
                    "INSERT INTO themen (guid, nr, titel, kernaussage, zusammenfassung, fachgebiet, schlagworte, fakten) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f["guid"], nr, t.get("titel", ""), t.get("kernaussage", ""), t.get("zusammenfassung", ""),
                     t["fachgebiet"], json.dumps(t.get("schlagworte", []), ensure_ascii=False),
                     json.dumps(t.get("fakten", []), ensure_ascii=False)))
            print(f"   {len(themen)} Themen")
            con.execute(
                "UPDATE folgen SET quelle=?, status='fertig', fehler=NULL, verarbeitet_am=? WHERE guid=?",
                (cfg.get("llm", {}).get("anbieter", "ollama"),
                 datetime.now().isoformat(timespec="seconds"), f["guid"]))
            if not transkripte_behalten:
                con.execute("UPDATE folgen SET transkript=NULL WHERE guid=?", (f["guid"],))
            neu += 1
        except TageslimitErreicht as ex:
            print(f"   {ex} – Rest folgt beim nächsten Lauf.")
            con.execute("UPDATE folgen SET status='neu' WHERE guid=?", (f["guid"],))
            con.commit()
            break
        except Exception as ex:  # Fehler merken, beim nächsten Lauf erneut versuchen
            print(f"   Fehler: {ex}")
            con.execute("UPDATE folgen SET status='fehler', fehler=? WHERE guid=?", (str(ex), f["guid"]))
        con.commit()
        time.sleep(1)
    return neu


# --------------------------------------------------------------------------- Buch erzeugen
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]

FARBEN = {  # eine Farbe je Fachgebiet (Etiketten in PDF und HTML)
    "Medizin & Gesundheit": "#c0392b", "Biologie & Evolution": "#2e7d32",
    "Ökologie & Artenvielfalt": "#558b2f", "Klima & Atmosphäre": "#0277bd",
    "Geowissenschaften": "#795548", "Astronomie & Raumfahrt": "#283593",
    "Physik": "#5e35b1", "Chemie & Materialien": "#00838f", "Energie": "#ef6c00",
    "Technik & Informatik": "#455a64", "Künstliche Intelligenz": "#6a1b9a",
    "Psychologie & Neurowissenschaft": "#ad1457", "Archäologie & Anthropologie": "#8d6e63",
    "Landwirtschaft & Ernährung": "#9e9d24", "Gesellschaft & Politik": "#37474f",
    "Sonstiges": "#757575",
}


def datum_lang(d: str) -> str:
    t = datetime.strptime(d[:10], "%Y-%m-%d")
    return f"{WOCHENTAGE[t.weekday()]}, {t.day}. {MONATE[t.month - 1]} {t.year}"


def datum_kurz(d: str) -> str:
    t = datetime.strptime(d[:10], "%Y-%m-%d")
    return f"{t.day}.{t.month}.{t.year}"


def monat_name(d: str) -> str:
    return f"{MONATE[int(d[5:7]) - 1]} {d[:4]}"


def lade_glossar() -> dict:
    pfad = BASIS / "glossar.toml"
    if not pfad.exists():
        return {}
    with open(pfad, "rb") as f:
        return tomllib.load(f)


def korrigieren(text: str, korrekturen: dict) -> str:
    for falsch, richtig in korrekturen.items():
        text = text.replace(falsch, richtig)
    return text


def buch_daten(con: sqlite3.Connection, cfg: dict) -> dict:
    """Alle sichtbaren Themen, korrigiert und sortiert, gruppiert nach Monat und Sendung."""
    korr = lade_glossar().get("korrekturen", {})
    neueste_zuerst = cfg.get("buch", {}).get("reihenfolge", "neueste_zuerst") == "neueste_zuerst"
    zeilen = con.execute(
        "SELECT t.guid, t.nr, t.titel, t.kernaussage, t.zusammenfassung, t.fachgebiet, t.schlagworte, "
        "t.fakten, f.datum, f.link, f.titel AS sendung FROM themen t JOIN folgen f ON f.guid = t.guid "
        "WHERE f.status='fertig' AND COALESCE(t.ausblenden, 0) = 0 "
        "ORDER BY f.datum, f.guid, t.nr").fetchall()
    eintraege = []
    for r in zeilen:
        k = lambda s: korrigieren(s or "", korr)
        eintraege.append({
            "id": "e" + hashlib.sha1(f"{r['guid']}#{r['nr']}".encode()).hexdigest()[:10],
            "guid": r["guid"], "datum": r["datum"], "link": r["link"] or "",
            "sendung": k(r["sendung"]), "titel": k(r["titel"]), "kern": k(r["kernaussage"]),
            "text": k(r["zusammenfassung"]), "gebiet": r["fachgebiet"] or "Sonstiges",
            "tags": [k(t) for t in json.loads(r["schlagworte"] or "[]")],
            "fakten": [k(t) for t in json.loads(r["fakten"] or "[]")],
        })
    if neueste_zuerst:
        eintraege.sort(key=lambda e: e["datum"], reverse=True)  # stabil: Themenfolge bleibt erhalten

    monate: list[dict] = []
    for e in eintraege:
        mk = e["datum"][:7]
        if not monate or monate[-1]["key"] != mk:
            monate.append({"key": mk, "name": monat_name(e["datum"]), "sendungen": []})
        sendungen = monate[-1]["sendungen"]
        if not sendungen or sendungen[-1]["guid"] != e["guid"]:
            sendungen.append({"guid": e["guid"], "datum": e["datum"], "titel": e["sendung"],
                              "link": e["link"], "themen": []})
        sendungen[-1]["themen"].append(e)

    gebiete: dict[str, list] = {g: [] for g in FACHGEBIETE}
    for e in eintraege:
        gebiete.setdefault(e["gebiet"], []).append(e)
    gebiete = {g: v for g, v in gebiete.items() if v}

    fakten = {g: [(f, e) for e in v for f in e["fakten"]] for g, v in gebiete.items()}
    fakten = {g: v for g, v in fakten.items() if v}

    datums = [e["datum"] for e in eintraege]
    return {
        "eintraege": eintraege, "monate": monate, "gebiete": gebiete, "fakten": fakten,
        "n_sendungen": len({e["guid"] for e in eintraege}), "n_themen": len(eintraege),
        "n_fakten": sum(len(v) for v in fakten.values()),
        "von": min(datums) if datums else "", "bis": max(datums) if datums else "",
        "neueste_zuerst": neueste_zuerst,
    }


def buch_markdown(b: dict, ziel: Path) -> None:
    z = ["# Forschungsbuch\n",
         f"*Kurzfassungen aus „Forschung aktuell“ (Deutschlandfunk) · {b['n_themen']} Themen aus "
         f"{b['n_sendungen']} Sendungen · Stand {datetime.now():%d.%m.%Y}. Maschinell erstellt – "
         f"Details bitte im Originalbeitrag prüfen.*\n"]
    if b["fakten"]:
        z.append("## Kurz & knapp\n")
        for g, liste in b["fakten"].items():
            z.append(f"### {g}\n")
            z += [f"- {f} *({datum_kurz(e['datum'])})*" for f, e in liste]
            z.append("")
    for m in b["monate"]:
        z.append(f"## {m['name']}\n")
        for s in m["sendungen"]:
            link = f" · [Sendung anhören]({s['link']})" if s["link"] else ""
            z.append(f"### {datum_lang(s['datum'])}\n")
            z.append(f"*{s['titel']}*{link}\n")
            for e in s["themen"]:
                z.append(f"#### {e['titel']}\n")
                z.append(f"`{e['gebiet']}` **{e['kern']}**\n")
                z.append(f"{e['text']}\n")
                for f in e["fakten"]:
                    z.append(f"> **Merke:** {f}\n")
                if e["tags"]:
                    z.append(f"*{' · '.join(e['tags'])}*\n")
    ziel.write_text("\n".join(z), encoding="utf-8")


def buch_html(b: dict, ziel: Path) -> None:
    e_ = html.escape
    chips = "".join(
        f'<button class="chip" data-g="{e_(g)}" style="--c:{FARBEN.get(g, "#757575")}">{e_(g)} '
        f'<span>{len(v)}</span></button>' for g, v in b["gebiete"].items())
    monatsnav = "".join(f'<a href="#m{m["key"]}">{e_(m["name"])}</a>' for m in b["monate"])

    fakten = []
    for g, liste in b["fakten"].items():
        punkte = "".join(f'<li>{e_(f)} <a href="#{e["id"]}">{datum_kurz(e["datum"])}</a></li>'
                         for f, e in liste)
        fakten.append(f'<h3 style="color:{FARBEN.get(g, "#757575")}">{e_(g)}</h3><ul>{punkte}</ul>')

    teile = []
    for m in b["monate"]:
        teile.append(f'<h2 class="monat" id="m{m["key"]}">{e_(m["name"])}</h2>')
        for s in m["sendungen"]:
            link = (f' · <a href="{e_(s["link"])}" target="_blank" rel="noopener">anhören</a>'
                    if s["link"] else "")
            karten = []
            for e in s["themen"]:
                farbe = FARBEN.get(e["gebiet"], "#757575")
                merke = "".join(f'<p class="merke"><b>Merke:</b> {e_(f)}</p>' for f in e["fakten"])
                tags = "".join(f"<em>{e_(t)}</em>" for t in e["tags"])
                karten.append(
                    f'<article id="{e["id"]}" data-g="{e_(e["gebiet"])}" style="--c:{farbe}">'
                    f'<div class="gebiet">{e_(e["gebiet"])}</div><h4>{e_(e["titel"])}</h4>'
                    f'<p class="kern">{e_(e["kern"])}</p><p>{e_(e["text"])}</p>{merke}'
                    f'<div class="tags">{tags}</div></article>')
            teile.append(f'<section class="tag"><h3>{datum_lang(s["datum"])}</h3>'
                         f'<div class="sendung">{e_(s["titel"])}{link}</div>{"".join(karten)}</section>')

    seite = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Forschungsbuch</title>
<style>
:root{{--bg:#faf8f4;--fg:#222;--muted:#6b6b6b;--card:#fff;--line:#e4dfd6;--acc:#1d5c63}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181a;--fg:#e6e6e6;--muted:#9a9a9a;--card:#1f2225;--line:#33373b;--acc:#6fc0c8}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 Georgia,serif}}
header,main{{max-width:820px;margin:auto;padding:0 16px}} header{{padding-top:24px}}
h1{{margin:0;font-size:2rem;color:var(--acc)}} .unter{{color:var(--muted);margin:.3em 0 1em}}
input{{width:100%;padding:10px 12px;font-size:1rem;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}}
.chips,.monate{{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}}
.chip{{font:13px system-ui;padding:4px 10px;border:1px solid var(--c);border-radius:99px;background:none;color:var(--fg);cursor:pointer}}
.chip span{{color:var(--muted)}} .chip.an{{background:var(--c);color:#fff}} .chip.an span{{color:#fff}}
.monate a{{font:13px system-ui;color:var(--acc)}}
details{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin:16px 0}}
summary{{font:600 1.05rem system-ui;cursor:pointer;color:var(--acc)}} details h3{{font:600 .95rem system-ui;margin:1em 0 .2em}}
details li{{margin:.25em 0}} details li a{{font:12px system-ui;color:var(--muted)}}
h2.monat{{border-bottom:2px solid var(--acc);padding-bottom:4px;margin-top:2em;color:var(--acc)}}
section.tag h3{{font:600 1rem system-ui;margin:1.6em 0 0}} .sendung{{font:13px system-ui;color:var(--muted);margin-bottom:6px}}
.sendung a{{color:var(--acc)}}
article{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:10px;padding:12px 16px;margin:10px 0}}
.gebiet{{font:600 11px system-ui;letter-spacing:.04em;text-transform:uppercase;color:var(--c)}}
h4{{margin:.15em 0 .35em;font-size:1.08rem}} .kern{{font-weight:bold}}
.merke{{background:var(--bg);border-radius:6px;padding:6px 10px;font-size:.95rem}}
.tags em{{font:12px system-ui;font-style:normal;color:var(--muted);margin-right:8px}}
.versteckt{{display:none}}
</style></head><body>
<header><h1>Forschungsbuch</h1>
<p class="unter">Kurzfassungen aus „Forschung aktuell“ (Deutschlandfunk) · {b['n_themen']} Themen aus {b['n_sendungen']} Sendungen ·
{b['n_fakten']} Fakten · Stand {datetime.now():%d.%m.%Y}</p>
<input id="q" placeholder="Durchsuchen …" oninput="filtern()">
<div class="chips">{chips}</div><div class="monate">{monatsnav}</div></header>
<main>
<details><summary>Kurz &amp; knapp – {b['n_fakten']} Fakten</summary>{"".join(fakten)}</details>
{"".join(teile)}
</main>
<script>
const aktiv=new Set();
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{{const g=c.dataset.g;
  aktiv.has(g)?aktiv.delete(g):aktiv.add(g);c.classList.toggle('an');filtern()}});
function filtern(){{const q=document.getElementById('q').value.toLowerCase();
  document.querySelectorAll('article').forEach(a=>{{const ok=(!aktiv.size||aktiv.has(a.dataset.g))&&a.textContent.toLowerCase().includes(q);
    a.classList.toggle('versteckt',!ok)}});
  document.querySelectorAll('section.tag').forEach(s=>s.classList.toggle('versteckt',!s.querySelector('article:not(.versteckt)')));
  document.querySelectorAll('h2.monat').forEach(h=>{{let n=h.nextElementSibling,sicht=false;
    while(n&&!n.matches('h2.monat')){{if(!n.classList.contains('versteckt'))sicht=true;n=n.nextElementSibling}}
    h.classList.toggle('versteckt',!sicht)}})}}
</script></body></html>"""
    ziel.write_text(seite, encoding="utf-8")


def _schriften() -> tuple[str, str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    kandidaten = ["/usr/share/fonts/truetype/dejavu", "/Library/Fonts",
                  str(Path.home() / "Library/Fonts"), "C:/Windows/Fonts"]
    for d in kandidaten:
        n, b, s = (Path(d) / f for f in ("DejaVuSerif.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"))
        if n.exists() and b.exists() and s.exists():
            pdfmetrics.registerFont(TTFont("Text", str(n)))
            pdfmetrics.registerFont(TTFont("Fett", str(b)))
            pdfmetrics.registerFont(TTFont("Sans", str(s)))
            return "Text", "Fett", "Sans"
        n, b = Path(d) / "DejaVuSans.ttf", Path(d) / "DejaVuSans-Bold.ttf"
        if n.exists() and b.exists():
            pdfmetrics.registerFont(TTFont("Text", str(n)))
            pdfmetrics.registerFont(TTFont("Fett", str(b)))
            return "Text", "Fett", "Text"
    return "Helvetica", "Helvetica-Bold", "Helvetica"


def buch_pdf(b: dict, ziel: Path) -> None:
    """Buch als PDF im Format A5 – handyfreundlich, mit Lesezeichen und internen Links."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (CondPageBreak, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    text, fett, sans = _schriften()
    akzent, grau = "#1d5c63", "#6b6b6b"
    P = lambda name, **kw: ParagraphStyle(name, **kw)
    st = {
        "titel": P("titel", fontName=fett, fontSize=28, leading=32, textColor=akzent),
        "unter": P("unter", fontName=sans, fontSize=10, leading=14, textColor=grau),
        "zahl": P("zahl", fontName=fett, fontSize=18, leading=20, textColor=akzent, alignment=1),
        "zahltext": P("zahltext", fontName=sans, fontSize=8, leading=10, textColor=grau, alignment=1),
        "h1": P("h1", fontName=fett, fontSize=19, leading=23, textColor=akzent, spaceAfter=6),
        "inhalt": P("inhalt", fontName=sans, fontSize=10.5, leading=17),
        "tag": P("tag", fontName=fett, fontSize=11, leading=14, spaceBefore=10, textColor=akzent),
        "sendung": P("sendung", fontName=sans, fontSize=7.5, leading=9.5, textColor=grau, spaceAfter=2),
        "gebiet": P("gebiet", fontName=fett, fontSize=6.5, leading=8, spaceBefore=7),
        "eintrag": P("eintrag", fontName=fett, fontSize=10.5, leading=13, spaceAfter=2),
        "kern": P("kern", fontName=fett, fontSize=9, leading=12, spaceAfter=3),
        "text": P("text", fontName=text, fontSize=8.8, leading=12.4),
        "merke": P("merke", fontName=sans, fontSize=8.3, leading=11, leftIndent=6, borderPadding=(3, 4, 3, 4),
                   backColor=colors.HexColor("#eef5f5"), spaceBefore=5, spaceAfter=1),
        "tags": P("tags", fontName=sans, fontSize=7, leading=9, textColor=grau, spaceBefore=3),
        "fgebiet": P("fgebiet", fontName=fett, fontSize=10, leading=13, spaceBefore=9, spaceAfter=2),
        "fakt": P("fakt", fontName=sans, fontSize=8.6, leading=11.5, leftIndent=9, firstLineIndent=-9,
                  spaceAfter=2.5),
        "reg": P("reg", fontName=sans, fontSize=8.3, leading=11, leftIndent=34, firstLineIndent=-34),
    }
    e_ = lambda t: html.escape(t or "", quote=True)

    class Buch(SimpleDocTemplate):
        def afterFlowable(self, fl):
            ziel_ = getattr(fl, "_lesezeichen", None)
            if ziel_:
                key, titel, ebene = ziel_
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(titel, key, level=ebene, closed=ebene == 0)

    def mit_lesezeichen(p, key, titel, ebene):
        p._lesezeichen = (key, titel, ebene)
        return p

    def seitenrand(canv, doc):
        canv.saveState()
        canv.setFont(sans, 7)
        canv.setFillColor(colors.HexColor(grau))
        if doc.page > 1:
            canv.drawString(12 * mm, 8 * mm, "Forschungsbuch")
            canv.drawRightString(A5[0] - 12 * mm, 8 * mm, str(doc.page))
        canv.restoreState()

    # Titelseite
    zeitraum = f"{datum_kurz(b['von'])} – {datum_kurz(b['bis'])}" if b["von"] else ""
    zahlen = Table([[Paragraph(str(b[k]), st["zahl"]) for k in ("n_sendungen", "n_themen", "n_fakten")],
                    [Paragraph(t, st["zahltext"]) for t in ("Sendungen", "Themen", "Fakten")]],
                   colWidths=[38 * mm] * 3)
    zahlen.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.6, colors.HexColor(akzent)),
                                ("TOPPADDING", (0, 0), (-1, 0), 8)]))
    story = [Spacer(1, 32 * mm), Paragraph("Forschungsbuch", st["titel"]), Spacer(1, 3 * mm),
             Paragraph("Kurzfassungen aus „Forschung aktuell“ (Deutschlandfunk)", st["unter"]),
             Paragraph(f"{zeitraum} · Stand {datetime.now():%d.%m.%Y}", st["unter"]),
             Spacer(1, 14 * mm), zahlen, Spacer(1, 16 * mm),
             Paragraph("Maschinell erstellte Zusammenfassungen. Details bitte im verlinkten "
                       "Originalbeitrag prüfen.", st["unter"]),
             PageBreak()]

    # Inhalt
    link = lambda ziel_, t, extra="": (f'<a href="#{ziel_}" color="{akzent}">{e_(t)}</a>'
                                        f'<font color="{grau}">{extra}</font>')
    story.append(mit_lesezeichen(Paragraph("Inhalt", st["h1"]), "inhalt", "Inhalt", 0))
    if b["fakten"]:
        story.append(Paragraph(link("fakten", "Kurz & knapp", f"  {b['n_fakten']} Fakten"), st["inhalt"]))
    story.append(Spacer(1, 4))
    for m in b["monate"]:
        n = sum(len(s["themen"]) for s in m["sendungen"])
        story.append(Paragraph(link(f"m{m['key']}", m["name"], f"  {len(m['sendungen'])} Sendungen · {n} Themen"),
                               st["inhalt"]))
    story += [Spacer(1, 4), Paragraph(link("register", "Register nach Fachgebiet"), st["inhalt"])]

    # Kurz & knapp
    if b["fakten"]:
        story += [PageBreak(), mit_lesezeichen(Paragraph("Kurz &amp; knapp", st["h1"]), "fakten", "Kurz & knapp", 0)]
        for g, liste in b["fakten"].items():
            farbe = FARBEN.get(g, grau)
            story.append(CondPageBreak(25 * mm))
            story.append(Paragraph(f'<font color="{farbe}">■</font> {e_(g)}', st["fgebiet"]))
            for f, e in liste:
                story.append(Paragraph(
                    f'<font color="{farbe}">•</font> {e_(f)} '
                    f'<a href="#{e["id"]}" color="{grau}"><font size="6.5">{datum_kurz(e["datum"])}</font></a>',
                    st["fakt"]))

    # Chronik
    for m in b["monate"]:
        story += [PageBreak(), mit_lesezeichen(Paragraph(e_(m["name"]), st["h1"]), f"m{m['key']}", m["name"], 0)]
        for s in m["sendungen"]:
            anh = f' · <a href="{e_(s["link"])}" color="{akzent}">anhören</a>' if s["link"] else ""
            kopf = [mit_lesezeichen(Paragraph(datum_lang(s["datum"]), st["tag"]),
                                    "s" + hashlib.sha1(s["guid"].encode()).hexdigest()[:10],
                                    datum_lang(s["datum"]), 1),
                    Paragraph(f"{e_(s['titel'])}{anh}", st["sendung"])]
            for i, e in enumerate(s["themen"]):
                farbe = FARBEN.get(e["gebiet"], grau)
                block = [Paragraph(f'<a name="{e["id"]}"/><font color="{farbe}">{e_(e["gebiet"]).upper()}</font>',
                                   st["gebiet"]),
                         Paragraph(e_(e["titel"]), st["eintrag"]),
                         Paragraph(e_(e["kern"]), st["kern"]),
                         Paragraph(e_(e["text"]), st["text"])]
                block += [Paragraph(f"<b>Merke:</b> {e_(f)}", st["merke"]) for f in e["fakten"]]
                if e["tags"]:
                    block.append(Paragraph(e_(" · ".join(e["tags"])), st["tags"]))
                story.append(KeepTogether((kopf if i == 0 else []) + block))

    # Register
    story += [PageBreak(), mit_lesezeichen(Paragraph("Register nach Fachgebiet", st["h1"]), "register",
                                           "Register nach Fachgebiet", 0)]
    for g, liste in b["gebiete"].items():
        story.append(CondPageBreak(20 * mm))
        story.append(Paragraph(f'<font color="{FARBEN.get(g, grau)}">■</font> {e_(g)} '
                               f'<font color="{grau}" size="8">({len(liste)})</font>', st["fgebiet"]))
        for e in sorted(liste, key=lambda e: e["datum"], reverse=b["neueste_zuerst"]):
            story.append(Paragraph(f'<font color="{grau}">{datum_kurz(e["datum"])[:-5]}</font>&nbsp;&nbsp;'
                                   f'<a href="#{e["id"]}" color="#222222">{e_(e["titel"])}</a>', st["reg"]))

    doc = Buch(str(ziel), pagesize=A5, leftMargin=12 * mm, rightMargin=12 * mm,
               topMargin=13 * mm, bottomMargin=15 * mm, title="Forschungsbuch",
               author="Forschungsbuch (automatisch)", subject="Kurzfassungen aus „Forschung aktuell“")
    doc.build(story, onFirstPage=seitenrand, onLaterPages=seitenrand)


def buch_bauen(con: sqlite3.Connection, cfg: dict | None = None) -> None:
    ordner = BASIS / "buch"
    ordner.mkdir(exist_ok=True)
    b = buch_daten(con, cfg or {})
    buch_markdown(b, ordner / "forschungsbuch.md")
    buch_html(b, ordner / "forschungsbuch.html")
    buch_pdf(b, ordner / "forschungsbuch.pdf")
    status_schreiben(con, ordner / "status.md")
    print(f"Buch aktualisiert: {ordner / 'forschungsbuch.pdf'}  "
          f"({b['n_themen']} Themen, {b['n_fakten']} Fakten)")


def status_schreiben(con: sqlite3.Connection, ziel: Path) -> None:
    z = [f"# Verarbeitungsstand ({datetime.now():%d.%m.%Y %H:%M})\n"]
    for r in con.execute("SELECT status, COUNT(*) n, MIN(datum) von, MAX(datum) bis FROM folgen GROUP BY status"):
        z.append(f"- **{r['status']}**: {r['n']} Sendungen ({(r['von'] or '')[:10]} bis {(r['bis'] or '')[:10]})")
    n = con.execute("SELECT COUNT(*) FROM themen WHERE COALESCE(ausblenden, 0) = 0").fetchone()[0]
    nf = sum(len(json.loads(r[0] or "[]")) for r in
             con.execute("SELECT fakten FROM themen WHERE COALESCE(ausblenden, 0) = 0"))
    z.append(f"- **Themen im Buch**: {n}")
    z.append(f"- **Fakten**: {nf}\n")
    fehler = con.execute("SELECT datum, titel, fehler FROM folgen WHERE status='fehler' "
                         "ORDER BY datum DESC LIMIT 10").fetchall()
    if fehler:
        z.append("## Letzte Fehler\n")
        z += [f"- {r['datum'][:10]} {r['titel']}: `{(r['fehler'] or '')[:200]}`" for r in fehler]
    ziel.write_text("\n".join(z) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10, help="max. Folgen pro Lauf (0 = alle im Feed)")
    ap.add_argument("--nur-buch", action="store_true", help="nur Buch neu erzeugen")
    ap.add_argument("--ab", default="", help="nur Folgen ab Datum JJJJ-MM-TT, z. B. --ab 2026-01-01 --limit 0")
    ap.add_argument("--max-minuten", type=int, default=0, help="nach N Minuten keine neue Folge mehr beginnen")
    ap.add_argument("--liste", action="store_true", help="Feed-Folgen mit Status anzeigen")
    args = ap.parse_args()

    cfg = lade_config()
    con = db_oeffnen()

    if args.liste:
        status = {r["guid"]: r["status"] for r in con.execute("SELECT guid, status FROM folgen")}
        for f in sorted(alle_folgen(cfg), key=lambda f: f["datum"]):
            print(f"{status.get(f['guid'], '-'):7} {f['datum']}  {f['titel']}")
        return
    if not args.nur_buch:
        verarbeiten(con, cfg, args.limit, args.ab, args.max_minuten)
    buch_bauen(con, cfg)


if __name__ == "__main__":
    main()
