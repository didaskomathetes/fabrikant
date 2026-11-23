# CI

Workflow: `<repo-root>/.github/workflows/fabrik-ci.yml` (GitHub lädt nur dort) (Jobs `test`, `browser`).
Läuft bei Pull Requests und Pushes auf `main`; Python 3.12, uv-Cache über `uv.lock`,
Action-Versionen per Commit-SHA gepinnt, Permissions `contents: read`.
Die Jobs laufen direkt im Repository-Root (Standalone-Layout, kein
`working-directory`).

## Jobs

- `test`: `uv sync --frozen`, `scripts/lint.sh` (ruff check + format, pyright, bandit ab Severity medium), `fabrik validate --offline projects/*.fabrik.yaml` (Specs inkl. vars/Templates; ohne host-abhängige root-Prüfung), `pytest -m "not browser and not perf" -n auto`
  (parallel via pytest-xdist, mit `--cov=fabrik`: Coverage-Gate `fail_under = 90` in `pyproject.toml`),
  davor JS-Lint (Biome, `biome.json`, nur `web/app.js`; Fehler blockieren, Warnungen nicht) und
  Dependency-Audit (`pip-audit` gegen die Lock-Umgebung); danach `pytest -m perf` seriell (Wall-Clock-Budgets
  vertragen keine CPU-Konkurrenz durch parallele Worker).
- `browser`: `playwright install --with-deps chromium` (Browser **und**
  System-Libs), dann `pytest -m browser`. Blockierend (kein `continue-on-error`).

## Secrets

- `SONAR_ADMIN_PASSWORD` wird **nur** als GitHub-Actions-Secret
  (Settings → Secrets and variables → Actions) gesetzt und per
  `${{ secrets.… }}` an einen vertrauenswürdigen Step übergeben — nie im Repo,
  nie per `echo`/`set -x`, nie an Jobs, die PR-Code ausführen (z. B. Browser-Tests).
  GitHub maskiert den Wert in Logs. Lokal steht er in der gitignorierten
  `fabrik.env`.
- `scripts/sonar-init.py` schreibt den Token nach `state/sonar-token.txt`
  mit Berechtigung `0600` (nur Owner). `state/` ist in `.gitignore`.

## Keine Leaks über Artefakte

`state/` (Token, SQLite-Logs) darf nie hochgeladen werden. Der Workflow lädt
keine Artefakte hoch; falls künftig `actions/upload-artifact` genutzt wird,
nur explizite Pfade (z. B. `coverage.xml`) angeben, nie `.` oder `state/`.
`tests/test_ci_workflow.py` sichert das ab.
