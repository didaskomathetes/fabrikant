"""MCP server (Roadmap #132, ADR 0064) on the official SDK.

Tool handlers are plain functions and are tested directly against real specs and run records;
the registry/gating and the protocol surface (annotations, structured output, refusal of hidden
tools) are tested through the SDK's in-process client.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp_support import call, list_tools, text

from fabrikant import engine
from fabrikant import mcp_server as mcp
from fabrikant.secrets import MASKED_VALUE, redact_secret_keys


def _write_spec(projects_dir, name="p", root=None, loops=None):
    """Minimal valid spec (same shape as test_replay._write_spec)."""
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


READ_TOOLS = {
    "list_projects",
    "get_spec",
    "get_raw_spec",
    "validate_spec",
    "list_runs",
    "get_run",
    "get_run_log",
    "queue_status",
    "list_approvals",
}
DEFAULT_TOOLS = READ_TOOLS | {"start_run"}
APPROVAL_TOOLS = {"approve_run", "reject_run"}
SPEC_WRITE_TOOLS = {"propose_spec_change", "list_spec_proposals"}


# ------------------------------------------------------------------- registry
def test_tools_by_default_are_reads_plus_start_run():
    assert {t.name for t in list_tools(mcp.McpOptions())} == DEFAULT_TOOLS


def test_opt_in_groups_add_their_tools():
    assert {t.name for t in list_tools(mcp.McpOptions(allow_approvals=True))} == DEFAULT_TOOLS | APPROVAL_TOOLS
    spec_write = mcp.McpOptions(allow_spec_write=True)
    assert {t.name for t in list_tools(spec_write)} == DEFAULT_TOOLS | SPEC_WRITE_TOOLS


def test_read_only_wins_over_every_opt_in():
    everything = mcp.McpOptions(read_only=True, allow_approvals=True, allow_spec_write=True)
    assert {t.name for t in list_tools(everything)} == READ_TOOLS


def test_hidden_tools_are_refused_at_call_time():
    for name in ("approve_run", "propose_spec_change"):
        res = call(mcp.McpOptions(), name, {"project": "p", "run_id": "r"})
        assert res.is_error and "Unknown tool" in text(res)
    res = call(mcp.McpOptions(read_only=True), "start_run", {"project": "p", "loop": "l"})
    assert res.is_error and "Unknown tool" in text(res)


def test_every_tool_has_annotations_description_and_object_schema():
    everything = mcp.McpOptions(allow_approvals=True, allow_spec_write=True)
    tools = list_tools(everything)
    assert {t.name for t in tools} == {t.name for t in mcp.TOOLS}
    for t in tools:
        a = t.annotations
        assert a is not None and a.open_world_hint is False
        assert a.read_only_hint is not None and a.destructive_hint is not None and a.idempotent_hint is not None
        assert t.description and t.input_schema["type"] == "object" and t.output_schema
    by_name = {t.name: t.annotations for t in tools}
    assert by_name["get_spec"].read_only_hint is True
    assert by_name["start_run"].read_only_hint is False and by_name["start_run"].destructive_hint is False
    assert by_name["reject_run"].destructive_hint is True


def test_env_flags_and_cli_flags_are_or_ed(monkeypatch):
    assert mcp.McpOptions.from_env() == mcp.McpOptions()
    monkeypatch.setenv(mcp.ALLOW_SPEC_WRITE_ENV, "yes")
    monkeypatch.setenv(mcp.READ_ONLY_ENV, "0")
    opts = mcp.McpOptions.from_env(allow_approvals=True)
    assert opts == mcp.McpOptions(allow_approvals=True, allow_spec_write=True)


# --------------------------------------------------------------- protocol path
def test_call_returns_structured_content_and_text(projects_dir):
    _write_spec(projects_dir, "alpha")
    res = call(mcp.McpOptions(), "list_projects")
    assert not res.is_error
    assert {p["name"] for p in res.structured_content["projects"]} == {"alpha"}
    assert json.loads(text(res))["count"] == res.structured_content["count"]


def test_tool_error_is_is_error_result(projects_dir):
    res = call(mcp.McpOptions(), "get_run", {"run_id": "ghost"})
    assert res.is_error and "not found" in text(res)


@pytest.mark.parametrize("bad", [True, 1.5, "3", 0, -1, mcp.MAX_LIST_LIMIT + 1])
def test_list_runs_limit_is_validated(bad):
    assert call(mcp.McpOptions(), "list_runs", {"limit": bad}).is_error


@pytest.mark.parametrize("bad", [0, -1, "x", True])
def test_get_run_log_lines_is_validated(bad):
    assert call(mcp.McpOptions(), "get_run_log", {"run_id": "r", "lines": bad}).is_error


def test_handler_crash_is_contained_and_server_survives(monkeypatch):
    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(mcp, "load_projects", boom)

    async def go():
        async with Client(mcp.build_server(mcp.McpOptions())) as c:
            first = await c.call_tool("list_projects", {})
            second = await c.call_tool("queue_status", {})
            return first, second

    first, second = asyncio.run(go())
    assert first.is_error  # server answered with an error result instead of dying
    assert second is not None


# ------------------------------------------------------------- tool handlers: specs
def test_list_projects(projects_dir):
    _write_spec(projects_dir, "alpha", loops={"l1": {"steps": [{"type": "shell", "cmd": "true"}]}})
    _write_spec(projects_dir, "beta")
    res = mcp.list_projects()
    assert {"alpha", "beta"} <= {p.name for p in res.projects}
    assert res.count == len(res.projects)


def test_list_projects_reports_load_errors(projects_dir):
    (projects_dir / "bad.fabrikant.yaml").write_text("name: bad\nroot: /nonexistent-root-xyz\nloops: {}\n")
    (bad,) = [p for p in mcp.list_projects().projects if p.name == "bad"]
    assert bad.error


def test_get_spec_returns_hash_and_hides_path(projects_dir):
    _write_spec(projects_dir, "p")
    res = mcp.get_spec("p")
    assert res["name"] == "p" and res["loops"]["l"]["steps"][0]["type"] == "shell"
    assert isinstance(res["_hash"], str) and res["_hash"]
    assert "_path" not in res


def test_get_spec_redacts_secret_values(projects_dir):
    root = projects_dir.parent / "proj"
    root.mkdir()
    (projects_dir / "p.fabrikant.yaml").write_text(
        f"name: p\nroot: {root}\nvars:\n  api_key: hunter2\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: 'true'}}\n"
    )
    assert "hunter2" not in json.dumps(mcp.get_spec("p")) + json.dumps(mcp.get_raw_spec("p"))


def test_redact_masks_whole_value_under_secret_key():
    # A secret-looking key masks its entire value (any type), not just str/int leaves.
    out = redact_secret_keys(
        {
            "api_keys": ["hunter2", "x"],
            "credentials": {"user": "u", "password": "pw"},
            "token": 7,
            "name": "n",
            "nested": {"ok": 1, "secret": ["leak"]},
        }
    )
    assert out["api_keys"] == MASKED_VALUE
    assert out["credentials"] == MASKED_VALUE
    assert out["token"] == MASKED_VALUE
    assert out["name"] == "n"
    assert out["nested"] == {"ok": 1, "secret": MASKED_VALUE}


def test_get_spec_unknown_project_raises(projects_dir):
    with pytest.raises(ToolError):
        mcp.get_spec("ghost")


def test_get_raw_spec(projects_dir):
    _write_spec(projects_dir, "p")
    res = mcp.get_raw_spec("p")
    assert res["name"] == "p" and "_hash" in res


def test_validate_spec_one_and_all(projects_dir):
    _write_spec(projects_dir, "alpha")
    _write_spec(projects_dir, "beta")
    one = mcp.validate_specs("alpha")
    assert one.valid and one.count == 1 and one.results[0].errors == []
    everything = mcp.validate_specs()
    assert everything.valid and {r.project for r in everything.results} == {"alpha", "beta"}


def test_validate_spec_reports_load_errors(projects_dir):
    (projects_dir / "bad.fabrikant.yaml").write_text("name: bad\nroot: /nonexistent-root-xyz\nloops: {}\n")
    res = mcp.validate_specs("bad")
    assert not res.valid and res.results[0].errors


def test_validate_spec_unknown_project_raises(projects_dir):
    _write_spec(projects_dir, "p")
    with pytest.raises(ToolError):
        mcp.validate_specs("ghost")


# ------------------------------------------------------------ tool handlers: runs
def test_list_runs_filters(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    for i in range(5):
        _write_run(runs_dir, "p", f"r{i}", status="pass")
    _write_run(runs_dir, "p", "ra", status="needs_approval")
    _write_run(runs_dir, "p", "rb", status="needs_approval")
    assert mcp.list_runs_tool("p", limit=2).count == 2
    assert [r["id"] for r in mcp.list_runs_tool("p", status="needs_approval").runs] == ["ra", "rb"]
    # status is filtered before the limit: up to `limit` runs *of that status*
    assert mcp.list_runs_tool("p", status="needs_approval", limit=1).count == 1
    assert mcp.list_runs_tool("p", limit=10).count == 7


def test_get_run(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")
    assert mcp.get_run_tool("r1", "p")["status"] == "pass"
    with pytest.raises(ToolError):
        mcp.get_run_tool("ghost", "p")


def test_run_records_redact_secret_vars(projects_dir, runs_dir):
    # Run records carry ``vars`` (secrets may have been passed to start_run): masked on the way out.
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", vars={"api_token": "s3cret-value", "n": 1})
    got = mcp.get_run_tool("r1", "p")
    assert got["vars"]["api_token"] == MASKED_VALUE and got["vars"]["n"] == 1
    listed = mcp.list_runs_tool("p").runs
    assert listed[0]["vars"]["api_token"] == MASKED_VALUE
    assert "s3cret-value" not in json.dumps(got) + json.dumps(listed)


def test_get_run_log_tail_and_bound(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1")
    (runs_dir / "p" / "r1.log").write_text("line1\nline2\nline3\nline4\n")
    assert mcp.get_run_log_tool("r1", "p").log == "line1\nline2\nline3\nline4\n"
    tail = mcp.get_run_log_tool("r1", "p", lines=2)
    assert tail.log == "line3\nline4" and tail.truncated
    (runs_dir / "p" / "r1.log").write_text("\n".join(str(i) for i in range(mcp.DEFAULT_LOG_LINES + 10)))
    res = mcp.get_run_log_tool("r1", "p")
    assert res.truncated and len(res.log.splitlines()) == mcp.DEFAULT_LOG_LINES


def test_get_run_log_missing_raises(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1")
    with pytest.raises(ToolError):
        mcp.get_run_log_tool("r1", "p")


def test_queue_status(monkeypatch, projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="running")
    _write_run(runs_dir, "p", "r2", status="needs_approval")
    monkeypatch.setattr(engine, "pool_status", lambda: {"workers": 4, "busy": 1})
    res = mcp.queue_status()
    assert res.pool.model_dump() == {"workers": 4, "busy": 1, "queue": 0}
    assert [r.id for r in res.running] == ["r1"] and [r.id for r in res.needs_approval] == ["r2"]
    assert res.queued == []


# -------------------------------------------------------- tool handlers: approvals
def test_list_approvals(projects_dir, runs_dir):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="needs_approval", approval={"step": "deploy"})
    _write_run(runs_dir, "p", "r2", status="pass")
    res = mcp.list_approvals("p")
    assert res.count == 1 and res.approvals[0].id == "r1" and res.approvals[0].approval == {"step": "deploy"}


@pytest.mark.parametrize(("approve", "reply", "resumed"), [(True, "▶ approved", True), (False, "⊘ rejected", False)])
def test_approve_reject_delegate_to_resume_as_mcp(monkeypatch, approve, reply, resumed):
    calls = {}

    def fake_resume(project, run_id, approve, actor=None):
        calls.update(project=project, run_id=run_id, approve=approve, actor=actor)
        return reply, resumed

    monkeypatch.setattr(mcp, "_chat_resume", fake_resume)
    res = (mcp.approve_run if approve else mcp.reject_run)("p", "r1")
    assert res.reply == reply and res.ok is True
    assert calls == {"project": "p", "run_id": "r1", "approve": approve, "actor": "mcp"}


@pytest.mark.parametrize("approve", [True, False])
def test_failed_approve_reject_is_a_tool_error(projects_dir, runs_dir, approve):
    _write_spec(projects_dir, "p")
    _write_run(runs_dir, "p", "r1", status="pass")  # not needs_approval
    with pytest.raises(ToolError):
        mcp._resume("p", "r1", approve=approve)


def test_approve_run_requires_args_through_protocol():
    res = call(mcp.McpOptions(allow_approvals=True), "approve_run", {"project": "p"})
    assert res.is_error


# ------------------------------------------------------------ tool handlers: start
def test_start_run_unknown_loop_and_project_raise(projects_dir):
    _write_spec(projects_dir, "p", loops={"real": {"steps": [{"type": "shell", "cmd": "true"}]}})
    with pytest.raises(ToolError):
        mcp.start_run("p", "missing")
    with pytest.raises(ToolError):
        mcp.start_run("ghost", "l")


def test_start_run_delegates_to_engine(monkeypatch, projects_dir):
    _write_spec(projects_dir, "p")
    calls = {}

    def fake_start(name, lname, trigger, *, run_id, note="", initial_vars=None, actor=None):
        calls.update(name=name, lname=lname, trigger=trigger, run_id=run_id, note=note, vars=initial_vars, actor=actor)
        return False

    monkeypatch.setattr(mcp, "start_priority_run", fake_start)
    res = mcp.start_run("p", "l", note="hi", vars={"a": "1"})
    assert res.started and not res.queued
    assert (calls["name"], calls["lname"], calls["trigger"], calls["actor"]) == ("p", "l", "mcp", "mcp")
    assert calls["vars"] == {"a": "1"} and calls["run_id"].endswith("-l") and res.run_id == calls["run_id"]


def test_start_run_worker_failure_is_a_tool_error(monkeypatch, projects_dir):
    _write_spec(projects_dir, "p")

    def refuse(*_a, **_k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(mcp, "start_priority_run", refuse)
    with pytest.raises(ToolError, match="worker start failed"):
        mcp.start_run("p", "l")


def test_start_run_rejects_non_object_vars_through_protocol(projects_dir):
    _write_spec(projects_dir, "p")
    assert call(mcp.McpOptions(), "start_run", {"project": "p", "loop": "l", "vars": "not-a-dict"}).is_error


# ---------------------------------------------------------------------- helpers
def test_redact_masks_secret_keys_recursively():
    out = redact_secret_keys({"a": {"api_key": "x", "name": "n"}, "l": [{"password": "p"}], "token": 5, "ok": 1})
    assert out["a"] == {"api_key": MASKED_VALUE, "name": "n"}
    assert out["l"] == [{"password": MASKED_VALUE}] and out["token"] == MASKED_VALUE and out["ok"] == 1
