"""scripts/sonar_issues.py: Pyright-/ShellCheck-JSON → SonarQube Generic Issue Import Format."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import load_script

si = load_script("sonar_issues.py", "sonar_issues")


def _pyright(base: Path, rule: str | None = "reportX", severity: str = "error", line: int = 4) -> dict:
    raw = {
        "file": str(base / "src" / "m.py"),
        "severity": severity,
        "message": "  broken \n",
        "range": {"start": {"line": line, "character": 2}, "end": {"line": line, "character": 9}},
    }
    if rule is not None:
        raw["rule"] = rule
    return raw


def _shellcheck(level: str = "warning", code: int = 2086, line: int = 7) -> dict:
    return {"file": "scripts/x.sh", "line": line, "column": 3, "level": level, "code": code, "message": "quote it"}


def test_pyright_issue_is_relative_one_based_and_line_only(tmp_path):
    report = si.build_report(si.from_pyright({"generalDiagnostics": [_pyright(tmp_path)]}, tmp_path))
    assert report["issues"] == [
        {
            "ruleId": "pyright:reportX",
            "primaryLocation": {"message": "broken", "filePath": "src/m.py", "textRange": {"startLine": 5}},
        }
    ]
    (rule,) = report["rules"]
    assert (rule["engineId"], rule["name"], rule["type"], rule["severity"]) == ("pyright", "reportX", "BUG", "MAJOR")
    assert rule["impacts"] == [{"softwareQuality": "RELIABILITY", "severity": "MEDIUM"}]


def test_shellcheck_issue_uses_sc_code_and_wiki_link(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # ShellCheck reports paths relative to its cwd
    report = si.build_report(si.from_shellcheck({"comments": [_shellcheck()]}, tmp_path))
    (issue,) = report["issues"]
    assert issue["ruleId"] == "shellcheck:SC2086"
    assert issue["primaryLocation"]["filePath"] == "scripts/x.sh"
    assert issue["primaryLocation"]["textRange"] == {"startLine": 7}
    (rule,) = report["rules"]
    assert rule["engineId"] == "shellcheck" and rule["severity"] == "MINOR"
    assert rule["description"].endswith("https://www.shellcheck.net/wiki/SC2086")


def test_rule_takes_worst_observed_severity(tmp_path):
    diags = [_pyright(tmp_path, severity=s) for s in ("warning", "error", "information")]
    (rule,) = si.build_report(si.from_pyright({"generalDiagnostics": diags}, tmp_path))["rules"]
    assert rule["severity"] == "MAJOR"


def test_shellcheck_info_and_style_are_info(tmp_path):
    comments = [_shellcheck("style", 2001), _shellcheck("info", 2016)]
    rules = si.build_report(si.from_shellcheck({"comments": comments}, tmp_path))["rules"]
    assert {r["severity"] for r in rules} == {"INFO"}


def test_missing_rule_and_unknown_severity_fall_back(tmp_path):
    report = si.build_report(
        si.from_pyright({"generalDiagnostics": [_pyright(tmp_path, rule=None, severity="weird")]}, tmp_path)
    )
    assert report["issues"][0]["ruleId"] == f"pyright:{si.PYRIGHT_FALLBACK_RULE}"
    assert report["rules"][0]["severity"] == "MAJOR"  # unknown = error


def test_clean_run_gives_empty_report():
    assert si.build_report([]) == {"rules": [], "issues": []}


def test_main_merges_both_tools(tmp_path):
    pyright = tmp_path / "pyright.json"
    pyright.write_text(json.dumps({"generalDiagnostics": [_pyright(tmp_path, severity="warning")]}))
    shellcheck = tmp_path / "shellcheck.json"
    shellcheck.write_text(json.dumps({"comments": [_shellcheck("error", 1009)]}))
    out = tmp_path / "out" / "external-issues.json"
    args = [str(out), "--pyright", str(pyright), "--shellcheck", str(shellcheck), "--base", str(tmp_path)]
    assert si.main(args) == 0
    data = json.loads(out.read_text())
    assert sorted(i["ruleId"] for i in data["issues"]) == ["pyright:reportX", "shellcheck:SC1009"]
    assert {r["engineId"] for r in data["rules"]} == {"pyright", "shellcheck"}


def test_main_without_inputs_writes_empty_report(tmp_path):
    out = tmp_path / "r.json"
    assert si.main([str(out)]) == 0
    assert json.loads(out.read_text()) == {"rules": [], "issues": []}
