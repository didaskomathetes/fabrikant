"""Approval request of a paused run.

Types the formerly loose `record['approval']` dict (precursor of the run model
from ADR 0034). The JSON format in the run record is unchanged: `to_dict()`
omits empty fields, `from_dict()` also reads older or incomplete
records (unknown keys are ignored).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace

from .i18n import t

# Fields in fixed order — shared by run log, chat and CLI
# (the UI in web/app.js formatApproval keeps the same order). Labels: i18n `approval.<field>`.
_TEXT_FIELDS = ("condition", "why", "what")

_STR_FIELDS = ("reason", "step", "condition", "why", "what", "issue", "pr", "pr_title")


@dataclass(frozen=True)
class ApprovalRequest:
    """What is to be approved and why.

    reason:      trigger — "step" (approval/approval_when), "gate" (step reports
                 needs_approval at runtime), "tier" (tier gate at loop end),
                 "criticality" (human_gate).
    step:        name of the waiting step ("" for the tier gate).
    condition:   satisfied `approval_when` condition, e.g. "label = needs approval".
    why:         domain rationale (step field `approval_reason`, e.g. LLM reasoning).
    what:        what the step will do (derived deterministically from the spec).
    permissions: agent permissions of the step, formatted readably.
    issue:       link to the triggering GitHub issue.
    pr:          link to the run's PR (on_pr trigger or `pr_url` from the pr step).
    pr_title:    title of this PR — tells the approver WHAT it is about (e.g. which ADR).
    tier:        tier of the loop at the tier gate.
    """

    reason: str
    step: str = ""
    condition: str = ""
    why: str = ""
    what: str = ""
    permissions: list[str] = field(default_factory=list)
    issue: str = ""
    pr: str = ""
    pr_title: str = ""
    tier: int | None = None

    @classmethod
    def from_dict(cls, data: object) -> ApprovalRequest:
        """From `record['approval']`; not a dict (old/broken records) → empty request.

        Fields with the wrong type are discarded (→ default); `permissions` accepts
        only lists, of which the string elements are kept.
        """
        if not isinstance(data, Mapping):
            data = {}
        strs = {name: v if isinstance(v := data.get(name), str) else "" for name in _STR_FIELDS}
        perms = data.get("permissions")
        permissions = [p for p in perms if isinstance(p, str)] if isinstance(perms, list) else []
        tier = data.get("tier")
        return cls(
            permissions=permissions,
            tier=tier if isinstance(tier, int) and not isinstance(tier, bool) else None,
            **strs,
        )

    def to_dict(self) -> dict[str, str | int | list[str]]:
        """JSON form for the run record; empty fields are omitted (as before)."""
        return {k: v for k, v in asdict(self).items() if v not in ("", [], None)}

    def merged(self, data: Mapping[str, object] | None) -> ApprovalRequest:
        """Copy with overridden fields (e.g. runtime context from human_gate)."""
        override = ApprovalRequest.from_dict(data).to_dict()
        if not override.get("reason"):
            override.pop("reason", None)
        return replace(self, **override)  # type: ignore[arg-type]

    def lines(self) -> list[str]:
        """Context as "key: value" lines for run log, chat and CLI."""
        out = [f"{t(f'approval.{key}')}: {getattr(self, key)}" for key in _TEXT_FIELDS if getattr(self, key)]
        out += [f"{t('approval.permissions')}: {p}" for p in self.permissions]
        if self.issue:
            out.append(f"issue: {self.issue}")
        if self.pr:
            out.append(f"pr: {self.pr_title} — {self.pr}" if self.pr_title else f"pr: {self.pr}")
        if self.tier is not None:
            out.append(f"tier: {self.tier}")
        return out
