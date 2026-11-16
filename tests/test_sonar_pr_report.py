"""scripts/sonar-pr-report.py — Diff-Parsing, Zeilenfilter, Rendering/Marker, diff-cover,
Kommentar-Upsert und CLI (Sonar-HTTP, git, gh und diff-cover gemockt)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sonar-pr-report.py"
KEY = "proj:fabrik:pr-7"
SHA = "c" * 40

DIFF = """diff --git a/fabrik/src/a.py b/fabrik/src/a.py
--- a/fabrik/src/a.py
+++ b/fabrik/src/a.py
@@ -3,0 +4,2 @@ def f():
+    x = 1
+    y = 2
@@ -10 +12 @@ def g():
-    old
+    new
diff --git a/fabrik/src/gone.py b/fabrik/src/gone.py
--- a/fabrik/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/fabrik/src/b.py b/fabrik/src/b.py
--- a/fabrik/src/b.py
+++ b/fabrik/src/b.py
@@ -5,2 +4,0 @@
-x
-y
"""


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sonar_pr_report", SCRIPT)
    assert spec is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m  # dataclasses lösen Annotationen über sys.modules auf
    spec.loader.exec_module(m)
    yield m
    sys.modules.pop(spec.name, None)


def _issue(path, line, severity="MAJOR", rule="python:S1", **extra):
    return {
        "component": f"{KEY}:{path}",
        "line": line,
        "severity": severity,
        "type": "CODE_SMELL",
        "rule": rule,
        "message": "msg",
        **extra,
    }


# ---------------------------------------------------------------- Diff
def test_parse_changed_lines(mod):
    assert mod.parse_changed_lines(DIFF) == {"fabrik/src/a.py": {4, 5, 12}}


# ---------------------------------------------------------------- Filter + Sortierung
def test_issues_filtered_to_changed_lines_and_sorted(mod):
    changed = {"fabrik/src/a.py": {4, 5, 12}}
    raw = [
        _issue("src/a.py", 12, "MINOR"),
        _issue("src/a.py", 5, "CRITICAL", rule="python:S2"),
        _issue("src/a.py", 4, "CRITICAL", rule="python:S1"),
        _issue("src/a.py", 7),  # unverändert
        _issue("src/b.py", 4),  # Datei ohne neue Zeilen
        {"component": f"{KEY}:src/a.py", "severity": "MAJOR"},  # Datei-Issue ohne Zeile
        {"component": "other:src/a.py", "line": 4},  # fremder Key
        _issue("src/a.py", 12, "WEIRD", message="a\nb"),
    ]
    issues = mod.issues_on_changed_lines(raw, KEY, "fabrik/", changed)
    assert [(i.severity, i.line, i.rule) for i in issues] == [
        ("CRITICAL", 4, "python:S1"),
        ("CRITICAL", 5, "python:S2"),
        ("MINOR", 12, "python:S1"),
        ("WEIRD", 12, "python:S1"),
    ]
    assert issues[-1].message == "a b"


# ---------------------------------------------------------------- Lint
def test_lint_issues_filtered_to_changed_lines_and_sorted(mod, monkeypatch):
    ruff_check = [
        {"filename": "src/a.py", "severity": "warning", "location": {"row": 5}, "code": "F401", "message": "unused"},
        # ruff meldet absolute Pfade → müssen auf den CWD-relativen Stand normalisiert werden
        {
            "filename": str(Path(__file__).resolve().parent.parent / "src" / "a.py"),
            "severity": "error",
            "location": {"row": 12},
            "code": "F841",
        },
        {"filename": "src/a.py", "severity": "error", "location": {"row": 7}, "code": "F821"},  # unverändert
        {"filename": "src/b.py", "severity": "error", "location": {"row": 4}, "code": "E302"},  # keine neuen Zeilen
    ]
    ruff_format = [{"filename": "src/a.py", "location": {"row": 4}, "message": "File would be reformatted"}]
    mypy = [  # JSON-Lines, wie mypy 2.x
        {
            "file": "src/a.py",
            "line": 12,
            "severity": "error",
            "code": "return-value",
            "message": "Incompatible\nreturn",
        },
    ]
    outputs = {
        ("ruff", "check", "--output-format", "json"): json.dumps(ruff_check),
        ("ruff", "format", "--check", "--output-format", "json"): json.dumps(ruff_format),
        ("mypy", "--output", "json", "src"): "\n".join(json.dumps(d) for d in mypy),
    }

    def _run(cmd):
        return outputs[tuple(cmd)]

    monkeypatch.setattr(mod, "_run", _run)
    issues = mod.lint_issues({"fabrik/src/a.py": {4, 5, 12}}, "fabrik/")
    assert [(i.severity, i.line, i.rule) for i in issues] == [
        ("error", 4, "ruff-format"),
        ("warning", 5, "ruff:F401"),
        ("error", 12, "mypy:return-value"),
        ("error", 12, "ruff:F841"),
    ]
    assert all(i.type == "LINT" and i.path == "fabrik/src/a.py" for i in issues)
    assert next(i for i in issues if i.rule == "mypy:return-value").message == "Incompatible return"


def test_lint_issues_tolerates_missing_or_broken_tools(mod, monkeypatch):
    monkeypatch.setattr(mod, "_run", lambda cmd: "" if cmd[0] == "ruff" else "not json")
    assert mod.lint_issues({"fabrik/src/a.py": {4}}, "fabrik/") == []


# ---------------------------------------------------------------- Rendering
def test_render_is_deterministic_and_ends_with_marker(mod):
    report = mod.QualityReport(
        pr=7,
        sha=SHA,
        measures={"coverage": "81.0", "bugs": "0", "ignored": "x"},
        issues=[mod.Issue("fabrik/src/a.py", 4, "MAJOR", "CODE_SMELL", "python:S1", "too complex")],
        lint_issues=[mod.Issue("fabrik/src/a.py", 5, "warning", "LINT", "ruff:F401", "unused")],
        diff_coverage=66.6,
        uncovered={"fabrik/src/b.py": [9], "fabrik/src/a.py": [5, 6]},
    )
    body = report.render()
    assert body == report.render()
    assert body.splitlines()[-1] == f"<!-- fabrik-quality sha={SHA} -->"
    assert mod.MARKER_RE.search(body).group(1) == SHA
    assert "Coverage geänderter Zeilen: **67 %**" in body
    assert body.index("fabrik/src/a.py` Zeilen 5, 6") < body.index("fabrik/src/b.py` Zeilen 9")
    assert "Kennzahlen (gesamter Stand): bugs=0, coverage=81.0" in body
    assert "[MAJOR/CODE_SMELL] `fabrik/src/a.py:4` too complex (python:S1)" in body
    assert "- Lint (ruff + mypy) auf geänderten Zeilen: **1**" in body
    assert "[warning/LINT] `fabrik/src/a.py:5` unused (ruff:F401)" in body


def test_render_without_data_and_with_overflow(mod):
    empty = mod.QualityReport(pr=1, sha=SHA).render()
    assert "**–**" in empty and "Kennzahlen (gesamter Stand): –" in empty and "**0**" in empty
    many = [mod.Issue("p", n, "INFO", "CODE_SMELL", "r", "m") for n in range(mod.MAX_LISTED_ISSUES + 3)]
    assert "… 3 weitere" in mod.QualityReport(pr=1, sha=SHA, issues=many).render()


# ---------------------------------------------------------------- Sonar-API
def test_fetch_issues_paginates_and_measures(mod, monkeypatch):
    monkeypatch.setattr(mod, "ISSUE_PAGE_SIZE", 2)
    calls = []

    def _get(host, token, path, params):
        calls.append((path, params.get("p")))
        if path == "/api/measures/component":
            return {"component": {"measures": [{"metric": "bugs", "value": "3"}, {"metric": "coverage"}]}}
        return {"issues": [{"n": 1}, {"n": 2}] if params["p"] == 1 else [{"n": 3}]}

    monkeypatch.setattr(mod, "sonar_get", _get)
    assert len(mod.fetch_issues("h", "t", KEY)) == 3
    assert mod.fetch_measures("h", "t", KEY) == {"bugs": "3", "coverage": ""}
    assert calls[:2] == [("/api/issues/search", 1), ("/api/issues/search", 2)]


def test_fetch_issues_stops_at_page_cap(mod, monkeypatch):
    monkeypatch.setattr(mod, "ISSUE_PAGE_SIZE", 2)
    monkeypatch.setattr(mod, "MAX_ISSUE_PAGES", 2)

    def _get(host, token, path, params):
        assert params["p"] <= 2
        return {"issues": [{"n": 1}, {"n": 2}]}  # immer volle Seite → Cap greift

    monkeypatch.setattr(mod, "sonar_get", _get)
    assert len(mod.fetch_issues("h", "t", KEY)) == 4


def test_sonar_get_uses_basic_auth(mod, monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _open(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _open)
    assert mod.sonar_get("http://h/", "tok", "/api/x", {"a": "1 2"}) == {"ok": True}
    assert seen["url"] == "http://h/api/x?a=1+2"
    assert seen["auth"] == "Basic dG9rOg=="  # base64("tok:")


# ---------------------------------------------------------------- diff-cover
def _fake_diff_cover(monkeypatch, mod, data):
    def _run(cmd, **kw):
        assert cmd[0] == "diff-cover" and "--compare-branch=origin/main" in cmd
        Path(cmd[-1].split(":", 1)[1]).write_text(json.dumps(data))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", _run)


def test_diff_coverage(mod, monkeypatch, tmp_path):
    data = {
        "total_num_lines": 10,
        "total_percent_covered": 80,
        "src_stats": {"fabrik/src/a.py": {"violation_lines": [7, 3]}, "fabrik/src/b.py": {"violation_lines": []}},
    }
    _fake_diff_cover(monkeypatch, mod, data)
    assert mod.diff_coverage(tmp_path / "c.xml", "origin/main") == (80.0, {"fabrik/src/a.py": [3, 7]})


def test_diff_coverage_without_measurable_lines(mod, monkeypatch, tmp_path):
    _fake_diff_cover(monkeypatch, mod, {"total_num_lines": 0, "src_stats": {}})
    assert mod.diff_coverage(tmp_path / "c.xml", "origin/main") == (None, {})


# ---------------------------------------------------------------- Kommentar-Upsert
@pytest.fixture
def fake_gh(mod, monkeypatch):
    state = {"comments": [], "calls": []}

    def _gh(*args, stdin=None):
        state["calls"].append((args, stdin))
        if args[0] == "api" and len(args) == 2:  # GET-Liste
            page = int(args[1].rsplit("page=", 1)[1])
            size = mod.COMMENT_PAGE_SIZE
            return json.dumps(state["comments"][(page - 1) * size : page * size])
        if args[:2] == ("repo", "view"):
            return "o/r\n"
        return "{}"

    monkeypatch.setattr(mod, "_gh", _gh)
    return state


def test_upsert_creates_when_no_marker_comment(mod, fake_gh):
    fake_gh["comments"] = [{"id": 1, "body": "human"}]
    assert mod.upsert_comment("o/r", 7, "report") == "created"
    args, stdin = fake_gh["calls"][-1]
    assert args[:4] == ("api", "-X", "POST", "repos/o/r/issues/7/comments")
    assert json.loads(stdin) == {"body": "report"}


def test_upsert_updates_existing_marker_comment_across_pages(mod, fake_gh, monkeypatch):
    monkeypatch.setattr(mod, "COMMENT_PAGE_SIZE", 2)
    fake_gh["comments"] = [
        {"id": 1, "body": "a"},
        {"id": 2, "body": "b"},
        {"id": 9, "body": f"old\n<!-- fabrik-quality sha={SHA} -->"},
    ]
    assert mod.upsert_comment("o/r", 7, "new") == "updated"
    assert fake_gh["calls"][-1][0][:4] == ("api", "-X", "PATCH", "repos/o/r/issues/comments/9")


# ---------------------------------------------------------------- CLI
def test_main_builds_report_and_posts(mod, monkeypatch, tmp_path, capsys, fake_gh):
    git = {
        ("diff", "-U0", "origin/main...HEAD"): DIFF,
        ("merge-base", "origin/main", "HEAD"): "m" * 40 + "\n",
        ("rev-parse", "--show-prefix"): "fabrik/\n",
        ("rev-parse", "HEAD"): SHA + "\n",
    }
    monkeypatch.setattr(mod, "_git", lambda *a: git[a])
    monkeypatch.setattr(mod, "fetch_measures", lambda h, t, k: {"bugs": "0"})
    monkeypatch.setattr(mod, "fetch_issues", lambda h, t, k: [_issue("src/a.py", 4)])
    monkeypatch.setattr(
        mod,
        "lint_issues",
        lambda changed, prefix: [mod.Issue("fabrik/src/a.py", 5, "warning", "LINT", "ruff:F401", "unused")],
    )
    cov = tmp_path / "coverage.xml"
    cov.write_text("<coverage/>")
    monkeypatch.setattr(mod, "diff_coverage", lambda path, ref: (100.0, {}))
    assert mod.main(["--pr", "7", "--project-key", KEY, "--coverage", str(cov), "--post"]) == 0
    out, err = capsys.readouterr()
    assert "Coverage geänderter Zeilen: **100 %**" in out
    assert "`fabrik/src/a.py:4`" in out
    assert "- Lint (ruff + mypy) auf geänderten Zeilen: **1**" in out
    assert out.strip().endswith(f"sha={SHA} -->")
    assert "PR-Kommentar created" in err
    assert any(c[0][:2] == ("repo", "view") for c in fake_gh["calls"])


def test_main_without_coverage_file(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "_git", lambda *a: SHA if a == ("rev-parse", "HEAD") else "")
    monkeypatch.setattr(mod, "fetch_measures", lambda h, t, k: {})
    monkeypatch.setattr(mod, "fetch_issues", lambda h, t, k: [])
    monkeypatch.setattr(mod, "lint_issues", lambda changed, prefix: [])
    assert mod.main(["--pr", "7", "--project-key", KEY, "--coverage", str(tmp_path / "nope.xml")]) == 0
    assert "**–**" in capsys.readouterr().out
