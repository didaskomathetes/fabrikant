"""Server-side diff of a spec-change proposal (web UI review, ADR 0066).

The diff is computed by running the proposal's patch through the same dry run as the CLI/MCP path
(:func:`fabrikant.specs.save_spec` with ``dry_run=True``) and comparing the validated result with the
spec currently on disk. Loop definitions are shown as YAML with secret-looking values masked
(key-based, with a fingerprint so a changed secret is visible, plus pattern scrubbing of strings);
the unified line diff is built from the masked text, so a secret never leaves the server.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import os
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from ruamel.yaml import YAML

from .secrets import MASKED_VALUE, is_secret_key, redact_text
from .spec_proposals import ProposalStatus, SpecProposal
from .specs import SpecUpdate, load_projects, save_spec, spec_hash, spec_path

TOP_LEVEL_FIELDS = ("description", "max_parallel")
DIFF_CONTEXT_LINES = 3


class ChangeKind(StrEnum):
    """How a loop differs between the current and the proposed spec."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass
class LoopChange:
    """One loop that differs; ``before``/``after`` are masked YAML ("" when absent)."""

    name: str
    kind: ChangeKind
    before: str = ""
    after: str = ""
    diff: list[str] = field(default_factory=list)  # unified diff lines (``+``/``-``/`` ``/``@@``)


@dataclass
class FieldChange:
    """A changed top-level field (``description``, ``max_parallel``)."""

    name: str
    before: str
    after: str


@dataclass
class ProposalDiff:
    """A proposal's metadata plus what applying it would change. ``stale``: the spec moved on."""

    id: str
    project: str
    proposed_by: str
    created: str
    status: str
    base_hash: str
    decided_by: str = ""
    decided: str = ""
    stale: bool = False
    changes: list[str] = field(default_factory=list)  # value-free summary from the proposal
    error: str = ""  # why no diff could be computed (e.g. the patch no longer validates)
    loops: list[LoopChange] = field(default_factory=list)
    fields: list[FieldChange] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-able form, including per-kind counts for the summary line."""
        out = asdict(self)
        out["counts"] = {k.value: sum(1 for c in self.loops if c.kind is k) for k in ChangeKind}
        return out


_FINGERPRINT_SALT = os.urandom(16)  # per process: before/after of one diff compare, nothing else does
FINGERPRINT_HEX_LEN = 6


def _mask_marker(value: object) -> str:
    """The mask with a short salted fingerprint, so a *changed* secret differs on both sides of the
    diff (``***redacted#a1b2c3***`` vs ``***redacted#d4e5f6***``) without revealing it."""
    digest = hashlib.sha256(_FINGERPRINT_SALT + repr(value).encode()).hexdigest()[:FINGERPRINT_HEX_LEN]
    return f"{MASKED_VALUE[:-3]}#{digest}***"


def _mask(value: object) -> object:
    """Mask values of secret-looking keys (fingerprinted) and scrub secret patterns out of every string."""
    if isinstance(value, dict):
        return {k: _mask_marker(v) if is_secret_key(k) else _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _yaml_text(value: object) -> str:
    """Masked, stable YAML rendering of ``value`` ("" for ``None``)."""
    if value is None:
        return ""
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    buf = io.StringIO()
    yml.dump(_mask(value), buf)
    return buf.getvalue()


def line_diff(before: str, after: str) -> list[str]:
    """Unified diff lines without the ``---``/``+++`` file headers."""
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=DIFF_CONTEXT_LINES)
    return [ln for ln in lines if not ln.startswith(("---", "+++"))]


def _loop_changes(current: dict, proposed: dict) -> list[LoopChange]:
    changes = []
    for name in sorted(current.keys() | proposed.keys()):
        old, new = current.get(name), proposed.get(name)
        if old == new:
            continue
        kind = ChangeKind.ADDED if old is None else ChangeKind.REMOVED if new is None else ChangeKind.CHANGED
        before, after = _yaml_text(old), _yaml_text(new)
        changes.append(LoopChange(name, kind, before, after, line_diff(before, after)))
    return changes


def _field_changes(current: dict, proposed: dict) -> list[FieldChange]:
    return [
        FieldChange(key, redact_text(str(current.get(key, ""))), redact_text(str(proposed.get(key, ""))))
        for key in TOP_LEVEL_FIELDS
        if current.get(key) != proposed.get(key)
    ]


def proposal_meta(proposal: SpecProposal) -> ProposalDiff:
    """Metadata only (no patch, no diff: cheap and free of spec contents, for lists)."""
    return ProposalDiff(
        id=proposal.id,
        project=proposal.project,
        proposed_by=proposal.proposed_by,
        created=proposal.created,
        status=str(proposal.status),
        base_hash=proposal.base_hash,
        decided_by=proposal.decided_by,
        decided=proposal.decided,
        changes=list(proposal.changes),
    )


def compute_diff(proposal: SpecProposal) -> ProposalDiff:
    """Diff ``proposal`` against the spec on disk (only for pending proposals; others carry metadata only)."""
    result = proposal_meta(proposal)
    if proposal.status is not ProposalStatus.PENDING:
        return result
    try:
        current = load_projects().get(proposal.project)
        if not current or current.get("_error"):
            raise KeyError(proposal.project)
        result.stale = spec_hash(spec_path(proposal.project)) != proposal.base_hash
        # No base_hash here: a stale proposal still shows what its patch would do to today's spec.
        proposed = save_spec(proposal.project, SpecUpdate.from_payload(proposal.patch), dry_run=True)
    except KeyError:
        result.error = f"unknown or broken project: {proposal.project!r}"
        return result
    except ValueError as e:
        result.error = f"invalid change: {e}"
        return result
    except OSError as e:
        # The spec file vanished (or turned unreadable) between the checks above and the hash/read.
        result.error = f"spec file unavailable: {e}"
        return result
    result.loops = _loop_changes(current.get("loops") or {}, proposed.get("loops") or {})
    result.fields = _field_changes(current, proposed)
    return result
