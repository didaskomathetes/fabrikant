# ADR 0068: Golden Runs — Run-Store-Markierung + `eval --golden`

**Status:** Draft; proposed on the `feat/golden-runs-store` branch
**Betrifft:** ADR 0065 (Golden Runs, `fabrikant eval`), ADR 0034 (run store, JSON + SQL), `docs/roadmap.md` First win 2 (#130)

## Context

ADR 0065 gibt uns die Golden-Baseline und den Eval: `fabrikant eval <project>
<loop> [--name N]`. Die drei Parameter muss werfend manuell kennen. In der
Praxis weiß man aber meist nur *welcher Run* der bekannte Gute war — „der
Run, den wir letzte Woche abgenickt haben" — und will darauf hin die
Eval laufen lassen, nicht Project/Loop/Name rekonstruieren.

Die Lücke (#130, Gaps 1+2): Der Run-Store (ADR 0034) weiß nicht, ob ein Run
als Golden-Baseline markiert ist. Die Markierung lebt nur in der
Sidecar-Datei `projects/<projekt>.golden.json` (`baseline_run`), die neben
der Spec liegt und nichts mit dem Run-Record selbst zu tun hat.

## Decision

**Gap 1 — Markierung im Run-Store.** Der Run-Record bekommt ein Feld
`golden` (Text, nullable): den *Namen* der Golden-Baseline, aus der dieser
Run als Baseline gefangen wurde.

- `Run.golden: str = ""` im Modell (`runmodel.py`), in `parse_run` (JSON) und
  in `_RUN_FIELDS` + dem deklarativen `runs`-Table (`runschema.py`) +
  Alembic-Migration `0007` (SQL).
- `save_golden(project, name, golden)` markiert **best-effort** den
  `baseline_run`: `mark_baseline(project, run_id, name)` setzt
  `golden = name` auf dem Run-Record. Fehlender Run (z. B. CI mit leeren
  `runs/`) ist kein Fehler — die markierung wird übersprungen, die
  Sidecar-Datei bleibt die committed Review-Quelle.
- Die Sidecar-Datei `projects/<projekt>.golden.json` bleibt unverändert die
  *Quell- und Review-Quelle*. `golden` im Store ist nur eine *Beziehung*
  (Run → Baseline-Name), keine zweite Kopie der Baseline.

**Gap 2 — `eval --golden <run-id>`.** Die Eval lässt sich jetzt über den Run
starten, dessen Markierung Project/Loop/Name auflöst:

- `golden_for_run(run_id)` → `(project, name)` oder `None` (unbekannt oder
  nicht markiert).
- `fabrikant eval --golden <run-id> [--yes]` — löst über die Markierung auf und
  läuft dann exakt wie `fabrikant eval <project> <loop> [--name N]`. Die
  klassischen Positional-Argumente werden optional (`nargs="?"`); ohne
  `--golden` und ohne `project loop` → Usage-Error.
- Exit-Contract unverändert: **0 = keine Regression**, 1 = Regression, 2 =
  Fehler/inkonklusiv.

**Gap 3 — CI-Workflow.** Bleibt wie in ADR 0065: das Workflow-File liegt
staged unter `docs/ci/golden-eval.yml` und wird erst nach Freigabe der
`workflows`-Write-Permission der contributing App nach
`.github/workflows/` gezogen. Von der Code-Seite nicht machbar — hier nicht
weiterverfolgt.

**Gap 4 — Nutzer-Doku.** `docs/golden-evals.md` dokumentiert den kompletten
Workflow (capture → mark → eval, beide CLI-Formen, Exit-Codes, CI-Opt-in).

## Consequences

- Ein bekannter Run ist jetzt direkt eval-ierbar (`fabrikant eval --golden
  <run-id>`) — Project/Loop/Name sind aus dem Store auflösbar, nicht aus
  dem Kopf.
- Die Markierung ist *best-effort* und *abgeleitet*: sie fehlt in
  Umgebungen ohne Run-Store (reine CI-Runners ohne `runs/`) und bricht
  nichts. Die committed Sidecar-Datei bleibt die Single Source of Truth.
- `golden` im Store ist ein Text (Name), kein Boolean — mehrere Runs können
  denselben Baseline-Namen tragen, und der Name adressiert die konkrete
  Baseline in der Sidecar-Datei.
- Migration `0007` ist additiv (`ADD COLUMN golden TEXT`, nullable) —
  bestehende DBs werden ohne Datenverlust upgraded; die `compare_metadata`-
  Prüfung (Schema ↔ `runschema`) bleibt grün, weil das deklarativen Table-
  Objekt und die Migration zusammen wachsen.
