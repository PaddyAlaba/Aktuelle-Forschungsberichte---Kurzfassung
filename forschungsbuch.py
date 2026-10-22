#!/usr/bin/env python3
"""
Forschungsbuch – baut aus dem Dlf-Podcast „Forschung aktuell" ein wachsendes Wissensbuch.

Ablauf pro Lauf:
  1. RSS-Feed abrufen (URL aus config, sonst automatisch über die iTunes-Suche ermittelt)
  2. Neue Folgen erkennen (bereits verarbeitete stehen in der SQLite-DB)
  3. Audio laden -> lokal mit faster-whisper transkribieren
  4. Zusammenfassung + Fachgebiet + Schlagworte per LLM (GitHub Models, Claude-API oder Ollama)
  5. Buch neu erzeugen: buch/forschungsbuch.pdf (+ .html und .md)

Aufruf:
  python forschungsbuch.py                 # neue Folgen verarbeiten (Standard: max. 10)
  python forschungsbuch.py --limit 50      # mehr Folgen auf einmal (Nachholen)
  python forschungsbuch.py --ab 2026-01-01 --limit 0   # rückwirkend alles ab 1.1.2026
  python forschungsbuch.py --nur-buch      # nur Buch aus vorhandener DB neu bauen
  python forschungsbuch.py --liste         # zeigt Folgen im Feed + Status
"""
from __future__ import annotations

import argparse
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
    "Wissenschaftspolitik", "Sonstiges",
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
        segmente, _ = _whisper.transcribe(tmp.name, language="de", vad_filter=True)
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
      "schlagworte": ["3 bis 6 kurze Schlagworte"]
    }}
  ]
}}
Keine wörtlichen Zitate, nichts erfinden, was nicht im Transkript steht."""


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
    for t in themen:
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
                    "INSERT INTO themen (guid, nr, titel, kernaussage, zusammenfassung, fachgebiet, schlagworte) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f["guid"], nr, t.get("titel", ""), t.get("kernaussage", ""), t.get("zusammenfassung", ""),
                     t["fachgebiet"], json.dumps(t.get("schlagworte", []), ensure_ascii=False)))
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
def eintraege(con: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    kapitel: dict[str, list] = {g: [] for g in FACHGEBIETE}
    for r in con.execute(
            "SELECT t.titel, t.kernaussage, t.zusammenfassung, t.fachgebiet, t.schlagworte, "
            "f.datum, f.link, f.titel AS sendung FROM themen t JOIN folgen f ON f.guid = t.guid "
            "WHERE f.status='fertig' ORDER BY f.datum DESC, t.nr"):
        kapitel.setdefault(r["fachgebiet"] or "Sonstiges", []).append(r)
    return {k: v for k, v in kapitel.items() if v}


def buch_markdown(kapitel: dict, ziel: Path) -> None:
    gesamt = sum(len(v) for v in kapitel.values())
    z = [f"# Forschungsbuch\n",
         f"*Kurzfassungen aus „Forschung aktuell“ (Deutschlandfunk) – {gesamt} Einträge, "
         f"Stand {datetime.now():%d.%m.%Y}. Private Notizen, maschinell erstellt; "
         f"Details immer im Originalbeitrag prüfen.*\n",
         "## Inhalt\n"]
    for k, v in kapitel.items():
        anker = re.sub(r"[^\w]+", "-", k.lower()).strip("-")
        z.append(f"- [{k}](#{anker}) ({len(v)})")
    for k, v in kapitel.items():
        z.append(f"\n## {k}\n")
        for r in v:
            tags = ", ".join(json.loads(r["schlagworte"] or "[]"))
            z.append(f"### {r['titel']}\n")
            z.append(f"*{r['datum'][:10]}* · [Originalbeitrag]({r['link']}) · {tags}\n")
            z.append(f"**{r['kernaussage']}**\n")
            z.append(f"{r['zusammenfassung']}\n")
    ziel.write_text("\n".join(z), encoding="utf-8")


def buch_html(kapitel: dict, ziel: Path) -> None:
    e = html.escape
    gesamt = sum(len(v) for v in kapitel.values())
    nav, inhalt = [], []
    for k, v in kapitel.items():
        anker = re.sub(r"[^\w]+", "-", k.lower()).strip("-")
        nav.append(f'<a href="#{anker}">{e(k)} <span>{len(v)}</span></a>')
        karten = []
        for r in v:
            tags = "".join(f"<em>{e(t)}</em>" for t in json.loads(r["schlagworte"] or "[]"))
            karten.append(
                f'<article><div class="meta">{e(r["datum"][:10])} · '
                f'<a href="{e(r["link"])}" target="_blank" rel="noopener">Original</a></div>'
                f'<h3>{e(r["titel"])}</h3><p class="kern">{e(r["kernaussage"] or "")}</p>'
                f'<p>{e(r["zusammenfassung"] or "")}</p><div class="tags">{tags}</div></article>')
        inhalt.append(f'<section id="{anker}"><h2>{e(k)}</h2>{"".join(karten)}</section>')

    seite = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Forschungsbuch</title>
<style>
:root{{--bg:#faf8f4;--fg:#222;--muted:#777;--card:#fff;--line:#e4dfd6;--acc:#1d5c63}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181a;--fg:#e6e6e6;--muted:#999;--card:#1f2225;--line:#33373b;--acc:#6fc0c8}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 Georgia,serif}}
header{{padding:24px 16px 8px;max-width:820px;margin:auto}}
h1{{margin:0;font-size:2rem}} header p{{color:var(--muted);margin:.3em 0}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font-size:1rem;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);margin:12px 0}}
nav{{display:flex;flex-wrap:wrap;gap:6px}} nav a{{font:13px system-ui;padding:4px 10px;border:1px solid var(--line);border-radius:99px;color:var(--fg);text-decoration:none}}
nav span{{color:var(--muted)}}
main{{max-width:820px;margin:auto;padding:0 16px 60px}}
h2{{border-bottom:2px solid var(--acc);padding-bottom:4px;margin-top:2em}}
article{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}}
h3{{margin:.2em 0 .4em;font-size:1.1rem}} .meta{{font:12px system-ui;color:var(--muted)}} .meta a{{color:var(--acc)}}
.kern{{font-weight:bold}} .tags em{{font:12px system-ui;font-style:normal;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 6px;margin-right:4px}}
</style></head><body>
<header><h1>Forschungsbuch</h1>
<p>Kurzfassungen aus „Forschung aktuell" (Deutschlandfunk) · {gesamt} Einträge · Stand {datetime.now():%d.%m.%Y}</p>
<input id="q" placeholder="Durchsuchen …" oninput="suche(this.value)"><nav>{"".join(nav)}</nav></header>
<main>{"".join(inhalt)}</main>
<script>
function suche(q){{q=q.toLowerCase();document.querySelectorAll('article').forEach(a=>{{a.style.display=a.textContent.toLowerCase().includes(q)?'':'none'}});
document.querySelectorAll('section').forEach(s=>{{s.style.display=[...s.querySelectorAll('article')].some(a=>a.style.display!=='none')?'':'none'}})}}
</script></body></html>"""
    ziel.write_text(seite, encoding="utf-8")


def _schriften() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    kandidaten = [
        "/usr/share/fonts/truetype/dejavu",            # Linux / GitHub Actions
        "/Library/Fonts", str(Path.home() / "Library/Fonts"),  # macOS (falls DejaVu installiert)
        "C:/Windows/Fonts",
    ]
    for d in kandidaten:
        n, b = Path(d) / "DejaVuSans.ttf", Path(d) / "DejaVuSans-Bold.ttf"
        if n.exists() and b.exists():
            pdfmetrics.registerFont(TTFont("Buch", str(n)))
            pdfmetrics.registerFont(TTFont("Buch-Fett", str(b)))
            return "Buch", "Buch-Fett"
    return "Helvetica", "Helvetica-Bold"  # Fallback (ohne „“ und einige Sonderzeichen)


def buch_pdf(kapitel: dict, ziel: Path) -> None:
    """Buch als PDF im Format A5 – gut lesbar auf dem Handy, mit Lesezeichen je Kapitel."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer)

    normal, fett = _schriften()
    akzent = colors.HexColor("#1d5c63")
    grau = colors.HexColor("#777777")
    st = {
        "titel": ParagraphStyle("titel", fontName=fett, fontSize=26, leading=30, textColor=akzent, spaceAfter=8),
        "unter": ParagraphStyle("unter", fontName=normal, fontSize=10, leading=14, textColor=grau),
        "inhalt": ParagraphStyle("inhalt", fontName=normal, fontSize=11, leading=18),
        "kapitel": ParagraphStyle("kapitel", fontName=fett, fontSize=17, leading=21, textColor=akzent,
                                  spaceAfter=8),
        "eintrag": ParagraphStyle("eintrag", fontName=fett, fontSize=11.5, leading=14.5, spaceBefore=10,
                                  spaceAfter=2),
        "meta": ParagraphStyle("meta", fontName=normal, fontSize=8, leading=10, textColor=grau, spaceAfter=3),
        "kern": ParagraphStyle("kern", fontName=fett, fontSize=9.5, leading=13, spaceAfter=3),
        "text": ParagraphStyle("text", fontName=normal, fontSize=9.5, leading=13.5),
        "tags": ParagraphStyle("tags", fontName=normal, fontSize=8, leading=10, textColor=akzent, spaceBefore=2),
    }
    e = lambda t: html.escape(t or "", quote=True)
    anker = lambda k: "k-" + re.sub(r"[^\w]+", "-", k.lower()).strip("-")

    class Buch(SimpleDocTemplate):
        def afterFlowable(self, fl):
            key = getattr(fl, "_anker", None)
            if key:
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(fl.getPlainText(), key, level=0)

    def fusszeile(canv, doc):
        canv.saveState()
        canv.setFont(normal, 8)
        canv.setFillColor(grau)
        canv.drawCentredString(A5[0] / 2, 8 * mm, f"– {doc.page} –")
        canv.restoreState()

    gesamt = sum(len(v) for v in kapitel.values())
    story = [
        Spacer(1, 30 * mm),
        Paragraph("Forschungsbuch", st["titel"]),
        Paragraph(f"Kurzfassungen aus „Forschung aktuell“ (Deutschlandfunk)<br/>"
                  f"{gesamt} Einträge · Stand {datetime.now():%d.%m.%Y}", st["unter"]),
        Spacer(1, 6 * mm),
        Paragraph("Maschinell erstellte Zusammenfassungen. Details bitte im verlinkten "
                  "Originalbeitrag prüfen.", st["unter"]),
        Spacer(1, 12 * mm),
        Paragraph("Inhalt", st["kapitel"]),
    ]
    for k, v in kapitel.items():
        story.append(Paragraph(f'<a href="#{anker(k)}" color="#1d5c63">{e(k)}</a> '
                               f'<font color="#777777">({len(v)})</font>', st["inhalt"]))

    for k, v in kapitel.items():
        story.append(PageBreak())
        kopf = Paragraph(e(k), st["kapitel"])
        kopf._anker = anker(k)
        story.append(kopf)
        for r in v:
            tags = " · ".join(json.loads(r["schlagworte"] or "[]"))
            link = f' · <a href="{e(r["link"])}" color="#1d5c63">Originalbeitrag</a>' if r["link"] else ""
            story.append(KeepTogether([
                Paragraph(e(r["titel"]), st["eintrag"]),
                Paragraph(f'{e(r["datum"][:10])}{link}', st["meta"]),
                Paragraph(e(r["kernaussage"]), st["kern"]),
                Paragraph(e(r["zusammenfassung"]), st["text"]),
                Paragraph(e(tags), st["tags"]),
            ]))

    doc = Buch(str(ziel), pagesize=A5, leftMargin=12 * mm, rightMargin=12 * mm,
               topMargin=14 * mm, bottomMargin=16 * mm, title="Forschungsbuch",
               author="Forschungsbuch (automatisch)")
    doc.build(story, onFirstPage=fusszeile, onLaterPages=fusszeile)


def buch_bauen(con: sqlite3.Connection) -> None:
    ordner = BASIS / "buch"
    ordner.mkdir(exist_ok=True)
    kapitel = eintraege(con)
    buch_markdown(kapitel, ordner / "forschungsbuch.md")
    buch_html(kapitel, ordner / "forschungsbuch.html")
    buch_pdf(kapitel, ordner / "forschungsbuch.pdf")
    status_schreiben(con, ordner / "status.md")
    print(f"Buch aktualisiert: {ordner / 'forschungsbuch.pdf'}  ({sum(map(len, kapitel.values()))} Einträge)")


def status_schreiben(con: sqlite3.Connection, ziel: Path) -> None:
    z = [f"# Verarbeitungsstand ({datetime.now():%d.%m.%Y %H:%M})\n"]
    for r in con.execute("SELECT status, COUNT(*) n, MIN(datum) von, MAX(datum) bis FROM folgen GROUP BY status"):
        z.append(f"- **{r['status']}**: {r['n']} Sendungen ({(r['von'] or '')[:10]} bis {(r['bis'] or '')[:10]})")
    n = con.execute("SELECT COUNT(*) FROM themen").fetchone()[0]
    z.append(f"- **Themen im Buch**: {n}\n")
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
    buch_bauen(con)


if __name__ == "__main__":
    main()
