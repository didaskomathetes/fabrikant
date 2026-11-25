"""CLI: list, validate, project add, run, replay, runs, serve, approve, reject, pause/resume/disable, chat, alert."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import traceback
from pathlib import Path

from . import auth, config, pause, profiles, retry_chains, scaffold, spec_proposals, spec_sources, specschema, templates
from .chat import _append_run_log, _approval_detail, _chat, _chat_alert_test, _revoke_approval
from .engine import reject_run, run_loop
from .failures import parse_kind
from .golden import (
    GoldenError,
    capture_golden,
    current_mark,
    eval_report,
    get_golden,
    golden_for_run,
    load_goldens,
    run_eval,
    save_golden,
)
from .i18n import t
from .replay import ReplayError, diff_runs, replay_run
from .runmodel import FAILED_RUN_STATUSES, RunStatus
from .runs import STEP_MARKS, claim_run, export_run, get_run, list_runs, show_run
from .runstore import STORE, PreconditionError
from .server import serve
from .specs import collect_projects, load_projects

# Exit codes of `golden` / `eval`: a real regression must be distinguishable from infrastructure errors.
EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2
EXIT_CODES_HELP = (
    "Exit codes: 0 = ok / no regression, 1 = regression, 2 = error or inconclusive (usage, infra, waiting)."
)
# Non-terminal eval outcomes: the run has not finished, so it proves nothing either way.
INCONCLUSIVE_RUN_STATUSES = frozenset({RunStatus.NEEDS_APPROVAL, RunStatus.QUEUED, RunStatus.RUNNING})


def cmd_init(args):
    """Scaffold a user project directory (ADR 0069)."""
    dest = Path(args.dir)
    created = scaffold.init_project(dest)
    for path in created:
        print(t("cli.init_created", path=path))
    if not created:
        print(t("cli.init_nothing", dir=dest.resolve()))
    print(t("cli.init_next", dir=dest.resolve()))


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
            for key in specschema.unknown_keys(spec):
                print(t("cli.unknown_spec_key", name=name, key=key))
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
    """project add <repo-pfad> — Zielrepo mit `.fabrikant/fabrikant.yaml` registrieren (ADR 0047)."""
    try:
        spec = spec_sources.register_target(args.path)
    except ValueError as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    print(t("cli.registered", spec=spec))


def cmd_template(args):
    """template list / template create — template library (ADR 0067)."""
    if args.template_cmd == "list":
        broken = False
        for tpl in templates.list_templates():
            if tpl.error:
                broken = True
                print(t("templates.list_error", name=tpl.name, source=tpl.source, error=tpl.error))
                continue
            print(t("templates.list_ok", name=tpl.name, source=tpl.source, description=tpl.description))
            print(t("templates.list_required", items=", ".join(tpl.required) or t("templates.list_none")))
            if tpl.defaults:
                print(t("templates.list_defaults", items=", ".join(tpl.defaults)))
        if broken:
            sys.exit(1)
        return
    # create
    project = args.project or args.name
    out = args.out or str(config.PROJECTS_DIR)
    try:
        path = templates.scaffold_spec(args.name, project, out, force=args.force)
    except (ValueError, FileExistsError) as e:
        print(t("cli.error", error=e))
        sys.exit(1)
    print(t("templates.created", path=path, project=project))


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


def cmd_golden(args):
    """Golden runs (roadmap #130): ``capture`` pins a golden, ``show`` lists them.

    Exit codes: ``EXIT_OK`` on success, ``EXIT_ERROR`` on usage/infrastructure errors."""
    try:
        if args.golden_cmd == "capture":
            g = capture_golden(args.project, args.loop, args.run_id)
            gname = args.name or args.loop
            other = current_mark(args.project, args.run_id)
            if other and other != gname and not getattr(args, "force", False):
                # A run carries at most one golden name (ADR 0068): re-marking would drop the other mark.
                print(t("cli.golden_other_mark", run=args.run_id, other=other, name=gname))
                sys.exit(EXIT_ERROR)
            p = save_golden(args.project, gname, g)
            print(f"✔ golden '{args.name or args.loop}' for {args.project}/{args.loop} from run {args.run_id} → {p}")
            return
        goldens = {args.name: get_golden(args.project, args.name)} if args.name else load_goldens(args.project)
    except GoldenError as e:
        print(f"Error: {e}")
        sys.exit(EXIT_ERROR)
    if not goldens:
        print(f"(no goldens for {args.project})")
        return
    for n, g in goldens.items():
        exp = g.get("expect", {})
        print(f"{n}  loop={g.get('loop')}  baseline={g.get('baseline_run')}  spec={str(g.get('spec_hash', ''))[:12]}")
        for s in exp.get("steps", []):
            print(f"    - {s.get('name')} [{s.get('status')}]  verdict={s.get('verdict') or '—'}")
        print(f"    status={exp.get('status')}")


def _confirm_eval(args) -> bool:
    """Confirm before the real eval re-run: ``--yes`` skips; without a TTY and without ``--yes`` it aborts."""
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("Aborted: no interactive input — confirm explicitly with --yes/-y.")
        return False
    return input(f"Re-run loop {args.project}/{args.loop} for real now (eval)? [y/N] ").strip().lower() in (
        "y",
        "yes",
        "j",
        "ja",
    )


def cmd_eval(args):
    """Eval (roadmap #130): compare a loop re-run against its golden baseline.

    Two forms: ``fabrikant eval <project> <loop> [--name NAME]`` or
    ``fabrikant eval --golden <run-id>`` (resolves project/loop/name from the run
    store's golden mark, ADR 0068).

    Exit codes: ``EXIT_OK`` no regression, ``EXIT_REGRESSION`` real diffs against the golden,
    ``EXIT_ERROR`` infrastructure/usage problem or inconclusive run (e.g. waiting for approval)."""
    golden_run = getattr(args, "golden_run", None)
    if golden_run is not None and not golden_run.strip():
        print(t("cli.eval_empty_golden"))
        sys.exit(EXIT_ERROR)
    if golden_run and (args.project or args.loop or args.name):
        print("Error: --golden RUN_ID resolves project/loop/name itself; do not combine it with them")
        sys.exit(EXIT_ERROR)
    try:
        if golden_run:
            resolved = golden_for_run(golden_run)
            if resolved is None:
                print(f"Error: run {golden_run!r} is unknown or not marked as a golden")
                sys.exit(EXIT_ERROR)
            project, name = resolved
            g = get_golden(project, name)
            if g.get("baseline_run") != golden_run:  # the mark is stale: the golden was re-captured
                print(
                    f"Error: run {golden_run!r} is no longer the baseline of golden {name!r} "
                    f"(current baseline: {g.get('baseline_run')!r})"
                )
                sys.exit(EXIT_ERROR)
            loop = g["loop"]
        else:
            if not (args.project and args.loop):
                print("Error: provide `project loop` or `--golden RUN_ID`")
                sys.exit(EXIT_ERROR)
            project, loop = args.project, args.loop
            name = args.name or loop
            g = get_golden(project, name)
            loop = g["loop"]
    except (GoldenError, KeyError) as e:
        print(f"Error: {e}")
        sys.exit(EXIT_ERROR)
    blocked = pause.manual_block_reason(project, loop)
    if blocked:
        print(f"Error: {blocked}")
        sys.exit(EXIT_ERROR)
    print(REPLAY_SIDE_EFFECT_NOTICE)
    if not _confirm_eval(args):
        print("Aborted.")
        sys.exit(EXIT_ERROR)
    claim = claim_run(project, loop, None)
    if claim is None:
        print(f"⚠ {project}: parallel run limit reached or {loop} is already running.")
        sys.exit(EXIT_ERROR)
    try:
        golden, baseline, record, diffs = run_eval(project, loop, name, actor=_cli_actor(), held_scope=claim.scope)
    except (GoldenError, ReplayError) as e:
        print(f"Error: {e}")
        sys.exit(EXIT_ERROR)
    except Exception:
        # Keep the traceback visible, but never let a crash look like a regression (exit 1).
        traceback.print_exc()
        sys.exit(EXIT_ERROR)
    finally:
        claim.release()
    print(f"eval  {record['project']}/{record['loop']}  ({record['id']})  [golden {name}]")
    _print_steps(record)
    print()
    print(eval_report(golden, baseline, record, diffs))
    if record.get("status") in INCONCLUSIVE_RUN_STATUSES:
        print(f"inconclusive: eval run is waiting for approval (status {record.get('status')})")
        sys.exit(EXIT_ERROR)
    sys.exit(EXIT_REGRESSION if diffs else EXIT_OK)


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
    except (auth.AuthError, ValueError) as e:
        print(t("cli.error", error=e))
        sys.exit(1)


def cmd_mcp(args):
    # fabrikant as an MCP stdio server (Roadmap #132): runs, specs, approvals for
    # any MCP-capable agent. Long-lived process — the client keeps the pipe open.
    # Imported lazily: the MCP SDK is heavy and only this subcommand needs it.
    from . import mcp_server

    options = mcp_server.McpOptions.from_env(
        read_only=args.read_only,
        allow_approvals=args.allow_approvals,
        allow_spec_write=args.allow_spec_write,
    )
    mcp_server.serve_stdio(options)


def cmd_proposals(args):
    """Human side of MCP spec-write: list / show / apply / reject spec-change proposals."""
    actor = _cli_actor()
    try:
        if args.action == "list":
            for pr in spec_proposals.list_proposals():
                print(f"{pr.id}  {pr.project}  {pr.status}  by {pr.proposed_by}  {', '.join(pr.changes)}")
        elif args.action == "show":
            pr = spec_proposals.get_proposal(args.id)
            print(json.dumps(pr.to_dict(), indent=2, ensure_ascii=False))
        elif args.action == "apply":
            if not args.yes:
                # An applied patch can add shell steps (= code execution): show it first.
                pr = spec_proposals.get_proposal(args.id)
                print(json.dumps(pr.to_dict(), indent=2, ensure_ascii=False))
                print(t("cli.proposal_confirm", id=pr.id))
                sys.exit(1)
            pr = spec_proposals.apply(args.id, actor)
            print(t("cli.proposal_applied", id=pr.id, project=pr.project))
        else:
            pr = spec_proposals.reject(args.id, actor)
            print(t("cli.proposal_rejected", id=pr.id, project=pr.project))
    except spec_proposals.ProposalError as e:
        print(t("cli.error", error=e))
        sys.exit(1)


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


def cmd_fetch(args):
    """Research-Fetcher (ADR 0060): Seite holen, bereinigen, Injection-Scan,
    Quelle/Quarantäne schreiben + Herkunftsprotokoll. Auto-Freigaben; Profile
    via ``--spec``/``--loop`` (Stufe 3)."""
    from .fetch import DetectorUnavailable, Fetcher, build_detectors, fetcher_config_for, resolve_profiles
    from .specs import load_spec_raw

    domains = [d.strip() for d in (args.approved_domains or "").split(",") if d.strip()]
    urls = [u.strip() for u in (args.approved_urls or "").split(",") if u.strip()]
    loop_note = None
    if args.spec:
        spec = load_spec_raw(Path(args.spec))
        loop = args.loop or "relay"
        profs = resolve_profiles(spec, loop)
        for d in fetcher_config_for(profs)["approved_domains"]:
            if d not in domains:
                domains.append(d)
        loop_note = f"loop={loop} profile={[p.name for p in profs]}"
    try:
        detectors = build_detectors(
            [n.strip() for n in args.detectors.split(",") if n.strip()] if args.detectors else None
        )
    except (ValueError, DetectorUnavailable) as e:
        print(f"Fehler: {e}")
        sys.exit(1)
    fetcher = Fetcher(
        approved_domains=domains, approved_urls=urls, out_dir=args.out, ledger_path=args.ledger, detectors=detectors
    )
    res = fetcher.fetch(args.url, requested_by=_cli_actor())
    if args.json:
        print(
            json.dumps(
                {
                    "url": res.url,
                    "status": res.status,
                    "reason": res.reason,
                    "content_type": res.content_type,
                    "fetched_at": res.fetched_at,
                    "source_path": str(res.source_path) if res.source_path else None,
                    "findings": [{"rule": f.rule, "severity": f.severity, "snippet": f.snippet} for f in res.findings],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"{res.status.upper():<14} {res.url}")
        if loop_note:
            print(f"  {loop_note}")
        if res.reason:
            print(f"  reason: {res.reason}")
        if res.findings:
            print(f"  findings: {len(res.findings)}")
            for f in res.findings:
                print(f"    {f.severity:<11} {f.rule}: {f.snippet}")
        if res.source_path:
            print(f"  -> {res.source_path}")
    sys.exit(0 if res.status == "clean" else 1)


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
    ap = argparse.ArgumentParser(prog="fabrikant", description=t("cli.description"))
    ap.add_argument(
        "--profile",
        default=None,
        metavar="|".join(p.value for p in profiles.Profile),
        help=t("cli.help.profile", env=profiles.PROFILE_ENV, default=profiles.DEFAULT_PROFILE.value),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help=t("cli.help.init"))
    p.add_argument("dir", nargs="?", default=".", help=t("cli.help.init_dir"))
    p.set_defaults(func=cmd_init)
    sub.add_parser("list", help=t("cli.help.list")).set_defaults(func=cmd_list)
    p = sub.add_parser("validate", help=t("cli.help.validate"))
    p.add_argument("specs", nargs="*", help=t("cli.help.validate_specs"))
    p.add_argument("--offline", action="store_true", help=t("cli.help.validate_offline"))
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("project", help=t("cli.help.project"))
    p.add_argument("action", choices=("add",))
    p.add_argument("path", help=t("cli.help.project_path"))
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("template", help=t("cli.help.template"))
    tpl_sub = p.add_subparsers(dest="template_cmd", required=True)
    tpl_sub.add_parser("list", help=t("cli.help.template_list"))
    tc = tpl_sub.add_parser("create", help=t("cli.help.template_create"))
    tc.add_argument("name", help=t("cli.help.template_name"))
    tc.add_argument("--project", help=t("cli.help.template_project"))
    tc.add_argument("--out", help=t("cli.help.template_out"))
    tc.add_argument("--force", action="store_true", help=t("cli.help.template_force"))
    p.set_defaults(func=cmd_template)

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

    p = sub.add_parser("golden", help=t("cli.help.golden"), epilog=EXIT_CODES_HELP)
    g = p.add_subparsers(dest="golden_cmd", required=True)
    gc = g.add_parser("capture", help=t("cli.help.golden_capture"))
    gc.add_argument("project")
    gc.add_argument("loop")
    gc.add_argument("run_id", help="Id des abgeschlossenen Baseline-Runs")
    gc.add_argument("--name", default=None, help=t("cli.help.golden_name"))
    gc.add_argument("--force", action="store_true", help=t("cli.help.golden_force"))
    gs = g.add_parser("show", help=t("cli.help.golden_show"))
    gs.add_argument("project")
    gs.add_argument("name", nargs="?", default=None, help="ein bestimmtes Golden (Default: alle)")
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser("eval", help=t("cli.help.eval"), epilog=EXIT_CODES_HELP)
    p.add_argument("project", nargs="?", default=None, help=t("cli.help.eval_project"))
    p.add_argument("loop", nargs="?", default=None, help=t("cli.help.eval_loop"))
    p.add_argument("--name", default=None, help=t("cli.help.golden_name"))
    p.add_argument(
        "--golden",
        dest="golden_run",
        default=None,
        metavar="RUN_ID",
        help=t("cli.help.eval_golden"),
    )
    p.add_argument("--yes", "-y", action="store_true", help="Rückfrage überspringen (Pflicht ohne TTY)")
    p.set_defaults(func=cmd_eval)

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
    p.add_argument("--port", type=int, default=None, help=t("cli.help.serve_port"))
    p.add_argument("--host", default=None, help=t("cli.help.serve_host"))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "mcp",
        help=t("cli.help.mcp"),
    )
    p.add_argument("--read-only", action="store_true", help=t("cli.help.mcp_read_only"))
    p.add_argument("--allow-approvals", action="store_true", help=t("cli.help.mcp_allow_approvals"))
    p.add_argument("--allow-spec-write", action="store_true", help=t("cli.help.mcp_allow_spec_write"))
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("proposals", help=t("cli.help.proposals"))
    p.add_argument("action", choices=["list", "show", "apply", "reject"])
    p.add_argument("id", nargs="?", default="")
    p.add_argument("--yes", action="store_true", help=t("cli.help.proposals_yes"))
    p.set_defaults(func=cmd_proposals)

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

    p = sub.add_parser("fetch", help="Research-Fetcher: Seite holen, bereinigen, Injection-Scan (ADR 0060)")
    p.add_argument("url", help="URL zu holen (braucht Auto-Freigabe)")
    p.add_argument("--approved-domains", default="", help="Kommagetrennte Domains (Auto-Freigabe)")
    p.add_argument("--approved-urls", default="", help="Kommagetrennte exakte URLs (Auto-Freigabe)")
    p.add_argument("--out", default=None, help="Ausgabe-Dir (sources/ + quarantine/)")
    p.add_argument("--ledger", default=None, help="Herkunftsprotokoll (JSONL)")
    p.add_argument("--spec", default=None, help="Projekt-Spec (YAML) mit access-Profilen")
    p.add_argument("--loop", default=None, help="Loop/Run-Name für Profil-Resolution (Default: relay)")
    p.add_argument(
        "--detectors",
        default=None,
        help="Kommagetrennte Injection-Detektoren: regex (Default), llm-guard (Extra injection-llm-guard); "
        "sonst FABRIKANT_FETCH_DETECTORS",
    )
    p.add_argument("--json", action="store_true", help="Ergebnis als JSON")
    p.set_defaults(func=cmd_fetch)

    args = ap.parse_args(argv)
    try:
        # Composition root: resolve profile and adapters once at startup (ADR 0056)
        args.adapters = profiles.compose(profiles.resolve_profile(args.profile))
    except (profiles.ProfileError, NotImplementedError) as e:
        sys.exit(t("cli.error", error=e))
    args.func(args)


if __name__ == "__main__":
    main()
