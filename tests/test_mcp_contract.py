"""Contract test: the real ``fabrikant mcp`` subprocess driven by the SDK's own stdio client.

Runs in the normal CI job (no marker): it needs no browser and no network. Every await is bounded
by ``TIMEOUT_S`` so a hang fails fast instead of eating the job timeout.
"""

from __future__ import annotations

import sys
import textwrap

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

TIMEOUT_S = 60
NOISY_SERVER = textwrap.dedent(
    """
    from fabrikant import mcp_server as m
    real = m.list_projects
    def noisy():
        print("engine chatter that must never reach the protocol stream")
        return real()
    m.list_projects = noisy
    m.serve_stdio(m.McpOptions())
    """
)


@pytest.fixture
def env(tmp_path):
    """Isolated FABRIKANT_DATA (projects) and FABRIKANT_HOME (state) with one valid project."""
    data, home, root = tmp_path / "data", tmp_path / "home", tmp_path / "root"
    (data / "projects").mkdir(parents=True)
    home.mkdir()
    root.mkdir()
    (data / "projects" / "demo.fabrikant.yaml").write_text(
        f"name: demo\nroot: {root}\nloops:\n  l:\n    steps:\n      - {{type: shell, cmd: 'true'}}\n"
    )
    return {
        "FABRIKANT_DATA": str(data),
        "FABRIKANT_HOME": str(home),
        "FABRIKANT_MCP_ALLOW_APPROVALS": "",
        "FABRIKANT_MCP_READONLY": "",
    }


def _params(env, *args: str, code: str | None = None) -> StdioServerParameters:
    argv = ["-c", code] if code else ["-m", "fabrikant", "mcp", *args]
    return StdioServerParameters(command=sys.executable, args=argv, env=env)


async def _session(params, fn):
    with anyio.fail_after(TIMEOUT_S):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _run(params, fn):
    return anyio.run(_session, params, fn)


async def _names(session):
    return {t.name for t in (await session.list_tools()).tools}


def test_initialize_and_tool_list_with_annotations(env):
    async def go(session):
        return (await session.list_tools()).tools

    tools = _run(_params(env), go)
    names = {t.name for t in tools}
    assert {"list_projects", "get_spec", "validate_spec", "start_run", "list_runs"} <= names
    assert not names & {"approve_run", "reject_run", "propose_spec_change", "write_spec_change"}
    for t in tools:
        assert t.annotations is not None and t.annotations.open_world_hint is False
        assert t.annotations.read_only_hint is not None and t.annotations.destructive_hint is not None
        assert t.output_schema is not None


def test_flags_reveal_and_hide_tools(env):
    approvals = _run(_params(env, "--allow-approvals"), _names)
    assert {"approve_run", "reject_run"} <= approvals and "propose_spec_change" not in approvals
    spec_write = _run(_params(env, "--allow-spec-write"), _names)
    assert {"propose_spec_change", "list_spec_proposals"} <= spec_write and "write_spec_change" not in spec_write
    direct = _run(_params(env, "--allow-spec-write", "--spec-write-direct"), _names)
    assert "write_spec_change" in direct
    read_only = _run(_params(env, "--read-only", "--allow-approvals", "--allow-spec-write"), _names)
    assert "start_run" not in read_only and not read_only & {"approve_run", "propose_spec_change"}
    via_env = _run(_params({**env, "FABRIKANT_MCP_ALLOW_SPEC_WRITE": "1"}), _names)
    assert "propose_spec_change" in via_env


def test_read_tool_and_validate_spec(env):
    async def go(session):
        projects = await session.call_tool("list_projects", {})
        spec = await session.call_tool("get_spec", {"project": "demo"})
        valid = await session.call_tool("validate_spec", {"project": "demo"})
        missing = await session.call_tool("get_run", {"run_id": "ghost"})
        return projects, spec, valid, missing

    projects, spec, valid, missing = _run(_params(env), go)
    assert [p["name"] for p in projects.structured_content["projects"]] == ["demo"]
    assert spec.structured_content["_hash"] and "_path" not in spec.structured_content
    assert valid.structured_content["valid"] is True and valid.structured_content["count"] == 1
    assert missing.is_error


def test_propose_over_the_wire_writes_nothing(env, tmp_path):
    async def go(session):
        h = (await session.call_tool("get_spec", {"project": "demo"})).structured_content["_hash"]
        return await session.call_tool(
            "propose_spec_change", {"project": "demo", "base_hash": h, "patch": {"description": "changed"}}
        )

    res = _run(_params(env, "--allow-spec-write"), go)
    assert not res.is_error and res.structured_content["status"] == "pending"
    assert "changed" not in (tmp_path / "data" / "projects" / "demo.fabrikant.yaml").read_text()


def test_engine_prints_do_not_corrupt_the_stream(env):
    async def go(session):
        return await session.call_tool("list_projects", {})

    res = _run(_params(env, code=NOISY_SERVER), go)
    assert not res.is_error and res.structured_content["count"] == 1
