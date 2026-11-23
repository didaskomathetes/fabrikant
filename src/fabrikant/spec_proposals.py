"""Spec-change proposals: an agent proposes, a human applies (MCP spec-write, ADR 0064).

The web chat keeps its spec proposals in the memory of the HTTP server process
(``chat._pending_specs``), which a separate ``fabrikant mcp`` process cannot reach. This module
is the persistent counterpart: a proposal is a small JSON file under ``<state>/spec-proposals``
holding a :class:`~fabrikant.specs.SpecUpdate` payload plus the ``spec_hash`` it was based on.

Every check and the write itself reuse :func:`fabrikant.specs.save_spec` (merge per loop, optimistic
lock, full schema validation, atomic replace, comments preserved). Proposing runs it as a dry
run, so an invalid or stale proposal is refused up front; applying runs it for real (with a
backup of the previous file). Only specs inside ``config.PROJECTS_DIR`` can be changed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from . import config
from .specs import SpecConflict, SpecUpdate, save_spec, spec_path

PROPOSALS_SUBDIR = "spec-proposals"
PROPOSAL_SUFFIX = ".json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # also keeps ids from escaping the store dir


class ProposalError(ValueError):
    """A proposal cannot be created, found or applied (message is safe to show to an agent)."""


class ProposalStatus(StrEnum):
    """Lifecycle of a proposal."""

    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


@dataclass
class SpecProposal:
    """One proposed spec change; ``patch`` is a :meth:`SpecUpdate.from_payload` payload."""

    id: str
    project: str
    base_hash: str
    patch: dict
    proposed_by: str
    created: str
    status: ProposalStatus = ProposalStatus.PENDING
    decided_by: str = ""
    decided: str = ""
    changes: list[str] = field(default_factory=list)  # value-free summary, safe to echo

    @classmethod
    def from_dict(cls, data: dict) -> SpecProposal:
        """Parse a stored record; unknown keys are ignored (forward compatible)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["status"] = ProposalStatus(known.get("status", ProposalStatus.PENDING))
        return cls(**known)

    def to_dict(self) -> dict:
        """JSON-able record."""
        return asdict(self)


def proposals_dir() -> Path:
    """Store directory (resolved lazily: ``config.STATE_DIR`` may be redirected)."""
    return config.STATE_DIR / PROPOSALS_SUBDIR


def summarize(update: SpecUpdate) -> list[str]:
    """Value-free description of a change (names only, never contents: specs may hold secrets)."""
    out = [f"loop {name}: set" for name in sorted(update.loops or {})]
    out += [f"loop {name}: remove" for name in update.remove_loops]
    out += [f"field {key}: set" for key in sorted(update.project_fields)]
    return out


def _spec_file(project: str) -> Path:
    """Spec file of ``project``; refuses anything outside the projects dir."""
    path = spec_path(project).resolve()
    if not path.is_relative_to(config.PROJECTS_DIR.resolve()):
        raise ProposalError(f"spec of {project!r} is outside the projects dir; refusing to change it")
    return path


def _check(project: str, update: SpecUpdate, base_hash: str) -> None:
    """Dry-run the change through ``save_spec``; map its failures to :class:`ProposalError`."""
    _spec_file(project)
    try:
        save_spec(project, update, base_hash=base_hash, dry_run=True)
    except KeyError:
        raise ProposalError(f"unknown project: {project!r}") from None
    except SpecConflict:
        raise ProposalError(
            "stale base_hash: the spec changed since it was read; re-read it and propose again"
        ) from None
    except ValueError as e:
        raise ProposalError(f"invalid change: {e}") from e


def _path(proposal_id: str) -> Path:
    if not _ID_RE.match(proposal_id):
        raise ProposalError(f"invalid proposal id: {proposal_id!r}")
    return proposals_dir() / f"{proposal_id}{PROPOSAL_SUFFIX}"


def _write(proposal: SpecProposal) -> None:
    """Atomic write of the record."""
    target = _path(proposal.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, suffix=".tmp", delete=False, encoding="utf-8") as fd:
        json.dump(proposal.to_dict(), fd, indent=2)
        tmp = Path(fd.name)
    os.replace(tmp, target)


def get_proposal(proposal_id: str) -> SpecProposal:
    """Load a proposal by id (unknown/invalid → :class:`ProposalError`)."""
    path = _path(proposal_id)
    if not path.is_file():
        raise ProposalError(f"unknown proposal: {proposal_id!r}")
    return SpecProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_proposals(status: ProposalStatus | None = None) -> list[SpecProposal]:
    """All proposals, newest first; optionally only those in ``status``."""
    if not proposals_dir().is_dir():
        return []
    found = [SpecProposal.from_dict(json.loads(p.read_text(encoding="utf-8"))) for p in proposals_dir().glob(f"*{PROPOSAL_SUFFIX}")]
    return sorted((p for p in found if status in (None, p.status)), key=lambda p: p.created, reverse=True)


def propose(project: str, patch: dict, base_hash: str, actor: str) -> SpecProposal:
    """Validate a change against the current spec and store it as a pending proposal."""
    try:
        update = SpecUpdate.from_payload(patch)
    except ValueError as e:
        raise ProposalError(f"invalid change: {e}") from e
    if not base_hash:
        raise ProposalError("base_hash required (the spec hash returned by get_spec)")
    _check(project, update, base_hash)
    proposal = SpecProposal(
        id=f"{config.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}",
        project=project,
        base_hash=base_hash,
        patch=patch,
        proposed_by=actor,
        created=config.utcnow().isoformat(),
        changes=summarize(update),
    )
    _write(proposal)
    return proposal


def apply(proposal_id: str, actor: str) -> SpecProposal:
    """Apply a pending proposal for real (backup of the previous file, atomic replace)."""
    proposal = get_proposal(proposal_id)
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(f"proposal {proposal.id} is already {proposal.status}")
    update = SpecUpdate.from_payload(proposal.patch)
    _check(proposal.project, update, proposal.base_hash)
    save_spec(proposal.project, update, base_hash=proposal.base_hash, backup=True)
    return _decide(proposal, ProposalStatus.APPLIED, actor)


def propose_and_apply(project: str, patch: dict, base_hash: str, actor: str) -> SpecProposal:
    """Direct write (explicit opt-in): same checks and audit record as propose + apply."""
    return apply(propose(project, patch, base_hash, actor).id, actor)


def reject(proposal_id: str, actor: str) -> SpecProposal:
    """Reject a pending proposal (kept as an audit record)."""
    proposal = get_proposal(proposal_id)
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(f"proposal {proposal.id} is already {proposal.status}")
    return _decide(proposal, ProposalStatus.REJECTED, actor)


def _decide(proposal: SpecProposal, status: ProposalStatus, actor: str) -> SpecProposal:
    proposal.status = status
    proposal.decided_by = actor
    proposal.decided = config.utcnow().isoformat()
    _write(proposal)
    return proposal
