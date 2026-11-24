#!/usr/bin/env python3
"""sonar_report — SonarQube-API-Antworten → Report-JSON und PR-Kommentar (Markdown), ohne LLM.

Aufruf aus scripts/sonar-scan.sh (Schritt 6b); die Antworten kommen als Dateien:

    sonar_report.py --gate G.json --measures M.json --issues I.json --project-key K \\
        --repo-prefix fabrikant/ --dashboard URL --report out.json \\
        [--comment out.md --link-base https://github.com/<repo>/blob/<sha>]

Nur Standardbibliothek (läuft mit dem System-Python wie die übrigen Skripte).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SEVERITY_ORDER = ("INFO", "LOW", "MEDIUM", "HIGH", "BLOCKER")
# Legacy-Severities (vor dem Clean-Code-Modell) → Impact-Severity, für Issues ohne `impacts`.
LEGACY_SEVERITY = {"INFO": "INFO", "MINOR": "LOW", "MAJOR": "MEDIUM", "CRITICAL": "HIGH", "BLOCKER": "BLOCKER"}
GATE_HEADLINE = {"OK": "✅ passed", "ERROR": "❌ failed", "WARN": "⚠️ warning"}
MAX_LISTED_ISSUES = 15
MISSING = "–"
# (Metrik, Beschriftung, Prozentwert?) — Reihenfolge = Tabellenzeilen.
METRIC_ROWS = (
    ("new_violations", "New issues", False),
    ("new_coverage", "Coverage on new code", True),
    ("new_duplicated_lines_density", "Duplication on new code", True),
    ("new_security_hotspots", "Security hotspots", False),
)


@dataclass(frozen=True)
class Issue:
    """Offenes Issue, Pfad relativ zur Repo-Wurzel (nicht zur Sonar-Projekt-Basis)."""

    rule: str
    severity: str
    type: str
    message: str
    path: str
    line: int | None = None

    @classmethod
    def from_api(cls, raw: dict, *, project_key: str, repo_prefix: str) -> Issue:
        return cls(
            rule=raw["rule"],
            severity=_severity(raw),
            type=raw.get("type", ""),
            message=raw["message"],
            path=repo_prefix + raw["component"].removeprefix(project_key + ":"),
            line=raw.get("line"),
        )

    def rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity) if self.severity in SEVERITY_ORDER else -1


@dataclass(frozen=True)
class Report:
    """Ergebnis einer Analyse: Gate, Kennzahlen, offene Issues."""

    status: str
    dashboard: str
    conditions: list[dict] = field(default_factory=list)
    measures: dict[str, str | None] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    issues_total: int = 0

    @classmethod
    def from_api(cls, gate: dict, measures: dict, issues: dict, *, project_key: str, repo_prefix: str, dashboard: str):
        status = gate["projectStatus"]
        parsed = [Issue.from_api(i, project_key=project_key, repo_prefix=repo_prefix) for i in issues["issues"]]
        return cls(
            status=status["status"],
            dashboard=dashboard,
            conditions=status.get("conditions", []),
            # PR-/Branch-Analysen liefern new_*-Werte teils nur unter `period`.
            measures={
                m["metric"]: m.get("value", (m.get("period") or {}).get("value"))
                for m in measures["component"].get("measures", [])
            },
            issues=parsed,
            issues_total=issues.get("total", len(parsed)),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    def comment(self, link_base: str) -> str:
        """PR-Kommentar (Englisch): Gate, Kennzahlen, verletzte Bedingungen, schwerste Issues, Dashboard-Link."""
        parts = [f"### SonarQube: Quality Gate {GATE_HEADLINE.get(self.status, self.status)}", self._metrics_table()]
        failed = [c for c in self.conditions if c.get("status") == "ERROR"]
        if failed:
            parts.append("**Failed conditions**\n" + "\n".join(_condition_line(c) for c in failed))
        if self.issues:
            ranked = sorted(self.issues, key=Issue.rank, reverse=True)[:MAX_LISTED_ISSUES]
            lines = [_issue_line(i, link_base) for i in ranked]
            if self.issues_total > len(ranked):
                lines.append(f"- … and {self.issues_total - len(ranked)} more")
            parts.append("**Issues**\n" + "\n".join(lines))
        parts.append(f"<sub>Local SonarQube analysis (fabrikant loop sonar-pr) · Details: {self.dashboard}</sub>")
        return "\n\n".join(parts) + "\n"

    def _metrics_table(self) -> str:
        rows = ["| Metric | Value |", "|---|---|"]
        rows += [f"| {label} | {_format(self.measures.get(key), pct)} |" for key, label, pct in METRIC_ROWS]
        return "\n".join(rows)


def _severity(raw: dict) -> str:
    """Höchste Impact-Severity; Fallback: Legacy-`severity`, auf das Impact-Modell abgebildet."""
    impacts = [i["severity"] for i in raw.get("impacts") or [] if i.get("severity") in SEVERITY_ORDER]
    if impacts:
        return max(impacts, key=SEVERITY_ORDER.index)
    legacy = str(raw.get("severity", ""))
    return LEGACY_SEVERITY.get(legacy, legacy)


def _format(value: str | None, percent: bool) -> str:
    if value is None:
        return MISSING
    if not percent:
        return value
    try:
        return f"{float(value):.1f} %"
    except ValueError:
        return value


def _condition_line(cond: dict) -> str:
    return (
        f"- `{cond.get('metricKey', '?')}`: {cond.get('actualValue', MISSING)}"
        f" (threshold: {cond.get('comparator', '')} {cond.get('errorThreshold', '')})".rstrip()
    )


def _issue_line(issue: Issue, link_base: str) -> str:
    where = f"{issue.path}:{issue.line}" if issue.line else issue.path
    anchor = f"#L{issue.line}" if issue.line else ""
    return f"- **{issue.severity}** [{where}]({link_base}/{issue.path}{anchor}) — {issue.message} (`{issue.rule}`)"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("gate", "measures", "issues", "report"):
        p.add_argument(f"--{name}", type=Path, required=True)
    p.add_argument("--project-key", required=True)
    p.add_argument("--repo-prefix", default="")
    p.add_argument("--dashboard", required=True)
    p.add_argument("--comment", type=Path)
    p.add_argument("--link-base", default="")
    a = p.parse_args(argv)
    if a.comment and not a.link_base:
        p.error("--comment requires --link-base")
    report = Report.from_api(
        *(json.loads(f.read_text(encoding="utf-8")) for f in (a.gate, a.measures, a.issues)),
        project_key=a.project_key,
        repo_prefix=a.repo_prefix,
        dashboard=a.dashboard,
    )
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report.to_json(), encoding="utf-8")
    if a.comment:
        a.comment.write_text(report.comment(a.link_base.rstrip("/")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
