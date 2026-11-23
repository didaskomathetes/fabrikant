"""MCP (Model Context Protocol) server — fabrikant as a stdio MCP endpoint.

Roadmap #132: expose runs, specs and approvals to any MCP-capable agent.

Built on the official MCP Python SDK (``mcp``, ``MCPServer``): the SDK owns the protocol, the
transport and the stdout hygiene (it diverts fd 1 to stderr while serving, so an engine
``print()`` cannot corrupt the stream). This module keeps what is fabrikant-specific: the tool
set, its gating flags, redaction, input validation, audit actor and process lifecycle.

The server is a *long-lived* process: the MCP client (Claude Desktop, opencode, ...) starts
``fabrikant mcp`` and keeps it running. Run/approval actions spawn worker threads inside this
process exactly like the HTTP server does, so keep it alive while a triggered run executes;
if the client kills it, the next fabrikant server start reaps the orphaned runs.

Tools are *registered* only when their gate is open, so a hidden tool is also refused at call
time (unknown tool). Gates: ``--read-only`` hides everything that changes state and wins over
the opt-ins ``--allow-approvals`` and ``--allow-spec-write``.

Run it with ``fabrikant mcp`` or the ``fabrikant-mcp`` console script.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import config, engine, spec_proposals
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

SERVER_NAME = "fabrikant"
try:
    SERVER_VERSION = importlib.metadata.version("fabrikant")
except importlib.metadata.PackageNotFoundError:  # running from a bare checkout
    SERVER_VERSION = "0.0.0+unknown"
MCP_ACTOR = "mcp"  # audit actor for actions taken through the MCP endpoint
READ_ONLY_ENV = "FABRIKANT_MCP_READONLY"  # truthy → mutating tools are hidden and refused
ALLOW_APPROVALS_ENV = "FABRIKANT_MCP_ALLOW_APPROVALS"  # truthy → approve_run/reject_run are offered (opt-in)
ALLOW_SPEC_WRITE_ENV = "FABRIKANT_MCP_ALLOW_SPEC_WRITE"  # truthy → propose_spec_change & co. are offered (opt-in)
_TRUTHY = ("1", "true", "yes", "on")
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 1000
DEFAULT_LOG_LINES = 2000  # bound for get_run_log when `lines` is omitted
DRAIN_TIMEOUT_S = 600  # on stdin EOF, wait this long for in-flight run workers
REDACTED = "***redacted***"
_SECRET_KEY_RE = re.compile(r"token|secret|passw|api[_-]?key|credential|authorization|private[_-]?key", re.I)

# --------------------------------------------------------------------------- #
# Options / gating.
# --------------------------------------------------------------------------- #


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class McpOptions:
    """Which tool groups this server instance offers (CLI flags and env vars, OR-ed)."""

    read_only: bool = False
    allow_approvals: bool = False
    allow_spec_write: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        read_only: bool = False,
        allow_approvals: bool = False,
        allow_spec_write: bool = False,
    ) -> McpOptions:
        """Options from the environment; a ``True`` argument (CLI flag) switches the option on."""
        return cls(
            read_only=read_only or _env_flag(READ_ONLY_ENV),
            allow_approvals=allow_approvals or _env_flag(ALLOW_APPROVALS_ENV),
            allow_spec_write=allow_spec_write or _env_flag(ALLOW_SPEC_WRITE_ENV),
        )

    def offers(self, gate: Gate) -> bool:
        """Whether tools behind ``gate`` are registered. Read-only wins over every opt-in."""
        if gate is Gate.READ:
            return True
        if self.read_only:
            return False
        return {
            Gate.MUTATING: True,
            # Approve/reject are the human gate; a prompt-injected agent must not get them by default.
            Gate.APPROVAL: self.allow_approvals,
            Gate.SPEC_WRITE: self.allow_spec_write,
        }[gate]


class Gate(StrEnum):
    """Condition under which a tool is offered."""

    READ = "read"
    MUTATING = "mutating"
    APPROVAL = "approval"
    SPEC_WRITE = "spec_write"


# --------------------------------------------------------------------------- #
# Typed arguments and results (the SDK derives input/output JSON schemas from these).
# --------------------------------------------------------------------------- #

# Strict: bool/float/numeric-string garbage is rejected instead of coerced.
_Limit = Annotated[int, Field(strict=True, ge=1, le=MAX_LIST_LIMIT, description="Maximum number of runs")]
_LogLines = Annotated[int, Field(strict=True, ge=1, description="Return only the last N lines")]
_Project = Annotated[str, Field(min_length=1, description="Project name")]
_RunId = Annotated[str, Field(min_length=1, description="Run id")]
_OptProject = Annotated[str | None, Field(description="Project name (default: all projects)")]


class ProjectInfo(BaseModel):
    """One project in ``list_projects``."""

    name: str
    description: str
    error: str | None = None


class ProjectList(BaseModel):
    """Result of ``list_projects``."""

    projects: list[ProjectInfo]
    count: int


class ValidationResult(BaseModel):
    """Validation outcome for one project."""

    project: str
    valid: bool
    errors: list[str]


class ValidationReport(BaseModel):
    """Result of ``validate_spec``."""

    valid: bool
    results: list[ValidationResult]
    count: int


class RunList(BaseModel):
    """Result of ``list_runs``."""

    runs: list[dict[str, Any]]
    count: int


class RunLog(BaseModel):
    """Result of ``get_run_log``."""

    run_id: str
    log: str
    truncated: bool


class StartedRun(BaseModel):
    """Result of ``start_run``."""

    started: bool
    queued: bool
    project: str
    loop: str
    run_id: str


class RunBrief(BaseModel):
    """A run in ``queue_status``."""

    id: str | None = None
    project: str | None = None
    loop: str | None = None
    trigger: str | None = None
    started: str | None = None


class PoolStatus(BaseModel):
    """Worker pool of the run launcher (mirrors ``LauncherStatus``)."""

    workers: int = 0
    busy: int = 0
    queue: int = 0


class QueueStatus(BaseModel):
    """Result of ``queue_status``."""

    pool: PoolStatus
    running: list[RunBrief]
    queued: list[RunBrief]
    needs_approval: list[RunBrief]


class ApprovalInfo(BaseModel):
    """An open approval request."""

    id: str
    project: str | None = None
    loop: str | None = None
    started: str | None = None
    note: str = ""
    approval: dict[str, Any]


class ApprovalList(BaseModel):
    """Result of ``list_approvals``."""

    approvals: list[ApprovalInfo]
    count: int


class ResumeResult(BaseModel):
    """Result of ``approve_run`` / ``reject_run``."""

    ok: bool
    reply: str
    run_id: str


class SpecPatch(BaseModel):
    """A change to a project spec (same shape as ``PUT /api/projects/<name>/spec``).

    ``loops`` are merged per loop into the existing spec (unknown keys of a loop are kept),
    ``remove_loops`` deletes loops, ``description`` / ``max_parallel`` update top-level fields.
    """

    model_config = ConfigDict(extra="forbid")

    loops: dict[str, dict[str, Any]] | None = None
    remove_loops: list[str] = Field(default_factory=list)
    description: str | None = None
    max_parallel: Annotated[int | None, Field(strict=True)] = None

    def to_payload(self) -> dict[str, Any]:
        """Payload for :meth:`fabrikant.specs.SpecUpdate.from_payload` (unset fields omitted)."""
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class ProposalInfo(BaseModel):
    """A spec-change proposal. Never echoes spec contents (they may hold secrets)."""

    id: str
    project: str
    status: spec_proposals.ProposalStatus
    changes: list[str]
    proposed_by: str
    next_step: str = ""


class ProposalList(BaseModel):
    """Result of ``list_spec_proposals``."""

    proposals: list[ProposalInfo]
    count: int


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Read tools.
# --------------------------------------------------------------------------- #


def list_projects() -> ProjectList:
    """List all fabrikant projects (YAML specs) with their description."""
    out = []
    for name, spec in load_projects().items():
        if isinstance(spec, dict) and spec.get("_error"):
            out.append(ProjectInfo(name=name, description="", error=spec["_error"]))
        else:
            out.append(ProjectInfo(name=name, description=(spec or {}).get("description", "")))
    return ProjectList(projects=out, count=len(out))


def get_spec(project: _Project) -> dict[str, Any]:
    """Expanded spec of a project (loops, steps, triggers) plus content hash (``_hash``)."""
    spec = _known_project(project)
    out = _redact({k: v for k, v in spec.items() if k != "_path"})  # no filesystem paths / secrets
    out["_hash"] = spec_hash(spec_path(project))
    return out


def get_raw_spec(project: _Project) -> dict[str, Any]:
    """Raw (un-expanded) spec of a project plus content hash (``_hash``, the edit source)."""
    _known_project(project)
    path = spec_path(project)
    if not path.is_file():
        raise ToolError(f"spec file not found for project {project!r}")
    raw = _redact(load_spec_raw(path))
    raw["_hash"] = spec_hash(path)
    return raw


def validate_specs(
    project: Annotated[str | None, Field(description="One project; omit for all")] = None,
) -> ValidationReport:
    """Validate project specs against the JSON schema (#131), like ``fabrikant validate``.

    The schema describes the *expanded* spec, so this validates the loaded (post
    vars/template/relay expansion) project specs — not raw YAML.
    """
    specs = load_projects()
    if project:
        if project not in specs:
            raise ToolError(f"unknown project: {project!r} (have: {', '.join(sorted(specs)) or 'none'})")
        targets = {project: specs[project]}
    else:
        targets = specs
    results = []
    for n, spec in targets.items():
        if not isinstance(spec, dict) or spec.get("_error"):
            err = (spec.get("_error") if isinstance(spec, dict) else None) or "failed to load"
            results.append(ValidationResult(project=n, valid=False, errors=[str(err)]))
            continue
        errors = validate_spec(spec)
        results.append(ValidationResult(project=n, valid=not errors, errors=errors))
    return ValidationReport(valid=all(r.valid for r in results), results=results, count=len(results))


def list_runs_tool(
    project: _OptProject = None,
    status: Annotated[str | None, Field(description="Only runs with this status")] = None,
    limit: _Limit | None = None,
) -> RunList:
    """Run records, newest first; optional project / status / limit filters.

    The status filter is applied *before* the limit, so ``status`` + ``limit`` return up to
    ``limit`` runs of that status (not the first ``limit`` runs, of which some may match).
    """
    limit = limit or DEFAULT_LIST_LIMIT
    runs = list_runs(project or None, None if status else limit)
    if status:
        runs = [r for r in runs if r.get("status") == status][:limit]
    return RunList(runs=runs, count=len(runs))


def get_run_tool(run_id: _RunId, project: _OptProject = None) -> dict[str, Any]:
    """One run record by id (falls back to all projects if project omitted)."""
    rec = get_run(run_id, project or None)
    if rec is None:
        raise ToolError(f"run not found: {run_id!r}")
    return rec


def get_run_log_tool(run_id: _RunId, project: _OptProject = None, lines: _LogLines | None = None) -> RunLog:
    """Run log as plain text, bounded to the last ``lines`` lines (default 2000)."""
    log = get_run_log(run_id, project or None)
    if log is None:
        raise ToolError(f"no log for run: {run_id!r}")
    lines = lines or DEFAULT_LOG_LINES
    all_lines = log.splitlines()
    truncated = len(all_lines) > lines
    return RunLog(run_id=run_id, log="\n".join(all_lines[-lines:]) if truncated else log, truncated=truncated)


def queue_status() -> QueueStatus:
    """Worker-pool + run-queue snapshot (running / queued / needs_approval)."""
    records = load_records()

    def brief(status: str) -> list[RunBrief]:
        return [RunBrief(**{k: r.get(k) for k in RunBrief.model_fields}) for r in records if r.get("status") == status]

    return QueueStatus(
        pool=PoolStatus(**engine.pool_status()),
        running=brief("running"),
        queued=brief("queued"),
        needs_approval=brief("needs_approval"),
    )


def list_approvals(project: _OptProject = None) -> ApprovalList:
    """Open approval requests: runs in status ``needs_approval``, newest first."""
    approvals = [
        ApprovalInfo(
            id=r["id"],
            project=r.get("project"),
            loop=r.get("loop"),
            started=r.get("started"),
            note=r.get("note", ""),
            approval=r.get("approval") or {},
        )
        for r in pending_approvals(project or None)
    ]
    return ApprovalList(approvals=approvals, count=len(approvals))


# --------------------------------------------------------------------------- #
# Mutating tools.
# --------------------------------------------------------------------------- #


def start_run(
    project: _Project,
    loop: Annotated[str, Field(min_length=1, description="Loop name")],
    note: str = "",
    vars: Annotated[dict[str, str] | None, Field(description="Initial variables (K=V)")] = None,
) -> StartedRun:
    """Start a loop run (mirrors ``POST /api/runs``); queued if at the parallel limit."""
    loops = _known_project(project).get("loops", {})
    if loop not in loops:
        raise ToolError(f"unknown loop: {loop!r} (available: {', '.join(sorted(loops)) or 'none'})")
    run_id = f"{config.now_id()}-{loop}"
    try:
        queued = start_priority_run(
            project, loop, "mcp", run_id=run_id, note=note, initial_vars=dict(vars or {}) or None, actor=MCP_ACTOR
        )
    except RuntimeError as e:
        raise ToolError(f"worker start failed: {e}") from e
    return StartedRun(started=not queued, queued=queued, project=project, loop=loop, run_id=run_id)


# Failure markers of _chat_resume replies: every failure message in the chat.* catalog
# (run_not_found, not_needs_approval, cannot_approve, cannot_reject) starts with one of
# these; success replies (approved, rejected, already approved) never do. If the catalog
# changes, this check must change with it.
_RESUME_FAILURE_MARKERS = ("✖", "⚠")


def _resume(project: str, run_id: str, *, approve: bool) -> ResumeResult:
    """Shared approve/reject: failures (unknown run, wrong status, ...) become tool errors."""
    reply, resumed = _chat_resume(project, run_id, approve, actor=MCP_ACTOR)
    if reply.lstrip().startswith(_RESUME_FAILURE_MARKERS):
        raise ToolError(reply)
    return ResumeResult(ok=resumed if approve else True, reply=reply, run_id=run_id)


def approve_run(project: _Project, run_id: _RunId) -> ResumeResult:
    """Approve a ``needs_approval`` run: resumes it (atomic, audited as actor 'mcp')."""
    return _resume(project, run_id, approve=True)


def reject_run(project: _Project, run_id: _RunId) -> ResumeResult:
    """Reject a ``needs_approval`` run: ends the loop here (atomic, audited as actor 'mcp')."""
    return _resume(project, run_id, approve=False)


# --------------------------------------------------------------------------- #
# Spec-write tools (opt-in; the human approves via `fabrikant proposals apply`).
# --------------------------------------------------------------------------- #

_BaseHash = Annotated[str, Field(min_length=1, description="The `_hash` returned by get_spec / get_raw_spec")]


def _proposal_info(proposal: spec_proposals.SpecProposal) -> ProposalInfo:
    next_step = (
        f"a human applies it: fabrikant proposals apply {proposal.id}"
        if proposal.status is spec_proposals.ProposalStatus.PENDING
        else ""
    )
    return ProposalInfo(
        id=proposal.id,
        project=proposal.project,
        status=proposal.status,
        changes=proposal.changes,
        proposed_by=proposal.proposed_by,
        next_step=next_step,
    )


def propose_spec_change(project: _Project, base_hash: _BaseHash, patch: SpecPatch) -> ProposalInfo:
    """Propose a spec change for human approval; nothing is written until a human applies it.

    The change is validated against the current spec (full schema validation) and refused if
    invalid or if ``base_hash`` is stale.
    """
    try:
        return _proposal_info(spec_proposals.propose(project, patch.to_payload(), base_hash, MCP_ACTOR))
    except spec_proposals.ProposalError as e:
        raise ToolError(str(e)) from e


def list_spec_proposals(
    status: Annotated[spec_proposals.ProposalStatus | None, Field(description="Only this status")] = None,
) -> ProposalList:
    """List spec-change proposals (pending / applied / rejected), newest first."""
    proposals = [_proposal_info(p) for p in spec_proposals.list_proposals(status)]
    return ProposalList(proposals=proposals, count=len(proposals))


# --------------------------------------------------------------------------- #
# Tool registry.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolDef:
    """A tool: handler, MCP name, description, annotations and the gate that offers it."""

    name: str
    fn: Callable[..., Any]
    gate: Gate
    annotations: ToolAnnotations
    description: str = ""


def _hints(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    """Annotations; fabrikant only ever touches its own state, never an open world."""
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


_READ = _hints(read_only=True, idempotent=True)

TOOLS: tuple[ToolDef, ...] = (
    ToolDef("list_projects", list_projects, Gate.READ, _READ),
    ToolDef("get_spec", get_spec, Gate.READ, _READ),
    ToolDef("get_raw_spec", get_raw_spec, Gate.READ, _READ),
    ToolDef(
        "validate_spec",
        validate_specs,
        Gate.READ,
        _READ,
        "Validate project specs against the JSON schema (like `fabrikant validate`). Pass `project` for one "
        "project, or omit it to validate every project. Returns valid + per-project errors.",
    ),
    ToolDef("list_runs", list_runs_tool, Gate.READ, _READ),
    ToolDef("get_run", get_run_tool, Gate.READ, _READ),
    ToolDef("get_run_log", get_run_log_tool, Gate.READ, _READ),
    ToolDef("queue_status", queue_status, Gate.READ, _READ),
    ToolDef("list_approvals", list_approvals, Gate.READ, _READ),
    ToolDef("start_run", start_run, Gate.MUTATING, _hints()),
    ToolDef("approve_run", approve_run, Gate.APPROVAL, _hints()),
    ToolDef("reject_run", reject_run, Gate.APPROVAL, _hints(destructive=True)),
    ToolDef("propose_spec_change", propose_spec_change, Gate.SPEC_WRITE, _hints()),
    ToolDef("list_spec_proposals", list_spec_proposals, Gate.SPEC_WRITE, _READ),
)


def build_server(options: McpOptions | None = None) -> MCPServer:
    """MCP server offering the tools ``options`` allow (others are not registered → refused)."""
    options = options or McpOptions.from_env()
    server = MCPServer(SERVER_NAME, version=SERVER_VERSION)
    for tool in TOOLS:
        if options.offers(tool.gate):
            server.add_tool(tool.fn, name=tool.name, description=tool.description or None, annotations=tool.annotations)
    return server


# --------------------------------------------------------------------------- #
# Process lifecycle.
# --------------------------------------------------------------------------- #


def _startup_recovery() -> None:
    """Reap ``running`` records left by a previous process (same as the HTTP server start)."""
    try:
        for rid in reap_orphaned_runs():
            print(f"[fabrikant] reaped orphaned run {rid}", file=sys.stderr)
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


def serve_stdio(options: McpOptions | None = None) -> None:
    """Serve MCP over stdio until the client closes stdin, then drain in-flight run workers.

    The SDK keeps stdout for protocol messages (fd 1 is diverted to stderr meanwhile).
    """
    baseline = set(threading.enumerate())
    _startup_recovery()
    build_server(options).run("stdio")
    _drain_workers(baseline)


def main() -> None:
    """Console-script entry point (``fabrikant-mcp``); ``fabrikant mcp`` adds the CLI flags."""
    serve_stdio()


if __name__ == "__main__":
    main()
