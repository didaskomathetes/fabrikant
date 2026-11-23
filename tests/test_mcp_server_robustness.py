"""Robustness tests for the MCP stdio server: protocol conformance, stdout hygiene,
argument validation, redaction, read-only mode and failure containment."""

from __future__ import annotations

import io
import json

import pytest

from fabrik import mcp_server as mcp


def _write_spec(projects_dir, name="p"):
    root = projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (projects_dir / f"{name}.fabrik.yaml").write_text(
        f'name: {name}\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{"type": "shell", "cmd": "true"}}\n'
    )


def _write_run(runs_dir, project, rid, status="pass"):
    (runs_dir / project).mkdir(parents=True, exist_ok=True)
    rec = {"id": rid, "project": project, "loop": "l", "status": status, "started": "2024-01-01T00:00:00Z"}
    (runs_dir / project / f"{rid}.json").write_text(json.dumps(rec))


def _exchange(lines: list[str]) -> tuple[list, str]:
    """Run serve_stdio over the given lines; return (parsed stdout messages, stderr text)."""
    in_buf, out_buf, err_buf = io.StringIO("\n".join(lines) + "\n"), io.StringIO(), io.StringIO()
    saved = mcp.sys.stdin, mcp.sys.stdout, mcp.sys.stderr
    mcp.sys.stdin, mcp.sys.stdout, mcp.sys.stderr = in_buf, out_buf, err_buf
    try:
        mcp.serve_stdio()
    finally:
        mcp.sys.stdin, mcp.sys.stdout, mcp.sys.stderr = saved
    return [json.loads(x) for x in out_buf.getvalue().splitlines() if x.strip()], err_buf.getvalue()


def _req(id_, method, params=None):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def test_parse_error_carries_null_id():
    (msg,), _ = _exchange(["{not json"])
    assert msg["id"] is None and msg["error"]["code"] == mcp.ERR_PARSE


def test_batch_requests_are_answered_as_a_batch():
    (batch,), _ = _exchange(
        [json.dumps([json.loads(_req(1, "ping")), {"jsonrpc": "2.0", "method": "notifications/initialized"}])]
    )
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_empty_batch_is_invalid():
    (msg,), _ = _exchange(["[]"])
    assert msg["error"]["code"] == mcp.ERR_INVALID_REQUEST


def test_notification_gets_no_response_but_request_form_gets_empty_result():
    msgs, _ = _exchange(
        [
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            _req(5, "notifications/initialized"),
        ]
    )
    assert msgs == [{"jsonrpc": "2.0", "id": 5, "result": {}}]


@pytest.mark.parametrize(
    "raw",
    [
        '{"jsonrpc": "2.0", "id": 1}',  # missing method
        '{"jsonrpc": "1.0", "id": 1, "method": "ping"}',  # wrong version
        '{"jsonrpc": "2.0", "id": true, "method": "ping"}',  # bool id
        '{"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}',  # object id
    ],
)
def test_invalid_requests_are_rejected_not_fatal(raw):
    msgs, _ = _exchange([raw, _req(2, "ping")])
    assert msgs[0]["error"]["code"] == mcp.ERR_INVALID_REQUEST
    assert msgs[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}  # server survived


@pytest.mark.parametrize(
    "params",
    [[], "x", {"name": "list_projects", "arguments": "x"}, {"name": "list_runs", "arguments": {"limit": 1e999}}],
)
def test_bad_params_never_kill_the_server(params):
    msgs, _ = _exchange([_req(1, "tools/call", params), _req(2, "ping")])
    assert msgs[-1]["id"] == 2


def test_handler_crash_becomes_tool_error_and_logs_to_stderr(monkeypatch):
    def boom(_args):
        raise OSError("disk gone")

    monkeypatch.setitem(next(t for t in mcp.TOOLS if t["name"] == "list_projects"), "handler", boom)
    msgs, err = _exchange([_req(1, "tools/call", {"name": "list_projects"}), _req(2, "ping")])
    assert msgs[0]["result"]["isError"] is True and "disk gone" in msgs[0]["result"]["content"][0]["text"]
    assert msgs[1]["id"] == 2 and "OSError" in err


def test_prints_inside_handlers_do_not_corrupt_stdout(monkeypatch):
    def noisy(_args):
        print("[fabrik] some engine log line")
        return {"ok": True}

    monkeypatch.setitem(next(t for t in mcp.TOOLS if t["name"] == "list_projects"), "handler", noisy)
    msgs, err = _exchange([_req(1, "tools/call", {"name": "list_projects"})])
    assert len(msgs) == 1 and msgs[0]["result"]["isError"] is False  # stdout still pure JSON lines
    assert "some engine log line" in err


def test_stdout_is_restored_after_serving():
    before = mcp.sys.stdout
    _exchange([_req(1, "ping")])
    assert mcp.sys.stdout is before


def test_protocol_version_negotiation():
    msgs, _ = _exchange(
        [_req(1, "initialize", {"protocolVersion": "2024-11-05"}), _req(2, "initialize", {"protocolVersion": "1999"})]
    )
    assert msgs[0]["result"]["protocolVersion"] == "2024-11-05"
    assert msgs[1]["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION


@pytest.mark.parametrize("bad", [True, 1.5, "3", 0, -1, mcp.MAX_LIST_LIMIT + 1])
def test_list_runs_limit_is_validated(bad):
    with pytest.raises(mcp.ToolError):
        mcp._list_runs({"limit": bad})


def test_get_run_log_defaults_to_bounded_tail(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1")
    (runs_dir / "p" / "r1.log").write_text("\n".join(str(i) for i in range(mcp.DEFAULT_LOG_LINES + 10)))
    res = mcp._get_run_log({"run_id": "r1", "project": "p"})
    assert res["truncated"] is True and len(res["log"].splitlines()) == mcp.DEFAULT_LOG_LINES
    with pytest.raises(mcp.ToolError):
        mcp._get_run_log({"run_id": "r1", "project": "p", "lines": 0})


def test_redact_masks_secret_keys_recursively():
    out = mcp._redact({"a": {"api_key": "x", "name": "n"}, "l": [{"password": "p"}], "token": 5, "ok": 1})
    assert out["a"] == {"api_key": mcp.REDACTED, "name": "n"}
    assert out["l"] == [{"password": mcp.REDACTED}] and out["token"] == mcp.REDACTED and out["ok"] == 1


def test_get_spec_hides_path_and_secrets(projects_dir):
    _write_spec(projects_dir, "p")
    spec = mcp._get_spec({"project": "p"})
    assert "_path" not in spec


def test_read_only_mode_hides_and_refuses_mutating_tools(monkeypatch):
    monkeypatch.setenv(mcp.READ_ONLY_ENV, "1")
    names = {t["name"] for t in mcp._visible_tools()}
    assert not names & {"start_run", "approve_run", "reject_run"} and "get_run" in names
    msgs, _ = _exchange([_req(1, "tools/call", {"name": "approve_run", "arguments": {"project": "p", "run_id": "r"}})])
    assert msgs[0]["error"]["code"] == mcp.ERR_INVALID_PARAMS


@pytest.mark.parametrize("approve", [True, False])
def test_failed_approve_reject_is_a_tool_error(projects_dir, runs_dir, approve):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")  # not needs_approval
    with pytest.raises(mcp.ToolError):
        mcp._resume({"project": "p", "run_id": "r1"}, approve=approve)


def test_drain_waits_for_new_threads(monkeypatch):
    import threading
    import time

    done = threading.Event()
    t = threading.Thread(target=lambda: (time.sleep(0.2), done.set()), daemon=True)
    baseline = set(threading.enumerate())
    t.start()
    mcp._drain_workers(baseline, timeout_s=5)
    assert done.is_set()


def test_startup_recovery_failure_is_contained(monkeypatch):
    monkeypatch.setattr(mcp, "reap_orphaned_runs", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    msgs, err = _exchange([_req(1, "ping")])
    assert msgs[0]["id"] == 1 and "db locked" in err


def test_approval_tools_are_opt_in(monkeypatch):
    monkeypatch.delenv(mcp.ALLOW_APPROVALS_ENV, raising=False)
    assert not {t["name"] for t in mcp._visible_tools()} & {"approve_run", "reject_run"}
    msgs, _ = _exchange([_req(1, "tools/call", {"name": "approve_run", "arguments": {"project": "p", "run_id": "r"}})])
    assert msgs[0]["error"]["code"] == mcp.ERR_INVALID_PARAMS  # refused, not merely hidden
    monkeypatch.setenv(mcp.ALLOW_APPROVALS_ENV, "1")
    assert {"approve_run", "reject_run"} <= {t["name"] for t in mcp._visible_tools()}


def test_read_only_wins_over_allow_approvals(monkeypatch):
    monkeypatch.setenv(mcp.ALLOW_APPROVALS_ENV, "1")
    monkeypatch.setenv(mcp.READ_ONLY_ENV, "1")
    names = {t["name"] for t in mcp._visible_tools()}
    assert not names & {"approve_run", "reject_run", "start_run"}
