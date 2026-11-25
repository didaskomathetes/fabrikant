# CI

Workflow: `.github/workflows/fabrikant-ci.yml` (GitHub only reads workflows from there). Jobs: `test`, `browser`,
`package`; tag releases use `release.yml` ([releasing.md](releasing.md)). It runs on pull requests and pushes to `main`
with Python 3.12, a uv cache keyed on `uv.lock`, actions pinned by commit SHA and `contents: read` permissions.
Jobs run in the repository root (no `working-directory`).

## Jobs

- `test`: `uv sync --frozen`, then JS lint (Biome, `biome.json`, only `src/fabrikant/web/app.js`; errors block,
  warnings don't) and a dependency audit (`pip-audit` against the locked environment), then `scripts/lint.sh`
  (ruff check + format, pyright, bandit from severity medium), `fabrikant validate --offline projects/*.fabrikant.yaml`
  (specs incl. vars/templates, without the host-dependent `root` check) and
  `pytest -m "not browser and not perf" -n auto` (parallel via pytest-xdist, with `--cov=fabrikant`; coverage gate
  `fail_under = 90` in `pyproject.toml`). Finally `pytest -m perf` runs serially, because wall-clock budgets don't
  tolerate CPU contention from parallel workers.
- `browser`: `playwright install --with-deps chromium` (browser **and** system libraries), then
  `pytest -m browser`. Blocking (no `continue-on-error`).
- `package`: builds the wheel and runs the clean-venv smoke test (`scripts/smoke-wheel.sh`).

## No leaks through artifacts

`state/` (tokens, SQLite logs) must never be uploaded. The workflow uploads no artifacts; if
`actions/upload-artifact` is added later, list explicit paths only (e.g. `coverage.xml`), never `.` or `state/`.
`tests/test_ci_workflow.py` enforces this.
