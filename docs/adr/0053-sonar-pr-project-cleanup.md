# ADR 0053: Sonar-PR-Projekte nach Merge/Close aufräumen

**Status:** Akzeptiert (2026-11-13)
**Herkunft:** Follow-up zu PR #348 (Sonar-Quality-Report im PR-Review).

## Kontext

Bis zur nativen PR-Analyse (Community-Branch-Plugin, PR #524) legte
`scripts/sonar-scan.sh --pr <n>` pro PR ein eigenes SonarQube-Projekt
`<org>:fabrik:pr-<n>` an, weil die Community Edition damals keine
PR-Analyse konnte. Nach Merge oder Close des PRs wurde das Projekt nie entfernt
und sammelte sich in SonarQube an (ein Projekt pro historischer PR). Seit #524
entstehen **keine** `:pr-<n>`-Projekte mehr — der Bestand an Legacy-Projekten
ist statisch und wird nur noch abgebaut.

## Entscheidung

1. **Python-Helfer `scripts/sonar-pr-cleanup.py`** (Muster: `scripts/pr-status.py`),
   nicht Shell: Paginierung, JSON-Parsing und Fail-safe-Logik sind in Python
   robuster und testbar.
   - Listet alle Sonar-Projekte per `api/projects/search` (paginiert, 500/Seite).
   - Filtert Keys des Musters `<base>:pr-<n>` (exakt; main-Key und fremde Keys
     bleiben unangetastet).
   - PR-Status via `gh api repos/<repo>/pulls/<n>` (konsistent mit den anderen
     Scripts); `state: open` → behalten, `closed` (gemergt oder nicht) → löschen
     per `api/projects/delete`.
   - `--dry-run` listet, was gelöscht würde.
2. **Fail-safe:** Ist der PR-Status nicht ermittelbar (GitHub nicht erreichbar,
   404, kaputtes JSON), wird das Projekt **nicht** gelöscht. Jeder Fehlschlag
   (Sonar- oder GitHub-API, abgelehntes Löschen) ist nur eine Warnung; das
   Script endet immer mit Exit 0.
3. **Aufruf im täglichen main-Scan:** `sonar-scan.sh` ruft das Script nach
   Quality-Gate-OK auf (Step 7) — nur im main-Scan (`SONAR_PR` leer), nicht bei
   PR-Scans (Loop `sonar-pr`). Advisory: ein Fehlschlag bricht den Scan nicht
   ab und ändert seinen Exit-Code nicht (zusätzlich `if !`-gewährte Warnung im
   Shell-Skript). Der tägliche Aufruf bleibt als sich selbst beendende
   Legacy-Aufräumung: solange keine `:pr-<n>`-Projekte mehr existieren, kostet
   der Lauf nur eine paginierte Projektliste (keine GitHub-Calls, keine
   Löschungen).

## Konsequenzen

- Pro Tageslauf: 1 paginierte Sonar-Liste + 1 GitHub-Call pro verbleibendem
  PR-Projekt (negligible Rate-Limit-Last; nach vollständiger Aufräumung nur
  noch die Liste).
- Sonar-Projekte offener PRs überleben den Lauf; ein PR, der nach dem Lauf
  wieder geöffnet wird, hat sein Projekt noch (Fail-safe wirkt in beide
  Richtungen: unbestimmt → behalten).
- Das Script ist standalone lauffähig (`--dry-run` für manuelle Prüfung) und
  damit auch als Einmal-Befehl einsetzbar, falls der tägliche Aufruf später
  entfernt wird.
