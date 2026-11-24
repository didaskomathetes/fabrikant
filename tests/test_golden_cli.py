"""CLI tests for ``fabrikant golden`` / ``fabrikant eval``: exit codes and guard paths (roadmap #130)."""

from __future__ import annotations

import pytest

from fabrikant import cli
from fabrikant.golden import GoldenError
from fabrikant.replay import ReplayError


class _Claim:
    """Minimal stand-in for ``RunClaim`` that records the release."""

    scope = "scope"

    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _Args:
    def __init__(self, **kw):
        self.__dict__.update({"project": "p", "loop": "l", "name": None, "yes": True, **kw})


@pytest.fixture
def claim(monkeypatch):
    c = _Claim()
    monkeypatch.setattr(cli, "claim_run", lambda *a, **k: c)
    return c


@pytest.fixture
def golden_known(monkeypatch):
    monkeypatch.setattr(cli, "get_golden", lambda project, name: {"loop": "l"})
    monkeypatch.setattr(cli.pause, "manual_block_reason", lambda *a: None)


def _stub_eval(monkeypatch, diffs=(), status="pass"):
    record = {"id": "e1", "project": "p", "loop": "l", "status": status, "steps": []}
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: ({}, {"id": "b1"}, record, list(diffs)))
    monkeypatch.setattr(cli, "eval_report", lambda *a: "REPORT")


def _exit_code(fn, args):
    with pytest.raises(SystemExit) as ei:
        fn(args)
    return ei.value.code


def test_eval_ok(golden_known, claim, monkeypatch):
    _stub_eval(monkeypatch)
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_OK
    assert claim.released


def test_eval_regression(golden_known, claim, monkeypatch):
    _stub_eval(monkeypatch, diffs=["step x differs"])
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_REGRESSION
    assert claim.released


@pytest.mark.parametrize("status", ["needs_approval", "running", "queued"])
def test_eval_inconclusive_is_error_not_regression(golden_known, claim, monkeypatch, capsys, status):
    _stub_eval(monkeypatch, diffs=["status differs"], status=status)
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_ERROR
    assert "inconclusive: eval run is waiting for approval" in capsys.readouterr().out


def test_eval_unknown_golden(monkeypatch, capsys):
    def boom(project, name):
        raise GoldenError("unknown golden")

    monkeypatch.setattr(cli, "get_golden", boom)
    assert _exit_code(cli.cmd_eval, _Args(name="nope")) == cli.EXIT_ERROR
    assert "unknown golden" in capsys.readouterr().out


def test_eval_blocked_project(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_golden", lambda *a: {"loop": "l"})
    monkeypatch.setattr(cli.pause, "manual_block_reason", lambda *a: "project paused")
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_ERROR
    assert "project paused" in capsys.readouterr().out


def test_eval_no_tty_without_yes_aborts(golden_known, claim, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    called = []
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: called.append(1))
    assert _exit_code(cli.cmd_eval, _Args(yes=False)) == cli.EXIT_ERROR
    assert not called


def test_eval_user_declines(golden_known, claim, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    called = []
    monkeypatch.setattr(cli, "run_eval", lambda *a, **k: called.append(1))
    assert _exit_code(cli.cmd_eval, _Args(yes=False)) == cli.EXIT_ERROR
    assert not called


def test_eval_user_accepts(golden_known, claim, monkeypatch):
    _stub_eval(monkeypatch)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _exit_code(cli.cmd_eval, _Args(yes=False)) == cli.EXIT_OK


def test_eval_claim_refused(golden_known, monkeypatch, capsys):
    monkeypatch.setattr(cli, "claim_run", lambda *a, **k: None)
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_ERROR
    assert "already running" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [GoldenError("g"), ReplayError("r"), RuntimeError("bug")])
def test_eval_releases_claim_and_exits_error_on_exception(golden_known, claim, monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli, "run_eval", boom)
    assert _exit_code(cli.cmd_eval, _Args()) == cli.EXIT_ERROR
    assert claim.released


def test_golden_capture(monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr(cli, "capture_golden", lambda project, loop, run_id: {"loop": loop})
    monkeypatch.setattr(cli, "save_golden", lambda project, name, g: saved.setdefault("n", name) and "/tmp/g.json")
    cli.cmd_golden(_Args(golden_cmd="capture", run_id="r1"))
    assert saved["n"] == "l"
    assert "golden 'l'" in capsys.readouterr().out


def test_golden_capture_error(monkeypatch, capsys):
    def boom(*a):
        raise GoldenError("baseline missing")

    monkeypatch.setattr(cli, "capture_golden", boom)
    assert _exit_code(cli.cmd_golden, _Args(golden_cmd="capture", run_id="r1")) == cli.EXIT_ERROR
    assert "baseline missing" in capsys.readouterr().out


_GOLDEN = {"loop": "l", "baseline_run": "b1", "spec_hash": "abc", "expect": {"status": "pass", "steps": []}}


def test_golden_show_all_and_empty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_goldens", lambda project: {"l": _GOLDEN})
    cli.cmd_golden(_Args(golden_cmd="show", name=None))
    assert "baseline=b1" in capsys.readouterr().out
    monkeypatch.setattr(cli, "load_goldens", lambda project: {})
    cli.cmd_golden(_Args(golden_cmd="show", name=None))
    assert "no goldens for p" in capsys.readouterr().out


def test_golden_show_unknown_name_is_clean_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_goldens", lambda project: {"l": _GOLDEN})
    assert _exit_code(cli.cmd_golden, _Args(golden_cmd="show", name="unknown")) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert "unknown" in out and "KeyError" not in out
