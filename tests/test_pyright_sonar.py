"""scripts/pyright_sonar.py: Pyright-JSON → SonarQube Generic Issue Import Format."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import load_script

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pyright_sonar.py"
ps = load_script(_PATH.name, "pyright_sonar")


def _diag(base: Path, rule: str | None = "reportX", severity: str = "error", line: int = 4) -> dict:
    raw = {
        "file": str(base / "src" / "m.py"),
        "severity": severity,
        "message": "  kaputt \n",
        "range": {"start": {"line": line, "character": 2}, "end": {"line": line, "character": 9}},
    }
    if rule is not None:
        raw["rule"] = rule
    return raw


def test_issue_is_relative_one_based_and_line_only(tmp_path):
    report = ps.convert({"generalDiagnostics": [_diag(tmp_path)]}, tmp_path)
    assert report["issues"] == [
        {
            "ruleId": "reportX",
            "primaryLocation": {"message": "kaputt", "filePath": "src/m.py", "textRange": {"startLine": 5}},
        }
    ]
    (rule,) = report["rules"]
    assert rule["engineId"] == "pyright"
    assert (rule["type"], rule["severity"]) == ("BUG", "MAJOR")
    assert rule["impacts"] == [{"softwareQuality": "RELIABILITY", "severity": "MEDIUM"}]


def test_rule_takes_worst_observed_severity(tmp_path):
    diags = [
        _diag(tmp_path, severity="warning"),
        _diag(tmp_path, severity="error"),
        _diag(tmp_path, severity="information"),
    ]
    (rule,) = ps.convert({"generalDiagnostics": diags}, tmp_path)["rules"]
    assert rule["severity"] == "MAJOR"


def test_missing_rule_and_unknown_severity_fall_back(tmp_path):
    report = ps.convert({"generalDiagnostics": [_diag(tmp_path, rule=None, severity="weird")]}, tmp_path)
    assert report["issues"][0]["ruleId"] == ps.FALLBACK_RULE
    assert report["rules"][0]["severity"] == "MAJOR"  # unbekannt = error


def test_clean_run_gives_empty_report(tmp_path):
    assert ps.convert({"summary": {"errorCount": 0}}, tmp_path) == {"rules": [], "issues": []}


def test_main_writes_report(tmp_path):
    src = tmp_path / "pyright.json"
    src.write_text(json.dumps({"generalDiagnostics": [_diag(tmp_path, severity="warning")]}))
    out = tmp_path / "out" / "sonar.json"
    assert ps.main([str(src), str(out), "--base", str(tmp_path)]) == 0
    data = json.loads(out.read_text())
    assert data["issues"][0]["primaryLocation"]["filePath"] == "src/m.py"
    assert data["rules"][0]["type"] == "CODE_SMELL"
