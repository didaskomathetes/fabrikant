# Development

`uv` project (lock file: `uv.lock`); pytest, ruff, pyright and bandit as the dev group. Python 3.11+ (CI runs
3.12). Contribution rules: [`AGENTS.md`](../AGENTS.md).

```bash
uv sync                 # venv + dependencies (runtime + dev)
uv run pytest           # unit/integration tests
uv run pytest -m "not browser and not perf" -n auto   # parallel (pytest-xdist), much faster
uv run ruff format .    # format
bash scripts/lint.sh    # gate: ruff check + format --check, pyright (types), bandit (SAST, medium and up), spec-reference up to date
```

`scripts/lint.sh` is the single source for CI, the develop/refactor `lint_cmd` and the Sonar reports. The spec
reference is regenerated with `python scripts/generate-spec-reference.py` (check mode: `--check`).

**Browser tests (UI):** `tests/test_ui_browser.py` starts an isolated fabrikant server + Chromium (Playwright)
and checks the interactive UI behavior (scrollbars, run modal, clickable loop specs, chat overlay). Once:
`uv run playwright install chromium`. To skip: `uv run pytest -m "not browser"`.

**CI:** see [ci.md](ci.md) (`.github/workflows/fabrikant-ci.yml`, PRs + pushes to `main`; jobs `test` and
`browser`; coverage gate `fail_under = 90`). Releases: [releasing.md](releasing.md). Code quality scans:
[sonarqube.md](sonarqube.md).
