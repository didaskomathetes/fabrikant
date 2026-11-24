#!/usr/bin/env python3
"""Report: loads all run records (`runs/`) into the typed model and lists the unloadable ones with the reason (ADR 0034).

Exit 0 if all are loadable, otherwise 1. Usage: ``uv run python scripts/check-run-records.py [project]``."""

import sys

from fabrikant.runstore import RunStore


def main(argv: list[str]) -> int:
    runs, report = RunStore().load_report(argv[1] if len(argv) > 1 else None)
    print(f"{report.loaded}/{report.total} records loaded")
    for failure in report.failures:
        print(f"  ✖ {failure.path}: {failure.reason}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
