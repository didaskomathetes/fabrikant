"""scripts/ci-matrix-pytest.py: Matrix aus dem Workflow lesen, betroffene Pakete wählen."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import load_script

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci-matrix-pytest.py"

WORKFLOW = {
    "jobs": {
        "python-test": {
            "strategy": {
                "matrix": {
                    "include": [
                        {"package": "rag", "test_home": "src/load_pipeline/user_docs"},
                        {"package": "rag", "test_home": "src/load_pipeline/common"},
                        {"package": "mcp-wrapper", "test_home": "src/mcp/wrapper"},
                        {"package": "admin-links", "test_home": "libs/admin-links"},
                    ]
                }
            },
            "steps": [
                {"uses": "actions/checkout@v7"},
                {
                    "name": "Run pytest",
                    "env": {"PYTHONPATH": ".:..", "WS": "${{ github.workspace }}"},
                    "run": "python -m pytest -q",
                },
            ],
        }
    }
}


@pytest.fixture
def mod():
    return load_script(SCRIPT.name, "ci_matrix_pytest")


@pytest.fixture
def matrix(mod):
    return mod.TestMatrix.from_workflow(WORKFLOW, "python-test")


def test_from_workflow_reads_entries_and_local_env(matrix):
    assert [e.test_home for e in matrix.entries][:2] == ["src/load_pipeline/user_docs", "src/load_pipeline/common"]
    assert matrix.env == {"PYTHONPATH": ".:.."}  # GitHub-Expressions sind lokal nicht auflösbar


@pytest.mark.parametrize(
    ("workflow", "message"),
    [({"jobs": {}}, "fehlt"), ({"jobs": {"python-test": {"steps": []}}}, "keine matrix")],
)
def test_from_workflow_rejects_missing_job_or_matrix(mod, workflow, message):
    with pytest.raises(ValueError, match=message):
        mod.TestMatrix.from_workflow(workflow, "python-test")


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ([], []),
        (["README.md", "infrastructure/main.tf"], []),
        (["src/mcp/server.py"], ["src/mcp/wrapper"]),
        # Komponente = zwei Segmente: Load-Pipeline-Änderung trifft alle Load-Pipeline-Einträge
        (["src/load_pipeline/common/x.py"], ["src/load_pipeline/user_docs", "src/load_pipeline/common"]),
        (["src/mcpx/a.py"], []),  # Präfix ohne Pfadgrenze ist keine Komponente
    ],
)
def test_affected_by_component(matrix, changed, expected):
    assert [e.test_home for e in matrix.affected(changed)] == expected


@pytest.mark.parametrize("shared", ["uv.lock", "pyproject.toml", "libs/sync-validation/x.py"])
def test_shared_paths_select_whole_matrix(matrix, shared):
    assert len(matrix.affected([shared])) == len(matrix.entries)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    wf = tmp_path / ".github" / "workflows" / "python.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(yaml.safe_dump(WORKFLOW))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_changed_paths_includes_committed_unstaged_and_untracked(mod, repo):
    (repo / "committed.py").write_text("")
    _git(repo, "add", "committed.py")
    _git(repo, "commit", "-q", "-m", "c")
    (repo / ".github" / "workflows" / "python.yml").write_text("changed: true\n")
    (repo / "new.py").write_text("")
    assert mod.changed_paths("main", repo) == [".github/workflows/python.yml", "committed.py", "new.py"]


def test_main_nothing_affected(mod, repo, capsys):
    (repo / "README.md").write_text("doc")
    assert mod.main(["--root", str(repo), "--base", "main"]) == 0
    assert "nichts zu tun" in capsys.readouterr().out


def test_main_dry_run_lists_selection(mod, repo, capsys):
    (repo / "src" / "mcp").mkdir(parents=True)
    (repo / "src" / "mcp" / "a.py").write_text("")
    assert mod.main(["--root", str(repo), "--base", "main", "--dry-run"]) == 0
    assert capsys.readouterr().out.strip() == "mcp-wrapper\tsrc/mcp/wrapper"


@pytest.mark.parametrize(("results", "code"), [(["pass"], 0), (["fail"], 1), (["crash", "crash"], 75)])
def test_main_runs_selected_entries(mod, repo, monkeypatch, results, code):
    (repo / "src" / "mcp").mkdir(parents=True)
    (repo / "src" / "mcp" / "a.py").write_text("")
    seen = []

    def fake_run(entry, env, root, cov_xml=None):
        seen.append((entry.test_home, env))
        return mod.Outcome(results.pop(0))

    monkeypatch.setattr(mod, "run_entry", fake_run)
    assert mod.main(["--root", str(repo), "--base", "main"]) == code
    assert all(s == ("src/mcp/wrapper", {"PYTHONPATH": ".:.."}) for s in seen)
    assert not results  # a crash runs exactly once more


def test_run_entry_stops_when_sync_fails(mod, monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert mod.run_entry(mod.MatrixEntry("p", "src/p"), {}, tmp_path) is mod.Outcome.FAIL
    assert len(calls) == 1 and calls[0][:2] == ["uv", "sync"]


def test_run_entry_runs_pytest_in_test_home_with_env(mod, monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert mod.run_entry(mod.MatrixEntry("p", "src/p"), {"X": "1"}, tmp_path) is mod.Outcome.PASS
    cmd, kw = calls[1]
    assert cmd[1:3] == ["-m", "pytest"] and kw["cwd"] == tmp_path / "src/p" and kw["env"]["X"] == "1"
    assert "--no-editable" in calls[0][0]  # ohne Coverage wie CI
    assert not any(a.startswith("--cov") for a in cmd)


def test_run_entry_with_coverage_syncs_editable_and_writes_xml(mod, monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mod.subprocess, "run", fake)
    xml = tmp_path / "cov" / "coverage-0.xml"
    assert mod.run_entry(mod.MatrixEntry("p", "src/p/tests"), {}, tmp_path, xml) is mod.Outcome.PASS
    sync, install, pytest_cmd = calls
    assert sync[:2] == ["uv", "sync"] and "--no-editable" not in sync  # Pfade auf die Quellen, nicht site-packages
    assert install[:3] == ["uv", "pip", "install"] and install[-1] == mod.COV_PACKAGE
    assert f"--cov={tmp_path / 'src/p'}" in pytest_cmd  # ganze Komponente, nicht nur test_home
    assert f"--cov-report=xml:{xml}" in pytest_cmd


def test_main_all_with_cov_dir_runs_whole_matrix(mod, repo, monkeypatch, tmp_path):
    (repo / "README.md").write_text("doc")  # nichts betroffen — --all läuft trotzdem alles
    seen = []

    def fake_run(entry, env, root, cov_xml=None):
        seen.append((entry.test_home, cov_xml))
        return mod.Outcome.PASS

    monkeypatch.setattr(mod, "run_entry", fake_run)
    cov = tmp_path / "cov"
    assert mod.main(["--root", str(repo), "--all", "--cov-dir", str(cov)]) == 0
    assert seen and [c for _, c in seen] == [cov / f"coverage-{i}.xml" for i in range(len(seen))]
    assert cov.is_dir()


def test_main_relative_cov_dir_is_made_absolute(mod, repo, monkeypatch, tmp_path):
    """Regression: pytest läuft im test_home — ein relativer --cov-dir schrieb die XMLs dorthin."""
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(
        mod, "run_entry", lambda entry, env, root, cov_xml=None: seen.append(cov_xml) or mod.Outcome.PASS
    )
    assert mod.main(["--root", str(repo), "--all", "--cov-dir", ".sonar-cov"]) == 0
    assert seen and all(c.is_absolute() and c.parent == tmp_path / ".sonar-cov" for c in seen)


@pytest.mark.parametrize(
    ("pytest_code", "outcome"),
    [(0, "pass"), (1, "fail"), (2, "fail"), (-11, "crash"), (139, "crash"), (128, "fail")],
)
def test_run_entry_tells_red_tests_from_crashes(mod, monkeypatch, tmp_path, pytest_code, outcome):
    """Regression: a SIGSEGV in the sandbox is a crash, not a red test result."""
    codes = iter([0, pytest_code])  # uv sync, pytest
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, next(codes)))
    assert mod.run_entry(mod.MatrixEntry("p", "src/p"), {}, tmp_path) is mod.Outcome(outcome)


@pytest.mark.parametrize(
    ("runs", "expected", "calls"),
    [
        (["crash", "pass"], "pass", 2),
        (["crash", "crash"], "crash", 2),
        (["fail"], "fail", 1),
        (["crash", "fail"], "fail", 2),
    ],
)
def test_run_entry_with_rerun_retries_only_crashes(mod, monkeypatch, tmp_path, capsys, runs, expected, calls):
    seen = []

    def fake_run(entry, env, root, cov_xml=None):
        seen.append(entry)
        return mod.Outcome(runs[len(seen) - 1])

    monkeypatch.setattr(mod, "run_entry", fake_run)
    assert mod.run_entry_with_rerun(mod.MatrixEntry("p", "src/p"), {}, tmp_path) is mod.Outcome(expected)
    assert len(seen) == calls
    assert ("abgestürzt" in capsys.readouterr().out) is (runs[0] == "crash")


@pytest.mark.parametrize(
    ("outcomes", "code", "stderr"),
    [
        ([("a", "pass"), ("b", "pass")], 0, ""),
        ([("a", "crash"), ("b", "pass")], 75, "abgestürzt: a"),
        ([("a", "crash"), ("b", "fail")], 1, "rot: b"),  # a real red result beats the crash
    ],
)
def test_exit_status(mod, capsys, outcomes, code, stderr):
    assert mod.exit_status([(home, mod.Outcome(o)) for home, o in outcomes]) == code
    assert stderr in capsys.readouterr().err
