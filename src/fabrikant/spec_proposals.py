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
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from . import config
from .filelock import locked
from .secrets import MASKED_VALUE, contains_masked_value
from .specs import SpecConflict, SpecUpdate, save_spec, spec_path

PROPOSALS_SUBDIR = "spec-proposals"
PROPOSAL_SUFFIX = ".json"
# Decided proposals are kept forever as audit records. They live in a subdirectory so the
# hot path (the web UI polls /api/projects every few seconds for *pending* proposals) only
# ever reads the small pending set, not the unbounded audit history.
DECIDED_SUBDIR = "decided"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # also keeps ids from escaping the store dir


class ProposalError(ValueError):
    """A proposal cannot be created, found or applied (message is safe to show to an agent)."""


class ProposalNotFound(ProposalError):
    """No proposal with that id exists."""


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
        """Parse a stored record; unknown keys are ignored (forward compatible).

        A record with an unknown status (corruption, newer fabrikant version) raises
        :class:`ProposalError` so one bad file cannot crash ``list_proposals``.
        """
        if not isinstance(data, dict):
            raise ProposalError("corrupted proposal record: not a JSON object")
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        try:
            known["status"] = ProposalStatus(known.get("status", ProposalStatus.PENDING))
        except ValueError as e:
            raise ProposalError(f"corrupted proposal record: unknown status {known.get('status')!r}") from e
        return cls(**known)

    def to_dict(self) -> dict:
        """JSON-able record."""
        return asdict(self)


def proposals_dir() -> Path:
    """Pending-proposals directory (resolved lazily: ``config.STATE_DIR`` may be redirected)."""
    return config.STATE_DIR / PROPOSALS_SUBDIR


def decided_dir() -> Path:
    """Directory for decided (applied/rejected) proposals, kept as audit records."""
    return proposals_dir() / DECIDED_SUBDIR


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


def _map_save_error(e: Exception, project: str) -> ProposalError:
    """Map a ``save_spec`` failure to a :class:`ProposalError` (safe to show to a human/agent)."""
    if isinstance(e, KeyError):
        return ProposalError(f"unknown project: {project!r}")
    if isinstance(e, SpecConflict):  # before ValueError: SpecConflict is a ValueError subclass
        return ProposalError("stale base_hash: the spec changed since it was read; re-read it and propose again")
    if isinstance(e, ValueError):
        return ProposalError(f"invalid change: {e}")
    return ProposalError(f"could not apply the change: {e}")


def _refuse_masked(patch: dict) -> None:
    """Refuse a patch carrying the redaction mask: agents see masked secrets and could echo them back."""
    if contains_masked_value(patch):
        raise ProposalError(
            f"the change contains {MASKED_VALUE!r}: secret values are masked when reading a spec; "
            "send only the fields you want to change and leave secret fields out"
        )


def _check(project: str, update: SpecUpdate, base_hash: str) -> None:
    """Dry-run the change through ``save_spec``; map its failures to :class:`ProposalError`."""
    _spec_file(project)
    try:
        save_spec(project, update, base_hash=base_hash, dry_run=True)
    except (KeyError, SpecConflict, ValueError, OSError) as e:
        raise _map_save_error(e, project) from e


def _path(proposal_id: str, *, decided: bool = False) -> Path:
    if not _ID_RE.match(proposal_id):
        raise ProposalError(f"invalid proposal id: {proposal_id!r}")
    base = decided_dir() if decided else proposals_dir()
    return base / f"{proposal_id}{PROPOSAL_SUFFIX}"


def _write(proposal: SpecProposal, *, decided: bool = False) -> None:
    """Atomic write (into the decided dir once decided); a failed write leaves no temp file."""
    target = _path(proposal.id, decided=decided)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, suffix=".tmp", delete=False, encoding="utf-8") as fd:
        tmp = Path(fd.name)
        try:
            json.dump(proposal.to_dict(), fd, indent=2)
        except BaseException:
            fd.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def get_proposal(proposal_id: str) -> SpecProposal:
    """Load a proposal by id (unknown/invalid → :class:`ProposalError`)."""
    # Decided record first: if a crash left both files, the decision wins over the stale pending copy.
    for path in (_path(proposal_id, decided=True), _path(proposal_id)):
        if path.is_file():
            try:
                return SpecProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except ValueError as e:  # JSONDecodeError is a ValueError
                raise ProposalError(f"corrupt proposal {proposal_id!r}: {e}") from e
    raise ProposalNotFound(f"unknown proposal: {proposal_id!r}")


def _load(dirs: list[Path]) -> list[SpecProposal]:
    """Parse every record in ``dirs``; unparseable files are skipped (one bad file must not
    make the whole listing unusable)."""
    found = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob(f"*{PROPOSAL_SUFFIX}"):
            try:
                found.append(SpecProposal.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (ProposalError, ValueError):
                continue
    return found


def list_proposals(status: ProposalStatus | None = None) -> list[SpecProposal]:
    """Proposals in ``status`` (or all), newest first.

    Pending records live in the top-level dir, decided ones in ``decided/``; only the
    directories that can hold the requested status are read, so the pending hot path never
    touches the (unbounded) audit history. Unparseable records are skipped (one bad file
    must not make the whole listing unusable); ``get_proposal`` still surfaces the error
    for a specific id.
    """
    if status is ProposalStatus.PENDING:
        dirs = [proposals_dir()]
    elif status is None:
        dirs = [proposals_dir(), decided_dir()]
    else:
        dirs = [decided_dir()]
    return sorted(_load(dirs), key=lambda p: p.created, reverse=True)


def propose(project: str, patch: dict, base_hash: str, actor: str) -> SpecProposal:
    """Validate a change against the current spec and store it as a pending proposal."""
    try:
        update = SpecUpdate.from_payload(patch)
    except ValueError as e:
        raise ProposalError(f"invalid change: {e}") from e
    if not base_hash:
        raise ProposalError("base_hash required (the spec hash returned by get_spec)")
    _refuse_masked(patch)
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
    """Apply a pending proposal for real (backup of the previous file, atomic replace).

    A spec that moves on between the dry-run check and the final write (or a filesystem
    error) raises :class:`ProposalError`, so callers (CLI, web UI) see a clean error
    instead of a raw traceback.
    """
    _path(proposal_id)  # validate the id before taking the lock (no stray files on a bad id)
    with _decision_lock():
        proposal = _pending(proposal_id)
        _refuse_masked(proposal.patch)  # also covers records written by an older version
        update = SpecUpdate.from_payload(proposal.patch)
        _check(proposal.project, update, proposal.base_hash)
        try:
            save_spec(proposal.project, update, base_hash=proposal.base_hash, backup=True)
        except (KeyError, SpecConflict, ValueError, OSError) as e:
            raise _map_save_error(e, proposal.project) from e
        return _decide(proposal, ProposalStatus.APPLIED, actor)


def reject(proposal_id: str, actor: str) -> SpecProposal:
    """Reject a pending proposal (kept as an audit record)."""
    _path(proposal_id)
    with _decision_lock():
        return _decide(_pending(proposal_id), ProposalStatus.REJECTED, actor)


def _decision_lock() -> AbstractContextManager[None]:
    """Serialises apply/reject across processes (CLI, web UI, MCP), so a proposal is decided once."""
    return locked(proposals_dir() / "decide")


def _pending(proposal_id: str) -> SpecProposal:
    """The proposal, which must still be pending."""
    proposal = get_proposal(proposal_id)
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(f"proposal {proposal.id} is already {proposal.status}")
    return proposal


def _decide(proposal: SpecProposal, status: ProposalStatus, actor: str) -> SpecProposal:
    proposal.status = status
    proposal.decided_by = actor
    proposal.decided = config.utcnow().isoformat()
    _write(proposal, decided=True)
    # The pending record is replaced by the decided one (moved out of the hot path).
    pending = _path(proposal.id)
    if pending.is_file():
        pending.unlink()
    return proposal
