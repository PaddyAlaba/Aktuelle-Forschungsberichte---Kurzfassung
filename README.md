# Forschungsbuch – Kurzfassungen aus „Forschung aktuell“

Ein GitHub-Workflow verarbeitet jeden Abend die neuen Beiträge des Deutschlandfunk-Podcasts
„Forschung aktuell“: Audio laden, lokal mit Whisper transkribieren, mit einem offenen KI-Modell (Qwen 2.5 über Ollama, läuft direkt im Workflow) zusammenfassen – kostenlos, ohne API-Schlüssel und
nach Fachgebiet einsortieren. Daraus entsteht ein wachsendes Buch:

- **`buch/forschungsbuch.pdf`** – Format A5, gut lesbar auf dem Handy, mit Lesezeichen je Kapitel
- `buch/forschungsbuch.html` – durchsuchbare Fassung
- `buch/forschungsbuch.md` – Textfassung

Jeder Eintrag enthält Kernaussage, Zusammenfassung, Schlagworte und einen Link zum Originalbeitrag.

## Einrichtung (einmalig)

Kein API-Schlüssel nötig: Das KI-Modell läuft mit Ollama direkt auf dem GitHub-Server.

1. **Rückwirkend für 2026 starten:** *Actions → Forschungsbuch aktualisieren → Run workflow*
   - `ab`: `2026-01-01`
   - `limit`: `0`

   Ein Lauf arbeitet höchstens ~5 Stunden (Transkription + Zusammenfassung brauchen auf dem
   GitHub-Server zusammen etwa 10 Minuten pro Sendung) und speichert dann den Zwischenstand. Der
   tägliche Lauf macht automatisch weiter, das Nachholen dauert so einige Tage – ohne dein Zutun.
   Den Fortschritt zeigt `buch/status.md`.
2. Danach läuft alles automatisch jeden Abend um 19:17 UTC.

## Lesen auf dem Handy

GitHub-App oder Browser → Repo → `buch/forschungsbuch.pdf` → herunterladen bzw. „Raw“.
Direktlink zum aktuellen Stand:
`https://github.com/PaddyAlaba/Aktuelle-Forschungsberichte---Kurzfassung/raw/main/buch/forschungsbuch.pdf`

## Einstellungen

`config.toml`: Feed-URL (leer = automatisch suchen), Whisper-Modell, KI-Anbieter und ob
Transkripte gespeichert werden (Standard: nein).

Bessere, schnellere Zusammenfassungen gegen kleines Geld: `anbieter = "claude"` und das Secret
`ANTHROPIC_API_KEY` anlegen. (GitHub Models wurde von GitHub eingestellt.)

## Lokal ausführen

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:7b    # Ollama muss lokal installiert sein (ollama.com)
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
