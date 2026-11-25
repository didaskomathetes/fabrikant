#!/usr/bin/env python3
"""sonar_issues — Pyright/ShellCheck JSON → one SonarQube Generic Issue Import report.

Sonar-Python imports Ruff/Bandit natively, Pyright and ShellCheck it does not; this report goes through
`sonar.externalIssuesReportPaths` (scripts/lint.sh --reports, scripts/sonar-scan.sh):

    sonar_issues.py OUT [--pyright pyright.json] [--shellcheck shellcheck.json] [--base DIR]

Inputs: `pyright --outputjson` and `shellcheck -f json1`. Rule IDs are `<tool>:<rule>` (e.g.
`pyright:reportOptionalMemberAccess`, `shellcheck:SC2086`). Paths are made relative to `--base`
(default: cwd = Sonar project base). Lines only, no columns: a column beyond the end of the line
would make Sonar discard the whole report. Standard library only (runs with the system Python).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

PYRIGHT = "pyright"
SHELLCHECK = "shellcheck"
# Diagnostics without a rule (syntax errors, unresolved imports, etc.).
PYRIGHT_FALLBACK_RULE = "pyright"
DOCS_URL = {
    PYRIGHT: "https://microsoft.github.io/pyright/#/configuration?id=type-check-diagnostics-settings",
    SHELLCHECK: "https://www.shellcheck.net/wiki/",
}


@dataclass(frozen=True)
class SeverityMapping:
    """Normalized severity → Sonar rule attributes (standard experience and MQR mode)."""

    rank: int
    type: str
    severity: str
    quality: str
    impact: str


ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITIES = {
    ERROR: SeverityMapping(3, "BUG", "MAJOR", "RELIABILITY", "MEDIUM"),
    WARNING: SeverityMapping(2, "CODE_SMELL", "MINOR", "MAINTAINABILITY", "LOW"),
    INFO: SeverityMapping(1, "CODE_SMELL", "INFO", "MAINTAINABILITY", "INFO"),
}
# Tool level → normalized severity (unknown = error: better too loud than swallowed).
LEVELS = {
    "error": ERROR,
    "warning": WARNING,
    "information": INFO,  # pyright
    "info": INFO,  # shellcheck
    "style": INFO,  # shellcheck
}


@dataclass(frozen=True)
class Diagnostic:
    """One finding of a tool; `line` is 1-based, `file` relative to the Sonar project base."""

    engine: str
    rule: str
    severity: str
    message: str
    file: str
    line: int

    @classmethod
    def from_pyright(cls, raw: dict, base: Path) -> Diagnostic:
        return cls(
            engine=PYRIGHT,
            rule=raw.get("rule") or PYRIGHT_FALLBACK_RULE,
            severity=LEVELS.get(raw.get("severity", ""), ERROR),
            message=raw.get("message", "").strip(),
            file=os.path.relpath(raw["file"], base),
            line=raw.get("range", {}).get("start", {}).get("line", 0) + 1,  # pyright counts from 0
        )

    @classmethod
    def from_shellcheck(cls, raw: dict, base: Path) -> Diagnostic:
        return cls(
            engine=SHELLCHECK,
            rule=f"SC{raw['code']}",
            severity=LEVELS.get(raw.get("level", ""), ERROR),
            message=raw.get("message", "").strip(),
            file=os.path.relpath(Path(raw["file"]).resolve(), base),
            line=raw.get("line", 1),
        )

    @property
    def rule_id(self) -> str:
        return f"{self.engine}:{self.rule}"

    def to_issue(self) -> dict:
        return {
            "ruleId": self.rule_id,
            "primaryLocation": {"message": self.message, "filePath": self.file, "textRange": {"startLine": self.line}},
        }


def _rule(d: Diagnostic, severity: str) -> dict:
    m = SEVERITIES[severity]
    docs = DOCS_URL[d.engine] + (d.rule if d.engine == SHELLCHECK else "")
    return {
        "id": d.rule_id,
        "name": d.rule,
        "description": f"{d.engine} {d.rule} — {docs}",
        "engineId": d.engine,
        "cleanCodeAttribute": "LOGICAL",
        "type": m.type,
        "severity": m.severity,
        "impacts": [{"softwareQuality": m.quality, "severity": m.impact}],
    }


def build_report(diagnostics: list[Diagnostic]) -> dict:
    """One rule per (tool, rule), with the most severe observed severity (Sonar only knows
    severity per rule)."""
    worst: dict[str, Diagnostic] = {}
    for d in diagnostics:
        seen = worst.get(d.rule_id)
        if seen is None or SEVERITIES[d.severity].rank > SEVERITIES[seen.severity].rank:
            worst[d.rule_id] = d
    return {
        "rules": [_rule(d, d.severity) for _, d in sorted(worst.items())],
        "issues": [d.to_issue() for d in diagnostics],
    }


def from_pyright(report: dict, base: Path) -> list[Diagnostic]:
    return [Diagnostic.from_pyright(d, base) for d in report.get("generalDiagnostics", [])]


def from_shellcheck(report: dict, base: Path) -> list[Diagnostic]:
    return [Diagnostic.from_shellcheck(c, base) for c in report.get("comments", [])]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("output", type=Path)
    p.add_argument("--pyright", type=Path)
    p.add_argument("--shellcheck", type=Path)
    p.add_argument("--base", type=Path, default=Path.cwd())
    a = p.parse_args(argv)
    base = a.base.resolve()
    diagnostics: list[Diagnostic] = []
    for path, parse in ((a.pyright, from_pyright), (a.shellcheck, from_shellcheck)):
        if path is not None:
            diagnostics += parse(json.loads(path.read_text(encoding="utf-8")), base)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(build_report(diagnostics), indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
