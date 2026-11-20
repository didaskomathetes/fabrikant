"""CLI: list, validate, project add, run, replay, runs, serve, approve, reject, pause/resume/disable, chat, alert."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import auth, config, pause, profiles, retry_chains, spec_sources
from .chat import _append_run_log, _approval_detail, _chat, _chat_alert_test, _revoke_approval
from .engine import reject_run, run_loop
from .failures import parse_kind
from .replay import ReplayError, diff_runs, replay_run
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
    """Specs prüfen; Exit 1 bei Fehlern. ``specs``: nur diese Dateien statt aller Quellen,
    ``--offline``: ohne host-abhängige Prüfungen (Projekt-``root`` muss nicht existieren — CI)."""
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
            print(f"✔ {name} ({len(spec['loops'])} Loops)")
            if not args.offline:
                _warn_connection_refs(name, spec)
    sys.exit(0 if ok else 1)


def _warn_connection_refs(name: str, spec: dict) -> None:
    """Unbekannte ``connection:``-Namen als Warnung (Verbindungen sind Host-Zustand, ADR 0052)."""
    from . import connections

    try:
        problems = connections.unknown_references(spec)
    except Exception as e:  # DB nicht erreichbar o. Ä.: validate soll daran nicht scheitern
        print(f"  ⚠ {name}: Verbindungen nicht prüfbar ({e})")
        return
    for msg in problems:
        print(f"  ⚠ {name}: {msg}")


def cmd_project(args):
    """project add <repo-pfad> — Zielrepo mit `.fabrik/fabrik.yaml` registrieren (ADR 0047)."""
    try:
        spec = spec_sources.register_target(args.path)
    except ValueError as e:
        print(f"Fehler: {e}")
        sys.exit(1)
    print(f"✔ registriert: {spec}")


def _cli_actor() -> str:
    """Audit-Name lokaler CLI-Aufrufer (``started_by``/``approved_by``)."""
    return f"cli:{getpass.getuser()}"


def cmd_run(args):
    vars = {}
    for kv in args.var:
        if "=" not in kv:
            print(f"Fehler: --var erwartet K=V, ist '{kv}'")
            sys.exit(1)
        k, v = kv.split("=", 1)
        vars[k] = v
    blocked = pause.manual_block_reason(args.project, args.loop)
    if blocked:
        print(f"Fehler: {blocked}")
        sys.exit(1)
    claim = claim_run(args.project, args.loop, vars or None)
    if claim is None:
        print(f"⚠ {args.project}: Limit paralleler Runs erreicht oder {args.loop} läuft bereits.")
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
        print(f"Fehler: {e}")
        sys.exit(1)
    finally:
        claim.release()
    print(f"{rec['status'].upper()}  {rec['project']}/{rec['loop']}  ({rec['id']})")
    _print_steps(rec)
    if rec["status"] == "needs_approval":
        print(_approval_detail(rec.get("approval") or {}))
    sys.exit(0 if rec["status"] == "pass" else 1)


def _print_steps(rec: dict) -> None:
    """Step-Liste eines Run-Records mit Status-Marke ausgeben."""
    for s in rec["steps"]:
        print(f"  {STEP_MARKS.get(s['status'], '?')} {s['name']} [{s['type']}] {s.get('summary', '')}")


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
    claim = claim_run(project, loop, None)
    if claim is None:
        print(f"⚠ {project}: Limit paralleler Runs erreicht oder {loop} läuft bereits.")
        sys.exit(1)
    try:
        orig, rec = replay_run(args.run_id, project=project, actor=_cli_actor(), held_scope=claim.scope)
    except ReplayError as e:
        print(f"Fehler: {e}")
        sys.exit(1)
    except Exception as e:
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
            print(f"✖ Run {args.run_id} nicht gefunden")
            sys.exit(1)
        print(out)
        return
    if args.runs_cmd == "export":
        rec = export_run(args.run_id, args.export_project)
        if rec is None:
            print(f"✖ Run {args.run_id} nicht gefunden")
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
        print(f"Fehler: {e}")
        sys.exit(1)


def _resume_sync(project: str, run_id: str, approve: bool) -> str:
    """Synchrones Resume für die CLI (Daemon-Threads würden mit dem Prozess sterben)."""
    run = get_run(run_id, project)
    if not run:
        return f"✖ Run {run_id} nicht gefunden."
    if run["status"] != "needs_approval":
        return f"✖ Run {run['id']} ist '{run['status']}', nicht needs_approval."
    detail = _approval_detail(run.get("approval") or {})
    if not approve:
        # Atomare Ablehnung (schließt einen gleichzeitigen Resume aus, ADR 0034) inkl. Cleanup
        # und Retry-Ketten-Buchung.
        try:
            run = reject_run(project, run, _cli_actor(), lambda msg: _append_run_log(project, run_id, msg))
        except PreconditionError as e:
            return f"⚠ {run['id']} konnte nicht abgelehnt werden: {e}"
        return f"⊘ {run['id']} abgelehnt — der Loop endet hier.{detail}"
    claim = claim_run(project, run["loop"], run.get("vars"), exclude_run_id=run_id)
    if claim is None:
        return f"⚠ {project}: Limit paralleler Runs erreicht, {run['loop']} läuft bereits oder wartet auf Freigabe."
    try:
        try:
            STORE.approve(project, run_id, run, _cli_actor())
        except PreconditionError as e:
            return f"⚠ {run['id']} konnte nicht freigegeben werden: {e}"
        rec = run_loop(project, run["loop"], trigger="approve", resume_id=run_id, held_scope=claim.scope)
        _print_steps(rec)
        return f"■ {rec['status'].upper()} — {run['loop']}"
    except Exception as e:
        # Resume hat nicht begonnen (z. B. Spec defekt) → Freigabe zurücknehmen, damit der Run
        # erneut freigegeben oder abgelehnt werden kann.
        _revoke_approval(project, run_id)
        return f"✖ Resume fehlgeschlagen: {e}"
    finally:
        claim.release()


def cmd_approve(args):
    run = get_run(args.run_id, args.project)
    if not run:
        print(f"✖ Run {args.run_id} nicht gefunden")
        sys.exit(1)
    print(_resume_sync(run["project"], args.run_id, True))


def cmd_reject(args):
    run = get_run(args.run_id, args.project)
    if not run:
        print(f"✖ Run {args.run_id} nicht gefunden")
        sys.exit(1)
    print(_resume_sync(run["project"], args.run_id, False))


def cmd_state(args):
    """pause|resume|disable <projekt> [loop] [--until ISO]."""
    state = {"pause": pause.PAUSED, "resume": pause.ACTIVE, "disable": pause.DISABLED}[args.cmd]
    try:
        pause.set_state(args.project, args.loop, state, until=args.until, actor=f"cli:{getpass.getuser()}")
    except ValueError as e:
        print(f"Fehler: {e}")
        sys.exit(1)
    scope = f"{args.project}/{args.loop}" if args.loop else args.project
    print(f"✔ {scope}: {state}" + (f" bis {args.until}" if args.until else ""))


def _read_password(args) -> str:
    """Passwort aus stdin (``--password-stdin``) oder interaktiv (zweimal) lesen."""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    pw = getpass.getpass("Passwort: ")
    if pw != getpass.getpass("Wiederholen: "):
        raise auth.AuthError("Passwörter stimmen nicht überein")
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
            raise auth.AuthError("Rolle nötig: fabrik user role <name> <viewer|operator|admin>")
    except auth.AuthError as e:
        print(f"Fehler: {e}")
        sys.exit(1)
    print(f"✔ user {args.action}: {args.name}")


def cmd_chat(args):
    # Lokaler CLI-Aufrufer ist vertrauenswürdig (Shell-Zugriff) → Audit als cli:<os-user> (ADR 0030).
    print(_chat(" ".join(args.message), who=auth.Principal(_cli_actor(), auth.ADMIN)))


def cmd_alert(args):
    print(_chat_alert_test(args.project))


def cmd_agent_env(_args):
    """Gescrubte Agent-Env als `KEY=VALUE`-Zeilen (ADR 0050) — für systemd-EnvironmentFile
    oder Docker `--env-file` des (geteilten) `opencode serve`-Prozesses."""
    from .agentenv import build_agent_env

    for k, v in sorted(build_agent_env().items()):
        if "\n" in v or "\r" in v:  # würde im EnvironmentFile/--env-file eine Zeile abreißen
            print(f"agent-env: {k} übersprungen (Zeilenumbruch im Wert)", file=sys.stderr)
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
            print("✔ egress-proxy gestoppt")
        else:
            print(egress.status())
    except RuntimeError as e:
        print(f"Fehler: {e}")
        sys.exit(1)


def cmd_import_runs(_args):
    from .sessionlog import import_runs

    if config.run_store_backend() == config.RUN_STORE_SQL:
        print("✖ import-runs füllt nur die alte sessionlog.db — im SQL-Store: `fabrik migrate-runs`")
        sys.exit(1)

    n = import_runs(config.RUNS_DIR)
    print(f"✔ {n} Runs importiert (bestehende Sessions übersprungen)")


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
    """Einmaliger Bestandsabgleich (ADR 0036, #290): Ketten für Trigger, deren
    komplette Runs gescheitert sind — interrupted/transient → pending, der Rest
    exhausted (UI „Abgelegt“). Idempotent: vorhandene Ketten bleiben unberührt."""
    s = retry_chains.backfill_chains()
    print(f"✔ Bestandsabgleich: {s.pending} pending, {s.exhausted} exhausted, {s.skipped} übersprungen")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fabrik", description="die Software-Fabrik")
    ap.add_argument(
        "--profile",
        default=None,
        metavar="|".join(p.value for p in profiles.Profile),
        help=f"Deployment-Profil (Default: ${profiles.PROFILE_ENV} oder {profiles.DEFAULT_PROFILE.value})",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Projekte + Loops anzeigen").set_defaults(func=cmd_list)
    p = sub.add_parser("validate", help="Specs prüfen")
    p.add_argument("specs", nargs="*", help="nur diese Spec-Dateien (Default: alle Quellen)")
    p.add_argument("--offline", action="store_true", help="ohne host-abhängige Prüfungen (root-Ordner) — CI")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("project", help="Zielrepo-Spec registrieren (project add <pfad>, ADR 0047)")
    p.add_argument("action", choices=("add",))
    p.add_argument("path", help="Repo-Wurzel mit .fabrik/fabrik.yaml")
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("run", help="Loop ausführen")
    p.add_argument("project")
    p.add_argument("loop")
    p.add_argument("--note", default="")
    p.add_argument("--var", action="append", default=[], metavar="K=V", help="Initiale Variable (mehrfach erlaubt)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help="Abgeschlossenen Run mit gepinnten Inputs neu ausführen (ADR 0061)")
    p.add_argument("run_id")
    p.add_argument("--project", default=None, help="Projekt (sonst aus dem Run abgeleitet)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("runs", help="Run-Historie (show/export: Einzelrun + Session-Log)")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=30)
    runs_sub = p.add_subparsers(dest="runs_cmd")
    ps = runs_sub.add_parser("show", help="Einzelrun anzeigen (Felder, Steps, Session-Log)")
    ps.add_argument("run_id")
    ps.add_argument("--project", default=None, dest="show_project")
    pe = runs_sub.add_parser("export", help="Einzelrun + Session-Log als JSON")
    pe.add_argument("run_id")
    pe.add_argument("--project", default=None, dest="export_project")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("serve", help="Engine + UI starten")
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.add_argument(
        "--host", default=None, help="Bind-Adresse (Default 127.0.0.1 / FABRIK_HOST; sonst FABRIK_AUTH=local)"
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("approve", help="needs_approval-Run freigeben (setzt fort)")
    p.add_argument("run_id")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="needs_approval-Run ablehnen (endet)")
    p.add_argument("run_id")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("migrate-runs", help="Run-JSON + Session-Log → SQL-Run-Store (ADR 0034, idempotent)")
    p.add_argument("--dry-run", action="store_true", help="Probelauf: nichts schreiben, Abweichungen listen")
    p.add_argument("--db-url", help="Ziel (Default: FABRIK_DB_URL bzw. state/fabrik.db)")
    p.set_defaults(func=cmd_migrate_runs)
    sub.add_parser("import-runs", help="P0-Run-JSONs → SQLite-Session-Log (ADR 0002)").set_defaults(
        func=cmd_import_runs
    )

    sub.add_parser(
        "retry-backfill",
        help="Ketten für komplett gescheiterte Trigger anlegen (einmalig, idempotent, ADR 0036)",
    ).set_defaults(func=cmd_retry_backfill)

    for verb, hlp in (
        ("pause", "Trigger anhalten (manuelle Runs bleiben erlaubt)"),
        ("resume", "Projekt/Loop fortsetzen"),
        ("disable", "Trigger UND manuelle Runs sperren"),
    ):
        p = sub.add_parser(verb, help=hlp)
        p.add_argument("project")
        p.add_argument("loop", nargs="?", default=None)
        if verb != "resume":
            p.add_argument("--until", default=None, metavar="ISO", help="Auto-Resume-Zeitpunkt (ISO 8601, sonst UTC)")
        else:
            p.set_defaults(until=None)
        p.set_defaults(func=cmd_state)

    p = sub.add_parser("user", help="Benutzer verwalten (FABRIK_AUTH=local, ADR 0030)")
    p.add_argument("action", choices=("add", "passwd", "role"))
    p.add_argument("name")
    p.add_argument("role", nargs="?", default=None, choices=auth.ROLES, help="Rolle (add: Default viewer)")
    p.add_argument("--password-stdin", action="store_true", help="Passwort aus stdin lesen (Automation)")
    p.set_defaults(func=cmd_user)

    p = sub.add_parser("chat", help="Chat-Befehl / LLM-Copilot")
    p.add_argument("message", nargs="+")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("alert", help="Alert-Kanäle testen (alert test [--project X])")
    p.add_argument("test", nargs="?", default="test")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_alert)

    sub.add_parser(
        "agent-env",
        help="Gescrubte Agent-Env als KEY=VALUE ausgeben (ADR 0050, für opencode-serve)",
    ).set_defaults(func=cmd_agent_env)

    p = sub.add_parser("egress", help="Default-Deny-Egress-Proxy (ADR 0051)")
    p.add_argument("action", choices=("start", "stop", "status"))
    p.set_defaults(func=cmd_egress)

    args = ap.parse_args(argv)
    try:
        # Composition Root: Profil und Adapter einmal beim Start auflösen (ADR 0056)
        args.adapters = profiles.compose(profiles.resolve_profile(args.profile))
    except (profiles.ProfileError, NotImplementedError) as e:
        sys.exit(f"Fehler: {e}")
    args.func(args)


if __name__ == "__main__":
    main()
