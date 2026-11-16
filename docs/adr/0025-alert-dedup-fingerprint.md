# ADR 0025: Alert-Deduplizierung per Fingerprint (statt Issue pro Run)

**Status:** Accepted (2026-10-19). Nummerierung: ursprünglich 0013, dann 0014, seit 2026-10-20
0025 (ADR-Nummern-Bereinigung: 0013/0014 waren doppelt belegt).
**Entscheider:** Dominic
**Code-PR:** [#125](https://github.com/didaskomathetes/quark-playground/pull/125)
(Implementierung, Tests, Config und dieses ADR)
**Kontext:** `_notify` (Kanal `github_issue`) öffnete bei **jedem** fehlgeschlagenen
Run ein neues GitHub-Issue. Der Title enthielt die Run-ID, daher waren keine zwei
Alerts identisch. Ein persistenter Fehler im 5-Minuten-`review-pr`-Cron (Pipe-Bug,
PR #124) produzierte 22 Alert-Issues (#102–#123) für denselben Fehler — eine
Alert-Flut, die das eigentliche Signal begrub und manuelles Aufräumen erforderte.

## Entscheidung

Alerts werden über einen stabilen **Fingerprint** des Fehlerbilds dedupliziert:

- **Fingerprint** = SHA-256 über `project/loop` + erster fehlgeschlagener Step +
  normalisierter Fehlertext, gekürzt auf 16 Hex-Zeichen (64 Bit).
- **Normalisierung** entfernt lauf-spezifische Tokens: UUIDs (8-4-4-4-12) und
  Hex-Tokens ≥ 8 Zeichen (Run-/Session-IDs, Hashes). Kurze, bedeutungsvolle
  Zahlen („1 error(s)“, „port 8080“) bleiben erhalten, damit sich unterschiedliche
  Fehlerbilder nicht zusammenziehen.
- Der Fingerprint liegt als unsichtbarer Marker im Issue-Body
  (`<!-- fabrik-alert:fp=… -->`).
- **Offenes Issue mit gleichem Fingerprint** → der Run wird dort kommentiert
  (Kanal-Eintrag `issue#N(+)`), es wird kein neues Issue angelegt.
- **Geschlossene Issues zählen nicht:** Tritt der Fehler nach dem Schließen erneut
  auf, wird ein neues Issue geöffnet (Alert eskaliert neu).
- **Lookup-Fehler** → das Issue wird trotzdem angelegt (Fail-safe: lieber ein
  Duplikat als ein verlorener Alert).
- **Lookup paginiert** über alle offenen Alert-Issues (Seitengröße
  `config.ALERT_OPEN_ISSUES_PAGE` = 100) bis eine Seite unvollständig ist, damit
  auch bei > 100 offenen Issues kein relevantes Issue übersehen wird.

## Betriebliche Trade-offs

- **Kollisionsrisiko:** 64-Bit-Fingerprint. Per Geburtstagsparadoxon wären
  ~4 Mrd. *unterschiedliche* Fehlerbilder pro Projekt/Loop nötig, bis die
  Kollisionswahrscheinlichkeit 50 % erreicht — bei der realen Anzahl an
  Fehlerbildern vernachlässigbar. Eine Kollision würde zwei *unterschiedliche*
  Fehler in ein Issue zusammenführen (kein verlorener Alert).
- **Secrets im log_tail:** Die letzten 15 Log-Zeilen landen ohne Redaction im
  Issue-Body/in Kommentaren; Logs können Tokens/Keys enthalten. Dieser ADR
  enthält bewusst keine Log-Beispiele. Minderung (explizite Risikoakzeptanz,
  bis Redaction umgesetzt ist):
  1. **Ops-only Sichtbarkeit:** Alert-Issues werden nur in privaten
     Fabrik-Projekt-Repos geöffnet (z. B. `didaskomathetes/quark-playground`);
     Zugriff = Repo-ACL (Fabrik-Operatoren, kein breites Team, kein
     öffentliches Repo). Keine zusätzlichen GitHub-Repo-Settings nötig,
     solange das Repo privat bleibt; für neue Repos gilt dieselbe
     Voraussetzung (privat + Operatoren-ACL).
  2. **Risikoakzeptanz:** Bewusst akzeptiert, dass Logs Tokens/Keys enthalten
     können; Owner: Dominic (Entscheider, oben).
  3. **Folge-Issue mit Deadline:** Redaction-Layer für `log_tail` vor dem
     Issue-Post ist als
     [#132](https://github.com/didaskomathetes/quark-playground/issues/132)
     angelegt (Pattern-basiert, konfigurierbar, Fail-safe). **Deadline:
     Umsetzung innerhalb von 2 Wochen nach Merge dieses ADR (bis 2026-10-31);
     Owner: Dominic.** Bis dahin gilt die Risikoakzeptanz aus Punkt 2.
- **Rate-Limits:** Der Lookup liest alle offenen Alert-Issues (typisch 1–3).
  Problematisch nur, wenn Alerts nie geschlossen werden.
- **Rollback:** PR #125 wird per Squash gemerged → Rollback =
  `git revert <squash-SHA>` (ein Commit). Betroffene Dateien:
  `fabrik/src/fabrik/steps/notify.py`, `fabrik/src/fabrik/config.py`
  (`ALERT_OPEN_ISSUES_PAGE`), `fabrik/tests/test_steps_notify.py`,
  `fabrik/README.md`. Verifikation: `uv run pytest -m "not browser"` grün;
  Alt-Issues ohne Marker werden einfach nicht gematcht — keine Datenmigration
  nötig.

## Abgelehnte Alternativen

- **Dedup über den Title:** Der Title enthält den Fehlertext, variiert aber pro
  Run (IDs, Zahlen) — ohne Normalisierung dasselbe Problem wie zuvor.
- **Zeitbasiertes Dedup (z. B. 24 h):** Zeit- statt Fehler-basiert — ein
  persistenter Fehler würde alle 24 h ein neues Issue öffnen.
- **Kein Dedup (Status quo):** Alert-Flut (beobachtet: 22 Issues für einen Fehler).

## Akzeptanzkriterien

- **Tests** (in PR #125, `tests/test_steps_notify.py`): Fingerprint-Stabilität
  (gleiche Fehler → gleicher Fingerprint, Run-/Session-IDs irrelevant),
  Dedup (offenes Issue → Kommentar, kein zweites Issue), Paginierung
  (> 1 Seite), Lookup-Fallback (API-Fehler → Issue trotzdem),
  Title-Sanitization, Max-Pages-Abort.
- **Metrik:** Kein Fehlerbild erzeugt > 1 offenes Alert-Issue; wiederkehrende
  Runs erhöhen die Kommentar-Zahl des bestehenden Issues.
- **Rollback-Test:** Revert des notify.py-Commits lässt Alt-Issues ohne Marker
  unverändert (kein Match, keine Migration).
- **Coverage:** 91 % Gesamt (Ziel ≥ 80 %), `notify.py` 100 % — gemessen
  2026-10-19 auf Branch `fabrik/adr-125-alert-dedup` mit
  `uv run pytest --cov -m "not browser"`.

## Folgen

- Ein Issue pro Fehlerbild (statt pro Run); wiederkehrende Runs werden
  kommentiert.
- Tests in `tests/test_steps_notify.py` (Fingerprint-Stabilität, Dedup,
  Paginierung, Lookup-Fallback, Title-Sanitization) — geliefert in
  [PR #125](https://github.com/didaskomathetes/quark-playground/pull/125),
  nicht in diesem ADR-PR.
- `ALERT_OPEN_ISSUES_PAGE` (Seitengröße des Lookups, Default 100) —
  Definition: `ALERT_OPEN_ISSUES_PAGE = 100` in
  `fabrik/src/fabrik/config.py`, eingeführt in
  [PR #125](https://github.com/didaskomathetes/quark-playground/pull/125)
  (Commit 75c0cbf, auf dem PR-Branch vorhanden). Dieser ADR-PR (Base =
  PR-Branch #125) sieht sie nach dem Merge des Stapels.
