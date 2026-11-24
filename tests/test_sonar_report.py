"""scripts/sonar_report.py: Sonar-API-Antworten → Report + PR-Kommentar (Template, kein LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import load_script

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sonar_report.py"
sr = load_script(_PATH.name, "sonar_report")

KEY = "fabrikant:fabrikant"
LINK = "https://github.com/o/r/blob/abc"


def _issue(severity: str = "MAJOR", impacts: list[str] | None = None, line: int | None = 3, n: int = 0) -> dict:
    raw: dict[str, object] = {
        "rule": f"python:S{n}",
        "severity": severity,
        "message": f"m{n}",
        "component": f"{KEY}:src/f{n}.py",
    }
    if impacts is not None:
        raw["impacts"] = [{"severity": s} for s in impacts]
    if line is not None:
        raw["line"] = line
    return raw


def _report(status: str = "OK", conditions: list | None = None, measures: list | None = None, issues=None, total=None):
    issues = issues or []
    return sr.Report.from_api(
        {"projectStatus": {"status": status, "conditions": conditions or []}},
        {"component": {"measures": measures or []}},
        {"issues": issues, "total": len(issues) if total is None else total},
        project_key=KEY,
        repo_prefix="fabrikant/",
        dashboard="http://sonar/dashboard",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_issue(impacts=["LOW", "HIGH"]), "HIGH"),  # höchste Impact-Severity
        (_issue(severity="CRITICAL"), "HIGH"),  # Legacy ohne impacts → Impact-Skala
        (_issue(severity="MINOR"), "LOW"),
    ],
)
def test_severity(raw, expected):
    assert sr.Issue.from_api(raw, project_key=KEY, repo_prefix="").severity == expected


def test_green_gate_without_issues_is_headline_table_footer_only():
    text = _report(measures=[{"metric": "new_coverage", "period": {"value": "87.25"}}]).comment(LINK)
    assert text.startswith("### SonarQube: Quality Gate ✅ passed")
    assert "| Coverage on new code | 87.2 % |" in text  # period-Wert, eine Nachkommastelle
    assert "| New issues | – |" in text  # fehlende Kennzahl
    assert "Failed conditions" not in text and "**Issues**" not in text
    assert text.rstrip().endswith("Details: http://sonar/dashboard</sub>")


def test_red_gate_lists_failed_conditions_only():
    conditions = [
        {
            "status": "ERROR",
            "metricKey": "new_coverage",
            "actualValue": "50.0",
            "comparator": "LT",
            "errorThreshold": "80",
        },
        {"status": "OK", "metricKey": "new_violations", "actualValue": "0"},
    ]
    text = _report(status="ERROR", conditions=conditions).comment(LINK)
    assert "❌ failed" in text
    assert "- `new_coverage`: 50.0 (threshold: LT 80)" in text
    assert "new_violations" not in text.split("Failed conditions")[1]


def test_issues_sorted_by_severity_capped_and_linked():
    issues = [_issue(impacts=["LOW"], n=i) for i in range(sr.MAX_LISTED_ISSUES)]
    issues.append(_issue(impacts=["BLOCKER"], line=None, n=99))
    text = _report(issues=issues, total=40).comment(LINK)
    listed = [line for line in text.splitlines() if line.startswith("- **")]
    assert len(listed) == sr.MAX_LISTED_ISSUES
    assert listed[0] == f"- **BLOCKER** [fabrikant/src/f99.py]({LINK}/fabrikant/src/f99.py) — m99 (`python:S99`)"
    assert f"[fabrikant/src/f0.py:3]({LINK}/fabrikant/src/f0.py#L3)" in text
    assert "- … and 25 more" in text  # total zählt alle, nicht nur die abgerufenen


def test_cli_writes_report_and_comment(tmp_path):
    files = {
        "gate": {"projectStatus": {"status": "WARN"}},
        "measures": {"component": {"measures": []}},
        "issues": {"issues": [_issue()], "total": 1},
    }
    for name, data in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    out = tmp_path / "out"
    rc = sr.main(
        [*(a for n in files for a in (f"--{n}", str(tmp_path / f"{n}.json"))),
         "--project-key", KEY, "--repo-prefix", "fabrikant/", "--dashboard", "d",
         "--report", str(out / "report.json"), "--comment", str(out / "comment.md"), "--link-base", LINK + "/"]
    )  # fmt: skip
    assert rc == 0
    assert json.loads((out / "report.json").read_text())["issues"][0]["path"] == "fabrikant/src/f0.py"
    assert "⚠️ warning" in (out / "comment.md").read_text()
    assert f"({LINK}/fabrikant/src/f0.py#L3)" in (out / "comment.md").read_text()  # kein doppelter Slash


def test_cli_comment_requires_link_base(tmp_path):
    for name in ("gate", "measures", "issues"):
        (tmp_path / f"{name}.json").write_text("{}")
    args = [a for n in ("gate", "measures", "issues") for a in (f"--{n}", str(tmp_path / f"{n}.json"))]
    with pytest.raises(SystemExit):
        sr.main([*args, "--project-key", KEY, "--dashboard", "d", "--report", str(tmp_path / "r.json"),
                 "--comment", str(tmp_path / "c.md")])  # fmt: skip
