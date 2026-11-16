"""`python -m fabrik.relay <command> --project P [--issue N]` — von den generierten Loops aufgerufen."""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from ..failures import classify_exception, exit_code
from ..gitops import project_github
from ..runmodel import FailureKind, verdict_line
from . import commands
from .config import RelayConfig, load
from .github import RelayGitHub

_ISSUE_COMMANDS = {
    "checkout": commands.checkout,
    "checkin": commands.checkin,
    "publish": commands.publish,
    "audit-prepare": commands.audit_prepare,
    "audit-reveal": commands.audit_reveal,
    "audit-publish": commands.audit_publish,
}


def _issue(raw: str | None) -> int:
    if raw is None or not raw.isdigit():
        raise SystemExit(f"--issue muss eine Issue-Nummer sein (ist {raw!r}; Run ohne issue_number gestartet?)")
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fabrik.relay", description=__doc__)
    parser.add_argument("command", choices=["dispatch", *_ISSUE_COMMANDS])
    parser.add_argument("--project", required=True)
    parser.add_argument("--issue")
    args = parser.parse_args(argv)
    spec, cfg = load(args.project)
    with project_github(spec):  # GitHub-Identitäten des Projekts (github_env_file)
        return _run(args, cfg)


def _run(args: argparse.Namespace, cfg: RelayConfig) -> int:
    ctx = commands.RelayContext(
        cfg=cfg,
        github=RelayGitHub(cfg.repo, bot_login=cfg.bot_login),
        now=datetime.now(UTC),
        workdir=Path.cwd(),
    )
    try:
        if args.command == "dispatch":
            print(commands.dispatch(ctx))
        else:
            summary = _ISSUE_COMMANDS[args.command](ctx, _issue(args.issue))
            if verdict := getattr(summary, "verdict", ""):
                print(verdict_line(verdict))  # vor der Summary: deren letzte Zeile ist die Step-Summary
            print(summary)
    except Exception as e:  # noqa: BLE001 — jeder Fehler wird klassifiziert und als Exit-Code gemeldet
        # Exit-Code = Fehlerklasse (ADR 0035): 75 transient (Netz, 5xx, Rate-Limit), 78 environment
        # (401/403 — meist abgelaufenes App-Token —, falsches bot_login), sonst 1 (unknown).
        failure = classify_exception(e)
        if failure.kind is FailureKind.UNKNOWN and not isinstance(e, commands.RelayError):
            traceback.print_exc()  # unerwartet: Stacktrace fürs Run-Log
        print(f"relay: {e} [{failure.kind}]", file=sys.stderr)
        return exit_code(failure.kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
