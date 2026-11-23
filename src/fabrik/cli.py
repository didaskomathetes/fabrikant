"""CLI: list, validate, project add, run, replay, runs, serve, approve, reject, pause/resume/disable, chat, alert."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import auth, config, pause, profiles, retry_chains, spec_sources
from .chat import _append_run_log, _approval_detail, _chat, _chat_alert_test, _revoke_approval
from .engine import reject_run, run_loop
from .failures import parse_kind
from .i18n import t
from .mcp_server import ALLOW_APPROVALS_ENV as MCP_ALLOW_APPROVALS_ENV
from .mcp_server import READ_ONLY_ENV as MCP_READ_ONLY_ENV
from .mcp_server import serve_stdio
from .replay import diff_runs, replay_run
from .runmodel import FAILED_RUN_STATUSES
from .runs import STEP_MARKS, claim_run, export_run, get_run, list_runs, show_run
from .runstore import STORE, PreconditionError
from .server import serve
from .specs import collect_projects, load_projects


def cmd_list(_args):
    for name, spec in load_projects().items():
        if spec.get("_error"):
            print(f"✖ {name}: {spec['_error']}")
            continue
        repo = spec.get("repo")
        branch = f"  @ {repo['branch']}" if repo else ""
        print(f"● {name}  {spec.get('description', '')}{branch}")
        for lname, ldef in spec.get("loops", {}).items():
            trigs = ", ".join(t.get("type", "?") for t in ldef.get("triggers") or [])
            print(f"    - {lname}  [{trigs}]  {ldef.get('description', '')}")


def cmd_validate(args):
    """Validate specs; exit 1 on errors. ``specs``: only these files instead of all sources,
    ``--offline``: without host-dependent checks (project ``root`` need not exist — CI)."""
    files = [Path(f) for f in args.specs] or None
    projects, conflicts = collect_projects(files, check_root=not args.offline)
    ok = not conflicts
    for msg in conflicts:
        print(f"✖ {msg}")
    for name, spec in projects.items():
        if spec.get("_error"):
            print(f"✖ {name}:\n   {spec['_error']}")
            ok = False
        else:
            print(t("cli.validate_loops", name=name, n=len(spec["loops"])))
            if not args.offline:
                _warn_connection_refs(name, spec)
    sys.exit(0 if ok else 1)


def _warn_connection_refs(name: str, spec: dict) -> None:
    """Unknown ``connection:`` names as a warning (connections are host state, ADR 0052)."""
    from . import connections

    try:
        problems = connections.unknown_references(spec)
    except Exception as e:  # DB not reachable or similar: validate should not fail because of that
        print(t("cli.connections_unchecked", name=name, error=e))
        return
    for msg in problems:
        print(f"  ⚠ {name}: {msg}")


def cmd_project(args):
    """project add <repo-pfad> — Zielrepo mit `.fabrik/fabrik.yaml` registrieren (ADR 0047)."""
    try:
        spec = spec_sources.register_target(args.path)
    except ValueError as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    print(t("cli.registered", spec=spec))


def _cli_actor() -> str:
    """Audit-Name lokaler CLI-Aufrufer (``started_by``/``approved_by``)."""
    return f"cli:{getpass.getuser()}"


def cmd_run(args):
    vars = {}
    for kv in args.var:
        if "=" not in kv:
            print(t("cli.var_format", kv=kv))
            sys.exit(1)
        k, v = kv.split("=", 1)
        vars[k] = v
    blocked = pause.manual_block_reason(args.project, args.loop)
    if blocked:
        print(t("cli.error", error=blocked))
        sys.exit(1)
    claim = claim_run(args.project, args.loop, vars or None)
    if claim is None:
        print(t("cli.run_limit", project=args.project, loop=args.loop))
        sys.exit(1)
    try:
        rec = run_loop(
            args.project,
            args.loop,
            trigger="cli",
            note=args.note or "",
            initial_vars=vars or None,
            held_scope=claim.scope,
            actor=_cli_actor(),
        )
    except Exception as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    finally:
        claim.release()
    print(f"{rec['status'].upper()}  {rec['project']}/{rec['loop']}  ({rec['id']})")
    _print_steps(rec)
    if rec["status"] == "needs_approval":
        print(_approval_detail(rec.get("approval") or {}))
    sys.exit(0 if rec["status"] == "pass" else 1)


def _print_steps(rec: dict) -> None:
    """Print the step list of a run record with status mark."""
    for s in rec["steps"]:
        print(f"  {STEP_MARKS.get(s['status'], '?')} {s['name']} [{s['type']}] {s.get('summary', '')}")


REPLAY_SIDE_EFFECT_NOTICE = (
    "⚠ Replay führt alle Steps real aus (PRs, Kommentare, Merges, Benachrichtigungen) — "
    "gepinnt sind nur die Eingaben, nicht die Seiteneffekte."
)


def _confirm_replay(args) -> bool:
    """Bestätigung vor dem realen Re-Run: `--yes` überspringt; ohne TTY ohne `--yes` Abbruch."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("Abbruch: keine interaktive Eingabe — mit --yes/-y explizit bestätigen.")
        return False
    return input(f"Run {args.run_id} jetzt real neu ausführen? [y/N] ").strip().lower() in ("y", "yes", "j", "ja")


def cmd_replay(args):
    """Replay: einen abgeschlossenen Run mit gepinnten Inputs neu ausführen (ADR 0061)."""
    orig = get_run(args.run_id, args.project)
    if orig is None:
        print(f"Fehler: Run {args.run_id} nicht gefunden")
        sys.exit(1)
    project, loop = orig["project"], orig["loop"]
    blocked = pause.manual_block_reason(project, loop)
    if blocked:
        print(f"Fehler: {blocked}")
        sys.exit(1)
    print(REPLAY_SIDE_EFFECT_NOTICE)
    if not _confirm_replay(args):
        print("Abgebrochen.")
        sys.exit(1)
    claim = claim_run(project, loop, None)
    if claim is None:
        print(f"⚠ {project}: Limit paralleler Runs erreicht oder {loop} läuft bereits.")
        sys.exit(1)
    try:
        orig, rec = replay_run(args.run_id, project=project, actor=_cli_actor(), held_scope=claim.scope, original=orig)
    except Exception as e:  # ReplayError (nicht ersetzbar) und Engine-Fehler werden gleich gemeldet
        print(f"Fehler: {e}")
        sys.exit(1)
    finally:
        claim.release()
    print(f"{rec['status'].upper()}  {rec['project']}/{rec['loop']}  ({rec['id']})  [replay of {orig['id']}]")
    _print_steps(rec)
    print()
    print(diff_runs(orig, rec))
    sys.exit(0 if rec["status"] == "pass" else 1)


def cmd_runs(args):
    """Run-Historie; `show`/`export` zeigen einen Einzelrun (Record + Session-Log)."""
    if args.runs_cmd == "show":
        out = show_run(args.run_id, args.show_project)
        if out is None:
            print(t("cli.run_not_found_short", run_id=args.run_id))
            sys.exit(1)
        print(out)
        return
    if args.runs_cmd == "export":
        rec = export_run(args.run_id, args.export_project)
        if rec is None:
            print(t("cli.run_not_found_short", run_id=args.run_id))
            sys.exit(1)
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return
    for r in list_runs(args.project, args.limit):
        kind = f"  [{parse_kind(r.get('failure_kind')).value}]" if r["status"] in FAILED_RUN_STATUSES else ""
        print(f"{r['id']}  {r['project']}/{r['loop']}  {r['status']:<6} {r.get('trigger', '')}{kind}")


def cmd_serve(args):
    try:
        serve(args.port, args.host)
    except auth.AuthError as e:
        print(t("cli.error", error=e))
        sys.exit(1)


def cmd_mcp(args):
    # fabrik as an MCP stdio server (Roadmap #132): runs, specs, approvals for
    # any MCP-capable agent. Long-lived process — the client keeps the pipe open.
    if args.read_only:
        os.environ[MCP_READ_ONLY_ENV] = "1"
    if args.allow_approvals:
        os.environ[MCP_ALLOW_APPROVALS_ENV] = "1"
    serve_stdio()


def _resume_sync(project: str, run_id: str, approve: bool) -> str:
    """Synchronous resume for the CLI (daemon threads would die with the process)."""
    run = get_run(run_id, project)
    if not run:
        return t("chat.run_not_found", run_id=run_id)
    if run["status"] != "needs_approval":
        return t("chat.not_needs_approval", run_id=run["id"], status=run["status"])
    detail = _approval_detail(run.get("approval") or {})
    if not approve:
        # Atomic rejection (rules out a concurrent resume, ADR 0034) incl. cleanup
        # and retry-chain booking.
        try:
            run = reject_run(project, run, _cli_actor(), lambda msg: _append_run_log(project, run_id, msg))
        except PreconditionError as e:
            return t("chat.cannot_reject", run_id=run["id"], error=e)
        return t("chat.rejected", run_id=run["id"], detail=detail)
    claim = claim_run(project, run["loop"], run.get("vars"), exclude_run_id=run_id)
    if claim is None:
        return t("cli.run_limit_resume", project=project, loop=run["loop"])
    try:
        try:
            STORE.approve(project, run_id, run, _cli_actor())
        except PreconditionError as e:
            return t("chat.cannot_approve", run_id=run["id"], error=e)
        rec = run_loop(project, run["loop"], trigger="approve", resume_id=run_id, held_scope=claim.scope)
        _print_steps(rec)
        return f"■ {rec['status'].upper()} — {run['loop']}"
    except Exception as e:
        # Resume did not start (e.g. spec broken) → revoke the approval so the run can
        # be approved or rejected again.
        _revoke_approval(project, run_id)
        return t("cli.resume_failed", error=e)
    finally:
        claim.release()


def cmd_approve(args):
    run = get_run(args.run_id, args.project)
    if not run:
        print(t("cli.run_not_found_short", run_id=args.run_id))
        sys.exit(1)
    print(_resume_sync(run["project"], args.run_id, True))


def cmd_reject(args):
    run = get_run(args.run_id, args.project)
    if not run:
        print(t("cli.run_not_found_short", run_id=args.run_id))
        sys.exit(1)
    print(_resume_sync(run["project"], args.run_id, False))


def cmd_state(args):
    """pause|resume|disable <projekt> [loop] [--until ISO]."""
    state = {"pause": pause.PAUSED, "resume": pause.ACTIVE, "disable": pause.DISABLED}[args.cmd]
    try:
        pause.set_state(args.project, args.loop, state, until=args.until, actor=f"cli:{getpass.getuser()}")
    except ValueError as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    scope = f"{args.project}/{args.loop}" if args.loop else args.project
    print(f"✔ {scope}: {state}" + (t("cli.state_until", until=args.until) if args.until else ""))


def _read_password(args) -> str:
    """Read the password from stdin (``--password-stdin``) or interactively (twice)."""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    pw = getpass.getpass(t("cli.password_prompt"))
    if pw != getpass.getpass(t("cli.password_repeat")):
        raise auth.AuthError(t("cli.password_mismatch"))
    return pw


def cmd_user(args):
    """user add|passwd|role — lokale Benutzerverwaltung (ADR 0030)."""
    try:
        if args.action == "add":
            auth.add_user(args.name, _read_password(args), args.role or auth.VIEWER)
        elif args.action == "passwd":
            auth.set_password(args.name, _read_password(args))
        elif args.role:
            auth.set_role(args.name, args.role)
        else:
            raise auth.AuthError(t("cli.role_required"))
    except auth.AuthError as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    print(f"✔ user {args.action}: {args.name}")


def cmd_chat(args):
    # The local CLI caller is trusted (shell access) → audit as cli:<os-user> (ADR 0030).
    print(_chat(" ".join(args.message), who=auth.Principal(_cli_actor(), auth.ADMIN)))


def cmd_alert(args):
    print(_chat_alert_test(args.project))


def cmd_agent_env(_args):
    """Scrubbed agent env as `KEY=VALUE` lines (ADR 0050) — for a systemd EnvironmentFile
    or Docker `--env-file` of the (shared) `opencode serve` process."""
    from .agentenv import build_agent_env

    for k, v in sorted(build_agent_env().items()):
        if "\n" in v or "\r" in v:  # would tear off a line in the EnvironmentFile/--env-file
            print(t("cli.agent_env_skipped", key=k), file=sys.stderr)
            continue
        print(f"{k}={v}")


def cmd_egress(args):
    """egress start|stop|status — Default-Deny-Egress-Proxy (ADR 0051)."""
    from . import egress

    try:
        if args.action == "start":
            egress.start(egress.EgressConfig.from_env())
            print(egress.status())
        elif args.action == "stop":
            egress.stop()
            print(t("cli.egress_stopped"))
        else:
            print(egress.status())
    except RuntimeError as e:
        print(t("cli.error", error=e))
        sys.exit(1)


def cmd_import_runs(_args):
    from .sessionlog import import_runs

    if config.run_store_backend() == config.RUN_STORE_SQL:
        print(t("cli.import_runs_sql"))
        sys.exit(1)

    n = import_runs(config.RUNS_DIR)
    print(t("cli.import_runs_done", n=n))


def cmd_migrate_runs(args):
    from .runmigrate import migrate_runs
    from .sessionlog import db_path

    report = migrate_runs(
        config.RUNS_DIR,
        db_path(),
        args.db_url or config.db_url(),
        dry_run=args.dry_run,
        archive_dir=config.runs_archive_dir(),
    )
    print("\n".join(report.lines()))
    sys.exit(0 if report.ok else 1)


def cmd_retry_backfill(_args):
    """One-off backfill (ADR 0036, #290): chains for triggers whose
    runs have all failed — interrupted/transient → pending, the rest
    exhausted (UI "Filed away"). Idempotent: existing chains stay untouched."""
    s = retry_chains.backfill_chains()
    print(t("cli.backfill_done", pending=s.pending, exhausted=s.exhausted, skipped=s.skipped))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fabrik", description=t("cli.description"))
    ap.add_argument(
        "--profile",
        default=None,
        metavar="|".join(p.value for p in profiles.Profile),
        help=t("cli.help.profile", env=profiles.PROFILE_ENV, default=profiles.DEFAULT_PROFILE.value),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help=t("cli.help.list")).set_defaults(func=cmd_list)
    p = sub.add_parser("validate", help=t("cli.help.validate"))
    p.add_argument("specs", nargs="*", help=t("cli.help.validate_specs"))
    p.add_argument("--offline", action="store_true", help=t("cli.help.validate_offline"))
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("project", help=t("cli.help.project"))
    p.add_argument("action", choices=("add",))
    p.add_argument("path", help=t("cli.help.project_path"))
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("run", help=t("cli.help.run"))
    p.add_argument("project")
    p.add_argument("loop")
    p.add_argument("--note", default="")
    p.add_argument("--var", action="append", default=[], metavar="K=V", help=t("cli.help.run_var"))
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help=t("cli.help.replay"))
    p.add_argument("run_id")
    p.add_argument("--project", default=None, help=t("cli.help.replay_project"))
    p.add_argument("--yes", "-y", action="store_true", help=t("cli.help.replay_yes"))
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("runs", help=t("cli.help.runs"))
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=30)
    runs_sub = p.add_subparsers(dest="runs_cmd")
    ps = runs_sub.add_parser("show", help=t("cli.help.runs_show"))
    ps.add_argument("run_id")
    ps.add_argument("--project", default=None, dest="show_project")
    pe = runs_sub.add_parser("export", help=t("cli.help.runs_export"))
    pe.add_argument("run_id")
    pe.add_argument("--project", default=None, dest="export_project")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("serve", help=t("cli.help.serve"))
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.add_argument("--host", default=None, help=t("cli.help.serve_host"))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "mcp",
        help=t("cli.help.mcp"),
    )
    p.add_argument("--read-only", action="store_true", help=t("cli.help.mcp_read_only"))
    p.add_argument(
        "--allow-approvals",
        action="store_true",
        help=t("cli.help.mcp_allow_approvals"),
    )
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("approve", help=t("cli.help.approve"))
    p.add_argument("run_id")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help=t("cli.help.reject"))
    p.add_argument("run_id")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("migrate-runs", help=t("cli.help.migrate_runs"))
    p.add_argument("--dry-run", action="store_true", help=t("cli.help.migrate_dry_run"))
    p.add_argument("--db-url", help=t("cli.help.migrate_db_url"))
    p.set_defaults(func=cmd_migrate_runs)
    sub.add_parser("import-runs", help=t("cli.help.import_runs")).set_defaults(func=cmd_import_runs)

    sub.add_parser(
        "retry-backfill",
        help=t("cli.help.retry_backfill"),
    ).set_defaults(func=cmd_retry_backfill)

    for verb, hlp in (
        ("pause", t("cli.help.pause")),
        ("resume", t("cli.help.resume")),
        ("disable", t("cli.help.disable")),
    ):
        p = sub.add_parser(verb, help=hlp)
        p.add_argument("project")
        p.add_argument("loop", nargs="?", default=None)
        if verb != "resume":
            p.add_argument("--until", default=None, metavar="ISO", help=t("cli.help.until"))
        else:
            p.set_defaults(until=None)
        p.set_defaults(func=cmd_state)

    p = sub.add_parser("user", help=t("cli.help.user"))
    p.add_argument("action", choices=("add", "passwd", "role"))
    p.add_argument("name")
    p.add_argument("role", nargs="?", default=None, choices=auth.ROLES, help=t("cli.help.user_role"))
    p.add_argument("--password-stdin", action="store_true", help=t("cli.help.user_password_stdin"))
    p.set_defaults(func=cmd_user)

    p = sub.add_parser("chat", help=t("cli.help.chat"))
    p.add_argument("message", nargs="+")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("alert", help=t("cli.help.alert"))
    p.add_argument("test", nargs="?", default="test")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_alert)

    sub.add_parser(
        "agent-env",
        help=t("cli.help.agent_env"),
    ).set_defaults(func=cmd_agent_env)

    p = sub.add_parser("egress", help=t("cli.help.egress"))
    p.add_argument("action", choices=("start", "stop", "status"))
    p.set_defaults(func=cmd_egress)

    args = ap.parse_args(argv)
    try:
        # Composition root: resolve profile and adapters once at startup (ADR 0056)
        args.adapters = profiles.compose(profiles.resolve_profile(args.profile))
    except (profiles.ProfileError, NotImplementedError) as e:
        sys.exit(t("cli.error", error=e))
    args.func(args)


if __name__ == "__main__":
    main()
