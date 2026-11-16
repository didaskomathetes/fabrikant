#!/usr/bin/env python3
"""pyright_sonar — Pyright-JSON (`pyright --outputjson`) → SonarQube Generic Issue Import Format.

Sonar-Python importiert Ruff/Bandit/Mypy nativ, Pyright nicht; dieser Report geht über
`sonar.externalIssuesReportPaths` (scripts/lint.sh --reports, scripts/sonar-scan.sh):

    pyright_sonar.py pyright.json pyright-sonar.json [--base DIR]

Pfade werden relativ zu `--base` (Default: cwd = Sonar-Projektbasis). Nur Zeilen, keine Spalten:
eine Spalte jenseits des Zeilenendes ließe Sonar den ganzen Report verwerfen.
Nur Standardbibliothek (läuft mit dem System-Python wie die übrigen Skripte).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

ENGINE_ID = "pyright"
# Diagnosen ohne Regel (Syntaxfehler, unaufgelöste Imports o. Ä.).
FALLBACK_RULE = "pyright"
DOCS_URL = "https://microsoft.github.io/pyright/#/configuration?id=type-check-diagnostics-settings"


@dataclass(frozen=True)
class SeverityMapping:
    """Pyright-Severity → Sonar-Regel-Attribute (Standard-Experience- und MQR-Modus)."""

    rank: int
    type: str
    severity: str
    quality: str
    impact: str


SEVERITIES = {
    "error": SeverityMapping(3, "BUG", "MAJOR", "RELIABILITY", "MEDIUM"),
    "warning": SeverityMapping(2, "CODE_SMELL", "MINOR", "MAINTAINABILITY", "LOW"),
    "information": SeverityMapping(1, "CODE_SMELL", "INFO", "MAINTAINABILITY", "INFO"),
}


@dataclass(frozen=True)
class Diagnostic:
    """Eine Pyright-Diagnose; `line` 1-basiert (Pyright zählt ab 0)."""

    rule: str
    severity: str
    message: str
    file: str
    line: int

    @classmethod
    def from_pyright(cls, raw: dict, base: Path) -> Diagnostic:
        severity = raw.get("severity", "error")
        return cls(
            rule=raw.get("rule") or FALLBACK_RULE,
            severity=severity if severity in SEVERITIES else "error",
            message=raw.get("message", "").strip(),
            file=os.path.relpath(raw["file"], base),
            line=raw.get("range", {}).get("start", {}).get("line", 0) + 1,
        )

    def to_issue(self) -> dict:
        return {
            "ruleId": self.rule,
            "primaryLocation": {"message": self.message, "filePath": self.file, "textRange": {"startLine": self.line}},
        }


def _rule(rule_id: str, severity: str) -> dict:
    m = SEVERITIES[severity]
    return {
        "id": rule_id,
        "name": rule_id,
        "description": f"Pyright diagnostic {rule_id} — {DOCS_URL}",
        "engineId": ENGINE_ID,
        "cleanCodeAttribute": "LOGICAL",
        "type": m.type,
        "severity": m.severity,
        "impacts": [{"softwareQuality": m.quality, "severity": m.impact}],
    }


def convert(pyright: dict, base: Path) -> dict:
    """Pyright-Output → Generic-Issue-Report. Eine Regel pro Pyright-Regel, mit der schwersten
    beobachteten Severity (Sonar kennt die Severity nur pro Regel)."""
    diagnostics = [Diagnostic.from_pyright(d, base) for d in pyright.get("generalDiagnostics", [])]
    worst: dict[str, str] = {}
    for d in diagnostics:
        if d.rule not in worst or SEVERITIES[d.severity].rank > SEVERITIES[worst[d.rule]].rank:
            worst[d.rule] = d.severity
    return {
        "rules": [_rule(rule_id, sev) for rule_id, sev in sorted(worst.items())],
        "issues": [d.to_issue() for d in diagnostics],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pyright_json", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--base", type=Path, default=Path.cwd())
    a = p.parse_args(argv)
    report = convert(json.loads(a.pyright_json.read_text(encoding="utf-8")), a.base.resolve())
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
