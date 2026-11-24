"""Guards für den CI-Workflow (docs/ci.md): keine Secret-/state-Leaks."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT  # Standalone-Repo: Repo-Root == Paket-Root; GitHub lädt nur <repo-root>/.github/workflows
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fabrikant-ci.yml"


def _steps(wf: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for job in wf["jobs"].values():
        yield from job["steps"]


def _load() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_state_not_uploaded_as_artifact():
    uploads = [s for s in _steps(_load()) if "upload-artifact" in s.get("uses", "")]
    for step in uploads:
        path = str(step.get("with", {}).get("path", ""))
        assert "state" not in path
        assert path.strip() not in {".", "./", ""}


def test_sonar_password_not_exposed_to_pr_code():
    """Browser-Tests führen PR-Code aus: kein Secret in deren Umgebung."""
    text = WORKFLOW.read_text()
    assert "secrets.SONAR_ADMIN_PASSWORD" not in text
    assert "echo $SONAR_ADMIN_PASSWORD" not in text


def test_playwright_installed_with_deps_before_browser_tests():
    runs = [s.get("run", "") for s in _steps(_load())]
    install = next(i for i, r in enumerate(runs) if "playwright install" in r)
    browser = next(i for i, r in enumerate(runs) if "-m browser" in r)
    assert "--with-deps" in runs[install]
    assert install < browser


def test_default_job_excludes_browser_tests():
    assert any('-m "not browser' in s.get("run", "") for s in _steps(_load()))


def test_perf_tests_run_serially():
    """Wall-Clock-Budgets nicht unter xdist-CPU-Konkurrenz: parallel ohne perf, perf ohne -n."""
    runs = [s.get("run", "") for s in _steps(_load())]
    parallel = [r for r in runs if "pytest" in r and " -n " in r]
    assert parallel and all("not perf" in r for r in parallel)
    assert any("-m perf" in r and " -n " not in r for r in runs)


def test_state_and_token_gitignored():
    assert "state/" in (ROOT / ".gitignore").read_text().split()


def test_workflow_at_repo_root_where_github_discovers_it():
    assert WORKFLOW.is_file()
    assert REPO_ROOT == ROOT  # Workflow liegt direkt an der Repo-Root (kein Monorepo-Unterverzeichnis)


def test_quality_gates_present():
    """Coverage-Gate, JS-Lint und Dependency-Audit laufen in CI (AGENTS.md)."""
    runs = [s.get("run", "") for s in _steps(_load())]
    assert any("--cov=fabrikant" in r and "not browser" in r for r in runs)
    assert any("biome" in r and "lint" in r for r in runs)
    assert any("pip-audit" in r for r in runs)
    assert "fail_under" in (ROOT / "pyproject.toml").read_text()
