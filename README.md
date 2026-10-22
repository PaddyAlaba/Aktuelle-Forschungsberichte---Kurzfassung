# Forschungsbuch – Kurzfassungen aus „Forschung aktuell“

Ein GitHub-Workflow verarbeitet jeden Abend die neuen Beiträge des Deutschlandfunk-Podcasts
„Forschung aktuell“: Audio laden, lokal mit Whisper transkribieren, mit einem KI-Modell über GitHub Models zusammenfassen (kostenlos, kein API-Schlüssel nötig) und
nach Fachgebiet einsortieren. Daraus entsteht ein wachsendes Buch:

- **`buch/forschungsbuch.pdf`** – Format A5, gut lesbar auf dem Handy, mit Lesezeichen je Kapitel
- `buch/forschungsbuch.html` – durchsuchbare Fassung
- `buch/forschungsbuch.md` – Textfassung

Jeder Eintrag enthält Kernaussage, Zusammenfassung, Schlagworte und einen Link zum Originalbeitrag.

## Einrichtung (einmalig)

Kein API-Schlüssel nötig: Der Workflow nutzt GitHub Models mit dem eingebauten Token.

1. **Rückwirkend für 2026 starten:** *Actions → Forschungsbuch aktualisieren → Run workflow*
   - `ab`: `2026-01-01`
   - `limit`: `0`

   GitHub Models hat ein kostenloses Tageslimit (laut Nutzerberichten ca. 150 Anfragen/Tag).
   Ist es erreicht, speichert der Lauf den Zwischenstand und der tägliche Lauf macht am nächsten
   Abend weiter. Das Nachholen von 2026 verteilt sich so auf etwa eine Woche – ohne dein Zutun.
2. Danach läuft alles automatisch jeden Abend um 19:17 UTC.

## Lesen auf dem Handy

GitHub-App oder Browser → Repo → `buch/forschungsbuch.pdf` → herunterladen bzw. „Raw“.
Direktlink zum aktuellen Stand:
`https://github.com/PaddyAlaba/Aktuelle-Forschungsberichte---Kurzfassung/raw/main/buch/forschungsbuch.pdf`

## Einstellungen

`config.toml`: Feed-URL (leer = automatisch suchen), Whisper-Modell, KI-Anbieter und ob
Transkripte gespeichert werden (Standard: nein).

Alternativ zu GitHub Models: `anbieter = "claude"` (Secret `ANTHROPIC_API_KEY` anlegen) oder lokal
`anbieter = "ollama"`.

## Lokal ausführen

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=...   # Personal Access Token mit Berechtigung „Models: read“
python forschungsbuch.py --liste                     # Folgen im Feed + Status
python forschungsbuch.py                             # neue Folgen (max. 10)
python forschungsbuch.py --ab 2026-01-01 --limit 0   # rückwirkend
python forschungsbuch.py --nur-buch                  # nur Buch neu bauen
```

## Hinweise

- Der Podcast-Feed enthält nicht unbegrenzt alte Folgen. Das Programm meldet beim Start, wie weit er
  zurückreicht.
- Inhalte: © Deutschlandradio. Dieses Projekt speichert nur eigene, maschinell erstellte
  Kurzfassungen mit Quellenlink, keine Sendungstexte oder Audiodateien. Für den privaten Gebrauch.
