"""MCP server (Roadmap #132): fabrikant as a stdio MCP endpoint.

Covers the JSON-RPC layer (initialize / ping / tools-list / notifications /
error codes), the tool handlers against real specs + run records, and a small
end-to-end stdio exchange.
"""

from __future__ import annotations

import io
import json

import pytest

from fabrikant import engine
from fabrikant import mcp_server as mcp


def _write_spec(projects_dir, name="p", root=None, loops=None):
    """Minimal valid spec (same shape as test_replay._write_spec)."""
    import json as _json

    root = root or projects_dir.parent / "proj"
    root.mkdir(parents=True, exist_ok=True)
    loops = loops or {"l": {"steps": [{"type": "shell", "cmd": "true"}]}}
    (projects_dir / f"{name}.fabrikant.yaml").write_text(
        f"name: {name}\nroot: {root}\nloops:\n"
        + "\n".join(
            f"  {ln}:\n    steps:\n" + "\n".join(f"      - {_json.dumps(s)}" for s in ldef["steps"])
            for ln, ldef in loops.items()
        )
    )


def _write_run(runs_dir, project, rid, status="pass", loop="l", **extra):
    d = runs_dir / project
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": rid,
        "project": project,
        "loop": loop,
        "status": status,
        "started": "2024-01-01T00:00:00Z",
        "spec_hash": "abc123def456",
        "vars": {},
        "steps": [],
    }
    rec.update(extra)
    (d / f"{rid}.json").write_text(json.dumps(rec))
    return rec


# --------------------------------------------------------------------- protocol
def test_initialize_returns_protocol_and_server_info():
    res = mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert res["protocolVersion"] == "2025-03-26"
    assert res["serverInfo"]["name"] == "fabrikant"
    assert "tools" in res["capabilities"]


def test_ping_returns_empty_result():
    assert mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {}


def test_initialized_notification_returns_none():
    assert mcp._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_raises_rpc_error():
    with pytest.raises(mcp.RpcError) as e:
        mcp._handle({"jsonrpc": "2.0", "id": 3, "method": "bogus/method"})
    assert e.value.code == -32601


_DEFAULT_TOOLS = {
    "list_projects",
    "get_spec",
    "get_raw_spec",
    "validate_spec",
    "list_runs",
    "get_run",
    "get_run_log",
    "start_run",
    "queue_status",
    "list_approvals",
}
_APPROVAL_TOOLS = {"approve_run", "reject_run"}


def test_tools_list_exposes_expected_tools(monkeypatch):
    # Approve/reject are opt-in (the human gate must not be reachable by a prompt-injected agent).
    monkeypatch.setenv(mcp.ALLOW_APPROVALS_ENV, "1")
    res = mcp._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    names = {t["name"] for t in res["tools"]}
    assert names == _DEFAULT_TOOLS | _APPROVAL_TOOLS
    for t in res["tools"]:
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]


def test_tools_call_unknown_tool_is_rpc_error():
    with pytest.raises(mcp.RpcError) as e:
        mcp._handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})
    assert e.value.code == -32602


def test_send_writes_newline_delimited_envelope(capsys):
    mcp._send(7, {"ok": True})
    out = capsys.readouterr().out
    line = out.strip()
    assert line.endswith("\n") or out.endswith("\n")
    msg = json.loads(line)
    assert msg == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_send_error_envelope(capsys):
    mcp._send(8, {"code": -32601, "message": "nope"})
    msg = json.loads(capsys.readouterr().out.strip())
    assert msg["id"] == 8
    assert msg["error"]["code"] == -32601


# ------------------------------------------------------------- tool handlers: specs
def test_list_projects(projects_dir):
    _write_spec(projects_dir, "alpha", loops={"l1": {"steps": [{"type": "shell", "cmd": "true"}]}})
    _write_spec(projects_dir, "beta")
    res = mcp._list_projects({})
    names = {p["name"] for p in res["projects"]}
    assert {"alpha", "beta"} <= names
    assert res["count"] == len(res["projects"])


def test_get_spec_returns_hash(projects_dir):
    _write_spec(projects_dir, "p")
    res = mcp._get_spec({"project": "p"})
    assert res["name"] == "p"
    assert res["loops"]["l"]["steps"][0]["type"] == "shell"
    assert isinstance(res["_hash"], str) and res["_hash"]


def test_get_spec_unknown_project_raises(projects_dir):
    with pytest.raises(mcp.ToolError):
        mcp._get_spec({"project": "ghost"})


def test_get_spec_missing_project_raises(projects_dir):
    with pytest.raises(mcp.ToolError):
        mcp._get_spec({})


def test_get_raw_spec(projects_dir):
    _write_spec(projects_dir, "p")
    res = mcp._get_raw_spec({"project": "p"})
    assert res["name"] == "p"
    assert "_hash" in res


def test_validate_spec_valid_project(projects_dir):
    _write_spec(projects_dir, "p")
    res = mcp._validate_spec({"project": "p"})
    assert res["valid"] is True
    assert res["count"] == 1
    assert res["results"][0]["project"] == "p"
    assert res["results"][0]["errors"] == []


def test_validate_spec_all_projects(projects_dir):
    _write_spec(projects_dir, "alpha")
    _write_spec(projects_dir, "beta")
    res = mcp._validate_spec({})
    assert res["valid"] is True
    assert res["count"] == 2
    assert {r["project"] for r in res["results"]} == {"alpha", "beta"}


def test_validate_spec_unknown_project_raises(projects_dir):
    _write_spec(projects_dir, "p")
    with pytest.raises(mcp.ToolError):
        mcp._validate_spec({"project": "ghost"})


# ------------------------------------------------------------ tool handlers: runs
def test_list_runs(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    _write_run(runs_dir, "p", "r2", status="needs_approval")
    res = mcp._list_runs({"project": "p", "limit": 10})
    assert {r["id"] for r in res["runs"]} == {"r1", "r2"}
    assert res["count"] == 2


def test_list_runs_status_filter(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    _write_run(runs_dir, "p", "r2", status="needs_approval")
    res = mcp._list_runs({"project": "p", "status": "needs_approval"})
    assert [r["id"] for r in res["runs"]] == ["r2"]


def test_list_runs_limit(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    for i in range(5):
        _write_run(runs_dir, "p", f"r{i}", status="pass")
    res = mcp._list_runs({"project": "p", "limit": 2})
    assert res["count"] == 2


def test_get_run(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    res = mcp._get_run({"run_id": "r1", "project": "p"})
    assert res["id"] == "r1"
    assert res["status"] == "pass"


def test_get_run_not_found_raises(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    with pytest.raises(mcp.ToolError):
        mcp._get_run({"run_id": "ghost", "project": "p"})


def test_get_run_log(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    (runs_dir / "p" / "r1.log").write_text("line1\nline2\nline3\nline4\n")
    full = mcp._get_run_log({"run_id": "r1", "project": "p"})
    assert full["log"] == "line1\nline2\nline3\nline4\n"
    tail = mcp._get_run_log({"run_id": "r1", "project": "p", "lines": 2})
    assert tail["log"] == "line3\nline4"


def test_get_run_log_missing_raises(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    with pytest.raises(mcp.ToolError):
        mcp._get_run_log({"run_id": "r1", "project": "p"})


# -------------------------------------------------------- tool handlers: approvals
def test_list_approvals(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="needs_approval", approval={"step": "deploy"})
    _write_run(runs_dir, "p", "r2", status="pass")
    res = mcp._list_approvals({"project": "p"})
    assert res["count"] == 1
    assert res["approvals"][0]["id"] == "r1"
    assert res["approvals"][0]["approval"] == {"step": "deploy"}


def test_approve_run_calls_resume(monkeypatch, projects_dir):
    calls = {}

    def fake_resume(project, run_id, approve, actor=None):
        calls.update(project=project, run_id=run_id, approve=approve, actor=actor)
        return "▶ approved", True

    monkeypatch.setattr(mcp, "_chat_resume", fake_resume)
    res = mcp._approve_run({"project": "p", "run_id": "r1"})
    assert res["ok"] is True
    assert calls == {"project": "p", "run_id": "r1", "approve": True, "actor": "mcp"}


def test_reject_run_calls_resume(monkeypatch, projects_dir):
    calls = {}

    def fake_resume(project, run_id, approve, actor=None):
        calls.update(project=project, run_id=run_id, approve=approve, actor=actor)
        return "⊘ rejected", False

    monkeypatch.setattr(mcp, "_chat_resume", fake_resume)
    res = mcp._reject_run({"project": "p", "run_id": "r1"})
    assert res["reply"] == "⊘ rejected"
    assert calls["approve"] is False
    assert calls["actor"] == "mcp"


def test_approve_run_requires_args(projects_dir):
    with pytest.raises(mcp.ToolError):
        mcp._approve_run({"project": "p"})


# ------------------------------------------------------------ tool handlers: start
def test_start_run_unknown_loop_raises(monkeypatch, projects_dir):
    _write_spec(projects_dir, "p", loops={"real": {"steps": [{"type": "shell", "cmd": "true"}]}})
    with pytest.raises(mcp.ToolError):
        mcp._start_run({"project": "p", "loop": "missing"})


def test_start_run_unknown_project_raises(monkeypatch, projects_dir):
    with pytest.raises(mcp.ToolError):
        mcp._start_run({"project": "ghost", "loop": "l"})


def test_start_run_delegates_to_engine(monkeypatch, projects_dir):
    _write_spec(projects_dir, "p", loops={"l": {"steps": [{"type": "shell", "cmd": "true"}]}})
    calls = {}

    def fake_start(name, lname, trigger, *, run_id, note="", initial_vars=None, actor=None):
        calls.update(
            name=name, lname=lname, trigger=trigger, run_id=run_id, note=note, initial_vars=initial_vars, actor=actor
        )
        return False

    monkeypatch.setattr(mcp, "start_priority_run", fake_start)
    res = mcp._start_run({"project": "p", "loop": "l", "note": "hi", "vars": {"a": "1"}})
    assert res["started"] is True
    assert res["queued"] is False
    assert calls["name"] == "p" and calls["lname"] == "l"
    assert calls["trigger"] == "mcp"
    assert calls["initial_vars"] == {"a": "1"}
    assert calls["actor"] == "mcp"
    assert calls["run_id"].endswith("-l")


def test_start_run_rejects_non_object_vars(monkeypatch, projects_dir):
    _write_spec(projects_dir, "p", loops={"l": {"steps": [{"type": "shell", "cmd": "true"}]}})
    with pytest.raises(mcp.ToolError):
        mcp._start_run({"project": "p", "loop": "l", "vars": "not-a-dict"})


def test_queue_status(monkeypatch, projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="running")
    _write_run(runs_dir, "p", "r2", status="needs_approval")
    monkeypatch.setattr(engine, "pool_status", lambda: {"workers": 4, "busy": 1})
    res = mcp._queue_status({})
    assert res["pool"] == {"workers": 4, "busy": 1}
    assert [r["id"] for r in res["running"]] == ["r1"]
    assert [r["id"] for r in res["needs_approval"]] == ["r2"]
    assert res["queued"] == []


# ------------------------------------------------------------------ end-to-end
def _stdio_exchange(lines: list[str]) -> list[dict]:
    """Feed JSON lines to serve_stdio, return the parsed response envelopes."""
    old_in, old_out = mcp.sys.stdin, mcp.sys.stdout
    in_buf = io.StringIO("\n".join(lines) + "\n")
    out_buf = io.StringIO()
    mcp.sys.stdin, mcp.sys.stdout = in_buf, out_buf
    try:
        mcp.serve_stdio()
    finally:
        mcp.sys.stdin, mcp.sys.stdout = old_in, old_out
    return [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]


def test_stdio_initialize_then_tools_list(projects_dir):
    _write_spec(projects_dir, "p")
    msgs = _stdio_exchange(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
    )
    by_id = {m["id"]: m for m in msgs if "id" in m}
    assert by_id[1]["result"]["serverInfo"]["name"] == "fabrikant"
    assert len(by_id[2]["result"]["tools"]) == len(_DEFAULT_TOOLS)
    # notification produced no envelope
    assert len(msgs) == 2


def test_stdio_tools_call_list_projects(projects_dir):
    _write_spec(projects_dir, "alpha")
    msgs = _stdio_exchange(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_projects", "arguments": {}},
                }
            ),
        ]
    )
    call = next(m for m in msgs if m.get("id") == 2)
    assert call["result"]["isError"] is False
    structured = call["result"]["structuredContent"]
    assert {p["name"] for p in structured["projects"]} >= {"alpha"}
    # text content is valid JSON too
    parsed = json.loads(call["result"]["content"][0]["text"])
    assert parsed["count"] == structured["count"]


def test_stdio_tools_call_error_is_iserror(projects_dir):
    msgs = _stdio_exchange(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_run", "arguments": {"run_id": "ghost"}},
                }
            ),
        ]
    )
    call = next(m for m in msgs if m.get("id") == 2)
    assert call["result"]["isError"] is True
    assert "not found" in call["result"]["content"][0]["text"]


def test_stdio_parse_error(capsys):
    msgs = _stdio_exchange(["{not valid json"])
    assert msgs[0]["error"]["code"] == -32700


def test_cli_registers_mcp_subcommand(monkeypatch):
    import fabrikant.cli as cli

    # `mcp` must be a registered subcommand that routes to serve_stdio. Patch the
    # imported symbol so main() doesn't block reading stdin.
    calls = {}
    monkeypatch.setattr(cli, "serve_stdio", lambda: calls.update(ran=True))
    cli.main(["mcp"])
    assert calls == {"ran": True}
