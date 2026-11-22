"""MCP (Model Context Protocol) server — fabrik as a stdio MCP endpoint.

Roadmap #132: expose runs, specs and approvals to any MCP-capable agent.

Hand-rolled and dependency-free (no ``mcp`` package) to match the "local
software factory" ethos: self-hosted, zero extra dependencies, always
available. Speaks MCP protocol ``2025-03-26`` over stdio using
newline-delimited JSON-RPC 2.0 (one JSON object per line on stdin/stdout).

The server is a *long-lived* process: the MCP client (e.g. Claude Desktop, an
opencode agent, ...) starts ``fabrik mcp`` and keeps it running, exchanging
JSON lines over the pipe. Run/approval actions spawn worker threads inside
this process exactly like the HTTP server does, so keep the process alive
while a triggered run is executing; if the client kills it, the next fabrik
server start reaps the orphaned runs (``reap_orphaned_runs``).

Run it directly with ``fabrik mcp`` or the ``fabrik-mcp`` console script.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from collections.abc import Callable
from typing import Any

from . import config
from .chat import _chat_resume
from .engine import start_priority_run
from .runs import (
    get_run,
    get_run_log,
    list_runs,
    load_records,
    pending_approvals,
)
from .specs import load_projects, load_spec_raw, spec_hash, spec_path
from .specschema import validate_spec

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "fabrik"
try:
    SERVER_VERSION = importlib.metadata.version("fabrik")
except importlib.metadata.PackageNotFoundError:  # running from a bare checkout
    SERVER_VERSION = "0.0.0+unknown"
MCP_ACTOR = "mcp"  # audit actor for actions taken through the MCP endpoint


class ToolError(Exception):
    """A tool-level failure (bad input / not found) → MCP ``isError`` result."""


class RpcError(Exception):
    """A JSON-RPC protocol error (unknown method / bad params)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Tool handlers — each takes the call arguments dict and returns JSON-able data.
# --------------------------------------------------------------------------- #


def _known_project(name: str) -> dict:
    """Load one project spec or raise ``ToolError`` (unknown / failed to load)."""
    specs = load_projects()
    if name not in specs:
        raise ToolError(f"unknown project: {name!r} (have: {', '.join(sorted(specs)) or 'none'})")
    spec = specs[name]
    if not isinstance(spec, dict) or spec.get("_error"):
        raise ToolError(
            f"project {name!r} failed to load: {spec.get('_error') if isinstance(spec, dict) else 'invalid'}"
        )
    return spec


def _list_projects(_args: dict) -> dict:
    """List all fabrik projects (YAML specs) with their description."""
    out = []
    for name, spec in load_projects().items():
        if isinstance(spec, dict) and spec.get("_error"):
            out.append({"name": name, "description": "", "error": spec["_error"]})
        else:
            out.append({"name": name, "description": (spec or {}).get("description", ""), "error": None})
    return {"projects": out, "count": len(out)}


def _get_spec(args: dict) -> dict:
    """Expanded spec of a project (loops, steps, triggers) plus content hash."""
    name = str(args.get("project") or "")
    if not name:
        raise ToolError("project required")
    spec = _known_project(name)
    out = dict(spec)
    out["_hash"] = spec_hash(spec_path(name))
    return out


def _get_raw_spec(args: dict) -> dict:
    """Raw (un-expanded) spec of a project plus content hash (edit source)."""
    name = str(args.get("project") or "")
    if not name:
        raise ToolError("project required")
    _known_project(name)
    path = spec_path(name)
    if not path.is_file():
        raise ToolError(f"spec file not found: {path}")
    raw = load_spec_raw(path)
    raw["_hash"] = spec_hash(path)
    return raw


def _validate_spec(args: dict) -> dict:
    """Validate project specs against the JSON schema (#131), like ``fabrik validate``.

    The schema describes the *expanded* spec, so this validates the loaded
    (post vars/template/relay expansion) project specs — not raw YAML. Pass
    ``project`` for one project, or omit it to validate every project.
    """
    name = str(args.get("project") or "")
    specs = load_projects()
    if name:
        if name not in specs:
            raise ToolError(f"unknown project: {name!r} (have: {', '.join(sorted(specs)) or 'none'})")
        targets = {name: specs[name]}
    else:
        targets = specs
    results = []
    for n, spec in targets.items():
        if not isinstance(spec, dict) or spec.get("_error"):
            err = spec.get("_error") if isinstance(spec, dict) else "failed to load"
            results.append({"project": n, "valid": False, "errors": [err]})
            continue
        errors = validate_spec(spec)
        results.append({"project": n, "valid": not errors, "errors": errors})
    return {"valid": all(r["valid"] for r in results), "results": results, "count": len(results)}


def _list_runs(args: dict) -> dict:
    """Run records, newest first; optional project / status / limit filters."""
    project = args.get("project") or None
    limit = args.get("limit", 50)
    status = args.get("status") or None
    runs = list_runs(project, int(limit) if limit is not None else None)
    if status:
        runs = [r for r in runs if r.get("status") == status]
    return {"runs": runs, "count": len(runs)}


def _get_run(args: dict) -> dict:
    """One run record by id (falls back to all projects if project omitted)."""
    run_id = str(args.get("run_id") or "")
    if not run_id:
        raise ToolError("run_id required")
    rec = get_run(run_id, args.get("project") or None)
    if rec is None:
        raise ToolError(f"run not found: {run_id!r}")
    return rec


def _get_run_log(args: dict) -> dict:
    """Run log as plain text; optional ``lines`` tail (like ``?lines=N``)."""
    run_id = str(args.get("run_id") or "")
    if not run_id:
        raise ToolError("run_id required")
    log = get_run_log(run_id, args.get("project") or None)
    if log is None:
        raise ToolError(f"no log for run: {run_id!r}")
    lines = args.get("lines")
    if lines:
        log = "\n".join(log.splitlines()[-max(1, int(lines)) :])
    return {"run_id": run_id, "log": log}


def _start_run(args: dict) -> dict:
    """Start a loop run (mirrors ``POST /api/runs``); queued if at the limit."""
    name = str(args.get("project") or "")
    lname = str(args.get("loop") or "")
    if not name or not lname:
        raise ToolError("project + loop required")
    spec = _known_project(name)
    loops = spec.get("loops", {})
    if lname not in loops:
        raise ToolError(f"unknown loop: {lname!r} (available: {', '.join(sorted(loops)) or 'none'})")
    note = str(args.get("note") or "")
    raw_vars = args.get("vars")
    if raw_vars is not None and not isinstance(raw_vars, dict):
        raise ToolError("vars must be an object (K=V)")
    initial_vars = {str(k): str(v) for k, v in (raw_vars or {}).items()} or None
    run_id = f"{config.now_id()}-{lname}"
    try:
        queued = start_priority_run(
            name, lname, "mcp", run_id=run_id, note=note, initial_vars=initial_vars, actor=MCP_ACTOR
        )
    except RuntimeError as e:
        raise ToolError(f"worker start failed: {e}") from e
    return {"started": not queued, "queued": queued, "project": name, "loop": lname, "run_id": run_id}


def _queue_status(_args: dict) -> dict:
    """Worker-pool + run-queue snapshot (running / queued / needs_approval)."""
    from .engine import pool_status

    records = load_records()

    def brief(r: dict) -> dict:
        return {
            "id": r.get("id"),
            "project": r.get("project"),
            "loop": r.get("loop"),
            "trigger": r.get("trigger"),
            "started": r.get("started"),
        }

    return {
        "pool": pool_status(),
        "running": [brief(r) for r in records if r.get("status") == "running"],
        "queued": [brief(r) for r in records if r.get("status") == "queued"],
        "needs_approval": [brief(r) for r in records if r.get("status") == "needs_approval"],
    }


def _list_approvals(args: dict) -> dict:
    """Open approval requests: runs in status ``needs_approval``, newest first."""
    project = args.get("project") or None
    approvals = [
        {
            "id": r["id"],
            "project": r.get("project"),
            "loop": r.get("loop"),
            "started": r.get("started"),
            "note": r.get("note", ""),
            "approval": r.get("approval") or {},
        }
        for r in pending_approvals(project)
    ]
    return {"approvals": approvals, "count": len(approvals)}


def _approve_run(args: dict) -> dict:
    """Approve a ``needs_approval`` run (resumes it; atomic, audited as MCP)."""
    name = str(args.get("project") or "")
    run_id = str(args.get("run_id") or "")
    if not name or not run_id:
        raise ToolError("project + run_id required")
    reply, resumed = _chat_resume(name, run_id, True, actor=MCP_ACTOR)
    return {"ok": resumed, "reply": reply, "run_id": run_id}


def _reject_run(args: dict) -> dict:
    """Reject a ``needs_approval`` run (ends the loop here; atomic, audited)."""
    name = str(args.get("project") or "")
    run_id = str(args.get("run_id") or "")
    if not name or not run_id:
        raise ToolError("project + run_id required")
    reply, _resumed = _chat_resume(name, run_id, False, actor=MCP_ACTOR)
    return {"reply": reply, "run_id": run_id}


# --------------------------------------------------------------------------- #
# Tool registry (name + JSON-Schema input + handler).
# --------------------------------------------------------------------------- #

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required)}


ToolHandler = Callable[[dict], Any]

TOOLS: list[dict] = [
    {
        "name": "list_projects",
        "description": "List all fabrik projects (YAML specs) with their description.",
        "inputSchema": _schema({}),
        "handler": _list_projects,
    },
    {
        "name": "get_spec",
        "description": "Get the expanded spec of a project (loops, steps, triggers) plus its content hash.",
        "inputSchema": _schema({"project": _STR}, required=("project",)),
        "handler": _get_spec,
    },
    {
        "name": "get_raw_spec",
        "description": "Get the raw (un-expanded) spec of a project plus its content hash (the edit source).",
        "inputSchema": _schema({"project": _STR}, required=("project",)),
        "handler": _get_raw_spec,
    },
    {
        "name": "validate_spec",
        "description": (
            "Validate project specs against the JSON schema (like `fabrik validate`). "
            "Pass `project` for one project, or omit it to validate every project. "
            "Returns valid + per-project errors."
        ),
        "inputSchema": _schema({"project": _STR}),
        "handler": _validate_spec,
    },
    {
        "name": "list_runs",
        "description": "List run records, newest first. Optional project, status and limit filters.",
        "inputSchema": _schema({"project": _STR, "status": _STR, "limit": _INT}),
        "handler": _list_runs,
    },
    {
        "name": "get_run",
        "description": "Get one run record by id (falls back to all projects when project is omitted).",
        "inputSchema": _schema({"run_id": _STR, "project": _STR}, required=("run_id",)),
        "handler": _get_run,
    },
    {
        "name": "get_run_log",
        "description": "Get a run's log as plain text; optional `lines` returns only the last N lines.",
        "inputSchema": _schema({"run_id": _STR, "project": _STR, "lines": _INT}, required=("run_id",)),
        "handler": _get_run_log,
    },
    {
        "name": "start_run",
        "description": "Start a loop run (like POST /api/runs). Queued if the project is at its parallel limit.",
        "inputSchema": _schema(
            {
                "project": _STR,
                "loop": _STR,
                "note": _STR,
                "vars": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            required=("project", "loop"),
        ),
        "handler": _start_run,
    },
    {
        "name": "queue_status",
        "description": "Worker-pool and run-queue snapshot: running / queued / needs_approval runs.",
        "inputSchema": _schema({}),
        "handler": _queue_status,
    },
    {
        "name": "list_approvals",
        "description": "List open approval requests (runs in status needs_approval), newest first.",
        "inputSchema": _schema({"project": _STR}),
        "handler": _list_approvals,
    },
    {
        "name": "approve_run",
        "description": "Approve a needs_approval run: resumes it (atomic, audited as actor 'mcp').",
        "inputSchema": _schema({"project": _STR, "run_id": _STR}, required=("project", "run_id")),
        "handler": _approve_run,
    },
    {
        "name": "reject_run",
        "description": "Reject a needs_approval run: ends the loop here (atomic, audited as actor 'mcp').",
        "inputSchema": _schema({"project": _STR, "run_id": _STR}, required=("project", "run_id")),
        "handler": _reject_run,
    },
]


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch.
# --------------------------------------------------------------------------- #


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise RpcError(-32602, f"unknown tool: {name!r}")
    try:
        data = tool["handler"](args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True}
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}


def _handle(req: dict) -> dict | None:
    """Dispatch one JSON-RPC request; None = notification (no response)."""
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in TOOLS
            ]
        }
    if method == "tools/call":
        return _call_tool(params)
    raise RpcError(-32601, f"method not found: {method!r}")


def _send(req_id: Any, payload: dict) -> None:
    """Write one JSON-RPC envelope as a newline-terminated line to stdout."""
    msg: dict[str, Any] = {"jsonrpc": "2.0"}
    if req_id is not None:
        msg["id"] = req_id
    if "code" in payload:
        msg["error"] = payload
    else:
        msg["result"] = payload
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio() -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send(None, {"code": -32700, "message": "parse error"})
            continue
        if not isinstance(req, dict):
            _send(None, {"code": -32600, "message": "invalid request"})
            continue
        req_id = req.get("id")
        is_notification = "id" not in req
        try:
            result = _handle(req)
        except RpcError as e:
            if not is_notification:
                _send(req_id, {"code": e.code, "message": e.message})
            continue
        if is_notification or result is None:
            continue
        _send(req_id, result)


def main() -> None:
    """Console-script entry point (``fabrik-mcp`` / ``fabrik mcp``)."""
    serve_stdio()


if __name__ == "__main__":
    main()
