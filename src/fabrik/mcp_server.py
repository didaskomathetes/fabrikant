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
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

from . import config, engine
from .chat import _chat_resume
from .engine import start_priority_run
from .runs import (
    get_run,
    get_run_log,
    list_runs,
    load_records,
    pending_approvals,
    reap_orphaned_runs,
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
SUPPORTED_PROTOCOLS = (PROTOCOL_VERSION, "2024-11-05")
READ_ONLY_ENV = "FABRIK_MCP_READONLY"  # truthy → mutating tools are hidden and refused
ALLOW_APPROVALS_ENV = "FABRIK_MCP_ALLOW_APPROVALS"  # truthy → approve_run/reject_run are offered (opt-in)
_TRUTHY = ("1", "true", "yes", "on")
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 1000
DEFAULT_LOG_LINES = 2000  # bound for get_run_log when `lines` is omitted
DRAIN_TIMEOUT_S = 600  # on stdin EOF, wait this long for in-flight run workers
REDACTED = "***redacted***"
_SECRET_KEY_RE = re.compile(r"token|secret|passw|api[_-]?key|credential|authorization|private[_-]?key", re.I)

# JSON-RPC error codes.
ERR_PARSE, ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND, ERR_INVALID_PARAMS, ERR_INTERNAL = (
    -32700,
    -32600,
    -32601,
    -32602,
    -32603,
)


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


def _require(args: dict, *keys: str) -> tuple[str, ...]:
    """Return the named arguments as strings or raise ``ToolError`` if any is missing/empty."""
    values = tuple(str(args.get(k) or "") for k in keys)
    if not all(values):
        raise ToolError(" + ".join(keys) + " required")
    return values


def _int_arg(args: dict, key: str, default: int | None, *, minimum: int = 1, maximum: int | None = None) -> int | None:
    """Validated integer argument (rejects bool/float/str garbage and out-of-range values)."""
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ToolError(f"{key} must be {bound}")
    return value


def _redact(obj: Any) -> Any:
    """Recursively mask values of secret-looking keys (token, password, api key, ...)."""
    if isinstance(obj, dict):
        return {
            k: REDACTED if _SECRET_KEY_RE.search(str(k)) and isinstance(v, (str, int)) else _redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _read_only() -> bool:
    return _env_flag(READ_ONLY_ENV)


def _approvals_enabled() -> bool:
    """Approve/reject are the human gate; a prompt-injected agent must not get them by default."""
    return _env_flag(ALLOW_APPROVALS_ENV) and not _read_only()


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
    (name,) = _require(args, "project")
    spec = _known_project(name)
    out = _redact({k: v for k, v in spec.items() if k != "_path"})  # no filesystem paths / secrets
    out["_hash"] = spec_hash(spec_path(name))
    return out


def _get_raw_spec(args: dict) -> dict:
    """Raw (un-expanded) spec of a project plus content hash (edit source)."""
    (name,) = _require(args, "project")
    _known_project(name)
    path = spec_path(name)
    if not path.is_file():
        raise ToolError(f"spec file not found for project {name!r}")
    raw = _redact(load_spec_raw(path))
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
    limit = _int_arg(args, "limit", DEFAULT_LIST_LIMIT, maximum=MAX_LIST_LIMIT)
    status = args.get("status") or None
    runs = list_runs(project, limit)
    if status:
        runs = [r for r in runs if r.get("status") == status]
    return {"runs": runs, "count": len(runs)}


def _get_run(args: dict) -> dict:
    """One run record by id (falls back to all projects if project omitted)."""
    (run_id,) = _require(args, "run_id")
    rec = get_run(run_id, args.get("project") or None)
    if rec is None:
        raise ToolError(f"run not found: {run_id!r}")
    return rec


def _get_run_log(args: dict) -> dict:
    """Run log as plain text; optional ``lines`` tail (like ``?lines=N``)."""
    (run_id,) = _require(args, "run_id")
    log = get_run_log(run_id, args.get("project") or None)
    if log is None:
        raise ToolError(f"no log for run: {run_id!r}")
    lines = _int_arg(args, "lines", DEFAULT_LOG_LINES) or DEFAULT_LOG_LINES
    all_lines = log.splitlines()
    truncated = len(all_lines) > lines
    return {"run_id": run_id, "log": "\n".join(all_lines[-lines:]) if truncated else log, "truncated": truncated}


def _start_run(args: dict) -> dict:
    """Start a loop run (mirrors ``POST /api/runs``); queued if at the limit."""
    name, lname = _require(args, "project", "loop")
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
        "pool": engine.pool_status(),
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


def _resume(args: dict, *, approve: bool) -> dict:
    """Shared approve/reject: failures (unknown run, wrong status, ...) become tool errors."""
    name, run_id = _require(args, "project", "run_id")
    reply, resumed = _chat_resume(name, run_id, approve, actor=MCP_ACTOR)
    if reply.lstrip().startswith(("✖", "⚠")):
        raise ToolError(reply)
    return {"ok": resumed if approve else True, "reply": reply, "run_id": run_id}


def _approve_run(args: dict) -> dict:
    """Approve a ``needs_approval`` run (resumes it; atomic, audited as MCP)."""
    return _resume(args, approve=True)


def _reject_run(args: dict) -> dict:
    """Reject a ``needs_approval`` run (ends the loop here; atomic, audited)."""
    return _resume(args, approve=False)


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
        "mutating": True,
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
        "mutating": True,
        "approval": True,
    },
    {
        "name": "reject_run",
        "description": "Reject a needs_approval run: ends the loop here (atomic, audited as actor 'mcp').",
        "inputSchema": _schema({"project": _STR, "run_id": _STR}, required=("project", "run_id")),
        "handler": _reject_run,
        "mutating": True,
        "approval": True,
    },
]


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch.
# --------------------------------------------------------------------------- #


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _visible_tools() -> list[dict]:
    """Tools offered to the client: mutating ones are hidden in read-only mode, and the
    approval tools only appear when explicitly enabled (opt-in)."""
    return [
        t
        for t in TOOLS
        if not (t.get("mutating") and _read_only()) and not (t.get("approval") and not _approvals_enabled())
    ]


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RpcError(ERR_INVALID_PARAMS, "arguments must be an object")
    tool = next((t for t in _visible_tools() if t["name"] == name), None)
    if tool is None:
        raise RpcError(ERR_INVALID_PARAMS, f"unknown tool: {name!r}")
    try:
        data = tool["handler"](args)
    except ToolError as e:
        return _tool_error(str(e))
    except (KeyError, ValueError, TypeError) as e:
        return _tool_error(f"bad arguments: {e}")
    except Exception as e:  # a failing tool must never take the long-lived server down
        traceback.print_exc(file=sys.stderr)
        return _tool_error(f"internal error: {type(e).__name__}: {e}")
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}


def _initialize(params: dict) -> dict:
    wanted = params.get("protocolVersion")
    return {
        "protocolVersion": wanted if wanted in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle(req: dict) -> dict | None:
    """Dispatch one JSON-RPC request; None = notification (no response)."""
    method = req.get("method")
    if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
        raise RpcError(ERR_INVALID_REQUEST, "invalid request")
    params = req.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(ERR_INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        return _initialize(params)
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in _visible_tools()
            ]
        }
    if method == "tools/call":
        return _call_tool(params)
    raise RpcError(ERR_METHOD_NOT_FOUND, f"method not found: {method!r}")


def _envelope(req_id: Any, payload: dict) -> dict:
    """JSON-RPC 2.0 envelope; errors without a usable id carry ``"id": null`` as the spec requires."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    msg["error" if "code" in payload else "result"] = payload
    return msg


def _send(req_id: Any, payload: dict, out: Any = None) -> None:
    """Write one JSON-RPC envelope as a newline-terminated line."""
    _write(_envelope(req_id, payload), out)


def _write(message: Any, out: Any = None) -> None:
    out = out or sys.stdout
    out.write(json.dumps(message, ensure_ascii=False) + "\n")
    out.flush()


def _valid_id(req_id: Any) -> bool:
    return req_id is None or (isinstance(req_id, (str, int, float)) and not isinstance(req_id, bool))


def _process(req: Any) -> dict | None:
    """One request object → response envelope, or None for notifications."""
    if not isinstance(req, dict):
        return _envelope(None, {"code": ERR_INVALID_REQUEST, "message": "invalid request"})
    req_id = req.get("id")
    is_notification = "id" not in req
    if not _valid_id(req_id):
        return _envelope(None, {"code": ERR_INVALID_REQUEST, "message": "invalid id"})
    try:
        result = _handle(req)
    except RpcError as e:
        return None if is_notification else _envelope(req_id, {"code": e.code, "message": e.message})
    except Exception as e:  # never let one bad request kill the server
        traceback.print_exc(file=sys.stderr)
        if is_notification:
            return None
        return _envelope(req_id, {"code": ERR_INTERNAL, "message": f"internal error: {type(e).__name__}"})
    if is_notification:
        return None
    return _envelope(req_id, result if result is not None else {})


def _process_line(line: str) -> Any:
    """One input line → response (envelope, list for batches, or None)."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return _envelope(None, {"code": ERR_PARSE, "message": "parse error"})
    if isinstance(data, list):
        if not data:
            return _envelope(None, {"code": ERR_INVALID_REQUEST, "message": "empty batch"})
        responses = [r for r in (_process(item) for item in data) if r is not None]
        return responses or None
    return _process(data)


def _startup_recovery() -> None:
    """Reap ``running`` records left by a previous process (same as the HTTP server start)."""
    try:
        for rid in reap_orphaned_runs():
            print(f"[fabrik] reaped orphaned run {rid}", file=sys.stderr)
    except Exception:  # recovery is best effort; the server must still come up
        traceback.print_exc(file=sys.stderr)


def _drain_workers(baseline: set[threading.Thread], timeout_s: float = DRAIN_TIMEOUT_S) -> None:
    """On shutdown, give run workers started through this server time to finish."""
    deadline = threading.Event()
    timer = threading.Timer(timeout_s, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        for t in threading.enumerate():
            if t in baseline or t is timer or t is threading.current_thread():
                continue
            while t.is_alive() and not deadline.is_set():
                t.join(timeout=0.5)
    finally:
        timer.cancel()


def serve_stdio() -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout.

    stdout carries *only* protocol messages: the real stdout is kept for replies
    and ``sys.stdout`` is pointed at stderr, so ``print()`` calls deep in the
    engine cannot corrupt the stream.
    """
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)  # real text streams only (not StringIO)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    out = sys.stdout
    sys.stdout = sys.stderr
    baseline = set(threading.enumerate())
    try:
        _startup_recovery()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            response = _process_line(line)
            if response is not None:
                _write(response, out)
        _drain_workers(baseline)
    finally:
        sys.stdout = out


def main() -> None:
    """Console-script entry point (``fabrik-mcp`` / ``fabrik mcp``)."""
    serve_stdio()


if __name__ == "__main__":
    main()
