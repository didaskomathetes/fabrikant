"""CLI: list, validate, run, runs, approve, reject, agent-env, chat, alert, import-runs, main()."""

from __future__ import annotations

import argparse
import json

import pytest

from fabrikant import cli, retry_chains


def _write_spec(projects_dir, name="p", root=None, loops=None):
    """Minimale gültige Spec schreiben."""
    root = root or projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    loops = loops or {"l": {"steps": [{"type": "shell", "cmd": "true"}]}}
    (projects_dir / f"{name}.fabrikant.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n"
        + "\n".join(
            f"  {ln}:\n    steps:\n" + "\n".join(f"      - {json.dumps(s)}" for s in ldef["steps"])
            for ln, ldef in loops.items()
        )
    )


def _write_run(runs_dir, project, rid, status="pass", loop="l"):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": rid, "project": project, "loop": loop, "status": status, "started": "2024-01-01T00:00:00Z"}
    (d / f"{rid}.json").write_text(json.dumps(rec))
    return rec


# ---------------------------------------------------------------- list
def test_cmd_list(projects_dir, tmp_path, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    cli.cmd_list(None)
    out = capsys.readouterr().out
    assert "p" in out
    assert "l" in out


def test_cmd_list_with_error(projects_dir, capsys):
    (projects_dir / "bad.fabrikant.yaml").write_text("name: bad\nroot: /nonexistent-xyz\nloops: {}\n")
    cli.cmd_list(None)
    out = capsys.readouterr().out
    assert "✖" in out


# ---------------------------------------------------------------- validate
def _validate_args(specs=None, offline=False):
    """argparse-Namespace wie `fabrikant validate` (main() setzt beide Attribute)."""
    return argparse.Namespace(specs=specs or [], offline=offline)


def test_cmd_validate_ok(projects_dir, tmp_path, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    with pytest.raises(SystemExit) as e:
        cli.cmd_validate(_validate_args())
    assert e.value.code == 0
    assert "✔" in capsys.readouterr().out


def test_cmd_validate_error(projects_dir, capsys):
    (projects_dir / "bad.fabrikant.yaml").write_text("name: bad\nroot: /nonexistent-xyz\nloops: {}\n")
    with pytest.raises(SystemExit) as e:
        cli.cmd_validate(_validate_args())
    assert e.value.code == 1
    assert "✖" in capsys.readouterr().out


def test_cmd_validate_files_offline(tmp_path, capsys):
    """CI-Modus: nur die übergebenen Specs, root muss nicht existieren."""
    spec = tmp_path / "ci.fabrikant.yaml"
    spec.write_text("name: ci\nroot: /nonexistent-xyz\nloops:\n  l:\n    steps:\n      - {type: shell, cmd: 'true'}\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--offline", str(spec)])
    assert e.value.code == 0
    assert "✔ ci" in capsys.readouterr().out


def test_cmd_validate_files_without_offline_checks_root(tmp_path, capsys):
    spec = tmp_path / "ci.fabrikant.yaml"
    spec.write_text("name: ci\nroot: /nonexistent-xyz\nloops:\n  l:\n    steps:\n      - {type: shell, cmd: 'true'}\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", str(spec)])
    assert e.value.code == 1
    assert "root fehlt" in capsys.readouterr().out


def _agent_spec(tmp_path, connection: str):
    spec = tmp_path / "c.fabrikant.yaml"
    spec.write_text(
        f"name: c\nroot: {tmp_path}\nloops:\n  l:\n    steps:\n"
        f"      - {{type: agent, name: dev, connection: {connection}, prompt: x}}\n"
    )
    return spec


def test_cmd_validate_warns_on_unknown_connection(tmp_path, capsys):
    """Unbekannte `connection:` ist Host-Zustand → Warnung, kein Fehler-Exit (ADR 0052)."""
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", str(_agent_spec(tmp_path, "ocx"))])
    assert e.value.code == 0
    assert "⚠ c: l/dev: Agent-Verbindung 'ocx' unbekannt" in capsys.readouterr().out


def test_cmd_validate_warns_on_unknown_keys(tmp_path, capsys):
    """Unknown keys have no effect → warning, exit code unchanged."""
    spec = tmp_path / "u.fabrikant.yaml"
    spec.write_text("name: u\nroot: /x\nloops:\n  l:\n    steps:\n      - {type: shell, cmd: 'true', sandboxd: true}\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--offline", str(spec)])
    assert e.value.code == 0
    assert "⚠ u: unbekannter Spec-Schlüssel loops/l/steps/0: sandboxd (wird ignoriert)" in capsys.readouterr().out


def test_cmd_validate_offline_skips_connection_check(tmp_path, capsys, monkeypatch):
    from fabrikant import connections

    monkeypatch.setattr(connections, "unknown_references", lambda spec: pytest.fail("offline: keine Host-Prüfung"))
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "--offline", str(_agent_spec(tmp_path, "ocx"))])
    assert e.value.code == 0


def test_cmd_validate_survives_unreachable_connection_store(tmp_path, capsys, monkeypatch):
    from fabrikant import connections

    def boom(spec):
        raise RuntimeError("db down")

    monkeypatch.setattr(connections, "unknown_references", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", str(_agent_spec(tmp_path, "oc"))])
    assert e.value.code == 0
    assert "Verbindungen nicht prüfbar (db down)" in capsys.readouterr().out


# ---------------------------------------------------------------- run
def test_cmd_run_pass(projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    calls = []

    def _run_loop(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "status": "pass",
            "project": "p",
            "loop": "l",
            "id": "r1",
            "steps": [{"name": "s", "type": "shell", "status": "pass", "summary": "ok"}],
        }

    monkeypatch.setattr(
        cli,
        "run_loop",
        _run_loop,
    )
    with pytest.raises(SystemExit) as e:
        cli.cmd_run(type("A", (), {"project": "p", "loop": "l", "note": "", "var": []})())
    assert e.value.code == 0
    assert "PASS" in capsys.readouterr().out
    assert calls[0][1]["held_scope"] == "p"
    free = cli.claim_run("p", "other")
    assert free is not None, "Claim muss nach dem Run freigegeben sein"
    free.release()


def test_cmd_run_rejects_busy_lock(projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    lock = cli.claim_run("p", "other")  # repo-lose Spec → max_parallel 1 (ADR 0028)
    assert lock is not None
    try:
        with pytest.raises(SystemExit) as e:
            cli.cmd_run(type("A", (), {"project": "p", "loop": "l", "note": "", "var": []})())
        assert e.value.code == 1
        out = capsys.readouterr().out
        assert "läuft bereits" in out
        assert "exclusive_group" in out  # also names the exclusive_group block reason
    finally:
        lock.release()


def test_cmd_run_needs_approval(projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    monkeypatch.setattr(
        cli,
        "run_loop",
        lambda *a, **k: {
            "status": "needs_approval",
            "project": "p",
            "loop": "l",
            "id": "r1",
            "steps": [],
            "approval": {"what": "was"},
        },
    )
    with pytest.raises(SystemExit) as e:
        cli.cmd_run(type("A", (), {"project": "p", "loop": "l", "note": "", "var": []})())
    assert e.value.code == 1
    assert "was" in capsys.readouterr().out


def test_cmd_run_bad_var(projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    with pytest.raises(SystemExit) as e:
        cli.cmd_run(type("A", (), {"project": "p", "loop": "l", "note": "", "var": ["noequals"]})())
    assert e.value.code == 1
    assert "K=V" in capsys.readouterr().out


def test_cmd_run_exception(projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")

    def _boom(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "run_loop", _boom)
    with pytest.raises(SystemExit) as e:
        cli.cmd_run(type("A", (), {"project": "p", "loop": "l", "note": "", "var": []})())
    assert e.value.code == 1
    assert "kaputt" in capsys.readouterr().out


# ---------------------------------------------------------------- runs
def test_cmd_runs(runs_dir, capsys):
    _write_run(runs_dir, "p", "r1", "pass")
    _write_run(runs_dir, "p", "r2", "fail")
    cli.cmd_runs(type("A", (), {"project": "p", "limit": 30, "runs_cmd": None})())
    out = capsys.readouterr().out
    assert "r1" in out
    assert "r2" in out
    # Fehlerklasse nur an gescheiterten Runs; Altbestand ohne Feld = unknown (ADR 0035)
    lines = {line.split()[0]: line for line in out.splitlines()}
    assert lines["r2"].endswith("[unknown]")
    assert "[" not in lines["r1"]


# ---------------------------------------------------------------- resume / approve / reject
def test_resume_sync_not_found(runs_dir, capsys):
    assert "nicht gefunden" in cli._resume_sync("p", "nope", True)


def test_resume_sync_wrong_status(runs_dir, capsys):
    _write_run(runs_dir, "p", "r1", "pass")
    assert "nicht needs_approval" in cli._resume_sync("p", "r1", True)


def test_resume_sync_reject(runs_dir, capsys):
    _write_run(runs_dir, "p", "r1", "needs_approval")
    out = cli._resume_sync("p", "r1", False)
    assert "abgelehnt" in out
    assert json.loads((runs_dir / "p" / "r1.json").read_text())["status"] == "rejected"


def test_resume_sync_reject_updates_retry_chain(runs_dir, monkeypatch, capsys):
    _write_run(runs_dir, "p", "r1", "needs_approval")
    ended = []
    monkeypatch.setattr(retry_chains, "record_run_end", lambda project, run: ended.append((project, run["status"])))
    cli._resume_sync("p", "r1", False)
    assert ended == [("p", "rejected")]


def test_resume_sync_approve(runs_dir, projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    _write_run(runs_dir, "p", "r1", "needs_approval", loop="l")
    monkeypatch.setattr(
        cli,
        "run_loop",
        lambda *a, **k: {
            "status": "pass",
            "loop": "l",
            "steps": [{"name": "s", "type": "shell", "status": "pass", "summary": "ok"}],
        },
    )
    out = cli._resume_sync("p", "r1", True)
    assert "PASS" in out


def test_resume_sync_approve_with_pr_vars(runs_dir, projects_dir, tmp_path, monkeypatch, capsys):
    """Der fortgesetzte Run blockiert seinen eigenen Claim nicht (ADR 0043)."""
    _write_spec(
        projects_dir, "p", root=tmp_path / "proj", loops={"approval": {"steps": [{"type": "shell", "cmd": "true"}]}}
    )
    d = runs_dir / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(
        json.dumps(
            {"id": "r1", "project": "p", "loop": "approval", "status": "needs_approval", "vars": {"pr_number": 5}}
        )
    )
    monkeypatch.setattr(cli, "run_loop", lambda *a, **k: {"status": "pass", "loop": "approval", "steps": []})
    out = cli._resume_sync("p", "r1", True)
    assert "PASS" in out


def test_cmd_approve_not_found(runs_dir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.cmd_approve(type("A", (), {"run_id": "nope", "project": None})())
    assert e.value.code == 1


def test_cmd_reject_not_found(runs_dir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.cmd_reject(type("A", (), {"run_id": "nope", "project": None})())
    assert e.value.code == 1


def test_cmd_approve_found(runs_dir, projects_dir, tmp_path, monkeypatch, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    _write_run(runs_dir, "p", "r1", "needs_approval")
    monkeypatch.setattr(
        cli,
        "run_loop",
        lambda *a, **k: {"status": "pass", "loop": "l", "steps": []},
    )
    cli.cmd_approve(type("A", (), {"run_id": "r1", "project": "p"})())
    assert "PASS" in capsys.readouterr().out


# ---------------------------------------------------------------- chat / alert / import
def test_cmd_chat(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_chat", lambda msg, who=None: f"echo: {msg}")
    cli.cmd_chat(type("A", (), {"message": ["hallo", "welt"]})())
    assert "echo: hallo welt" in capsys.readouterr().out


def test_cmd_alert(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_chat_alert_test", lambda p: "alert-ok")
    cli.cmd_alert(type("A", (), {"project": None})())
    assert "alert-ok" in capsys.readouterr().out


def test_cmd_agent_env_scrubs_secrets(monkeypatch, capsys):
    """ADR 0050: `fabrikant agent-env` druckt nur die Allowlist — keine Secrets."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("FABRIKANT_AGENT_ENV_EXTRA", raising=False)
    cli.cmd_agent_env(None)
    out = capsys.readouterr().out
    assert "PATH=/usr/bin" in out
    assert "GITHUB_TOKEN" not in out


def test_cmd_import_runs(runs_dir, monkeypatch, capsys):
    monkeypatch.setattr("fabrikant.sessionlog.import_runs", lambda d: 3)
    cli.cmd_import_runs(None)
    assert "3 Runs importiert" in capsys.readouterr().out


def test_cmd_retry_backfill(monkeypatch, capsys):
    monkeypatch.setattr(
        retry_chains, "backfill_chains", lambda **kw: retry_chains.BackfillSummary(pending=2, exhausted=5, skipped=1)
    )
    cli.cmd_retry_backfill(None)
    assert "2 pending, 5 exhausted, 1 übersprungen" in capsys.readouterr().out


def test_main_retry_backfill(monkeypatch, capsys):
    monkeypatch.setattr(retry_chains, "backfill_chains", lambda **kw: retry_chains.BackfillSummary())
    cli.main(["retry-backfill"])
    assert "0 pending, 0 exhausted, 0 übersprungen" in capsys.readouterr().out


# ---------------------------------------------------------------- main()
def test_main_list(projects_dir, tmp_path, capsys):
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    cli.main(["list"])
    assert "p" in capsys.readouterr().out


def test_main_runs(runs_dir, capsys):
    _write_run(runs_dir, "p", "r1", "pass")
    cli.main(["runs", "--project", "p"])
    assert "r1" in capsys.readouterr().out


def test_main_chat(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_chat", lambda msg, who=None: "chat-ans")
    cli.main(["chat", "hi"])
    assert "chat-ans" in capsys.readouterr().out


def test_main_chat_passes_cli_actor(monkeypatch, capsys):
    """Chat-Befehle aus der CLI tragen den Aufrufer (cli:<os-user>) ins Audit (ADR 0030)."""
    seen = {}
    monkeypatch.setattr(cli, "_cli_actor", lambda: "cli:anna")
    monkeypatch.setattr(cli, "_chat", lambda msg, who=None: seen.update(who=who) or "ok")
    cli.main(["chat", "hi"])
    assert seen["who"].name == "cli:anna"
    assert seen["who"].allows(cli.auth.ADMIN)


def test_main_chat_run_records_cli_actor(monkeypatch, capsys):
    """Integration: `chat lauf` reicht den CLI-Actor bis zum Run-Start durch."""
    seen = {}
    monkeypatch.setattr(cli, "_cli_actor", lambda: "cli:anna")
    monkeypatch.setattr(
        "fabrikant.chat._chat_start_run", lambda *a, actor=None, **k: seen.update(actor=actor) or "gestartet"
    )
    monkeypatch.setattr("fabrikant.chat.load_projects", lambda: {"p": {"loops": {"l": {}}}})
    cli.main(["chat", "lauf", "p", "l"])
    assert seen["actor"] == "cli:anna"


# ---------------------------------------------------------------- _print_steps
def test_print_steps_marks(capsys):
    rec = {
        "steps": [
            {"name": "a", "type": "shell", "status": "pass", "summary": "ok"},
            {"name": "b", "type": "pr", "status": "skipped"},
            {"name": "c", "type": "approval", "status": "waiting"},
            {"name": "d", "type": "shell", "status": "weird"},
        ]
    }
    cli._print_steps(rec)
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["  ✔ a [shell] ok", "  – b [pr] ", "  ⏸ c [approval] ", "  ? d [shell] "]


def test_resume_sync_failed_resume_stays_decidable(runs_dir, projects_dir, tmp_path, monkeypatch, capsys):
    """Resume scheitert vor dem Start (z. B. Spec defekt): Freigabe zurückgenommen, der Run
    lässt sich erneut freigeben oder ablehnen."""
    _write_spec(projects_dir, "p", root=tmp_path / "proj")
    _write_run(runs_dir, "p", "r1", "needs_approval", loop="l")

    def _boom(*a, **k):
        raise RuntimeError("Projekt unbekannt")

    monkeypatch.setattr(cli, "run_loop", _boom)
    assert "Resume fehlgeschlagen" in cli._resume_sync("p", "r1", True)
    rec = json.loads((runs_dir / "p" / "r1.json").read_text())
    assert rec["status"] == "needs_approval" and rec["approved_by"] is None
    assert "abgelehnt" in cli._resume_sync("p", "r1", False)


def test_cmd_agent_env_skips_values_with_newlines(monkeypatch, capsys):
    """Ein Zeilenumbruch im Wert würde EnvironmentFile/--env-file-Zeilen einschleusen."""
    monkeypatch.setenv("FABRIKANT_AGENT_ENV_EXTRA", "BAD,GOOD")
    monkeypatch.setenv("BAD", "x\nINJECTED=1")
    monkeypatch.setenv("GOOD", "ok")
    cli.cmd_agent_env(None)
    cap = capsys.readouterr()
    assert "GOOD=ok" in cap.out and "INJECTED" not in cap.out and "BAD=" not in cap.out
    assert "BAD" in cap.err
