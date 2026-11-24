#!/usr/bin/env python3
"""Report: lädt alle Run-Records (`runs/`) ins typisierte Modell und nennt die nicht ladbaren mit Grund (ADR 0034).

Exit 0, wenn alle ladbar sind, sonst 1. Aufruf: ``uv run python scripts/check-run-records.py [projekt]``."""

import sys

from fabrikant.runstore import RunStore


def main(argv: list[str]) -> int:
    runs, report = RunStore().load_report(argv[1] if len(argv) > 1 else None)
    print(f"{report.loaded}/{report.total} Records geladen")
    for failure in report.failures:
        print(f"  ✖ {failure.path}: {failure.reason}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
