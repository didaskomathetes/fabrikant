Du führst ein gezieltes Refactoring im Projekt `fabrikant` (Repo `didaskomathetes/fabrikant`) durch.

## Vorbereitung (Pflicht)
1. Halte dich an ALLE Regeln aus `AGENTS.md` (liegt bereits im Kontext), v. a.
   Boy-Scout-Regel: Codebase hinterlassen sauberer als vorgefunden, aber Scope begrenzen.
2. Lies die betroffene(n) Datei(en) und deren Tests in `tests/`.

## Target
{target}

## Regeln für dieses Refactoring
- **Verhalten bleibt zu 100% erhalten.** Refactoring ≠ Feature-Arbeit. Wenn du merkest, dass
  du Features ändern willst: STOPP, nur Struktur/Logik-Klärung, keine neuen Fähigkeiten.
- Ändere nur Dateien in diesem Verzeichnis (dem `fabrikant/`-Paket des Repos).
- Vorher und nachher müssen diese Commands grün sein:
  - `uv run pytest -m "not browser"`
  - `uv run ruff format src/ tests/ scripts/`
  - `bash scripts/lint.sh` (ruff, pyright, bandit)
- Ziel: Komplexität reduzieren, Duplikat entfernen, Namesclarity, Doku ergänzen — so weit es
  den Target betrifft.
- Commit und Push übernimmt der Fabrik-`git`-Step — du musst NICHT committen oder pushen.
- Antworte am Ende in 2-3 Sätzen, was refactored wurde und warum.
