"""Freigabe-Anfrage (Approval-Request) eines pausierten Runs.

Typisiert den bisher losen `record['approval']`-Dict (Vorstufe zum Run-Modell
aus ADR 0034). Das JSON-Format im Run-Record bleibt unverändert: `to_dict()`
lässt leere Felder weg, `from_dict()` liest auch ältere bzw. unvollständige
Records (unbekannte Keys werden ignoriert).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace

# Anzeige-Labels in fester Reihenfolge — gemeinsam für Run-Log, Chat und CLI
# (die UI in web/app.js formatApproval hält dieselbe Reihenfolge).
_TEXT_LINES = (("kind", "art"), ("condition", "bedingung"), ("why", "warum"), ("what", "was"))

_STR_FIELDS = ("reason", "kind", "step", "condition", "why", "what", "issue", "pr", "pr_title")


@dataclass(frozen=True)
class ApprovalRequest:
    """Was freigegeben werden soll und warum.

    reason:      Auslöser — "step" (approval/approval_when), "gate" (Step meldet
                 zur Laufzeit needs_approval), "tier" (Tier-Gate am Loop-Ende),
                 "criticality" (human_gate), "escalation" (an einen Menschen eskaliert, ADR 0059).
    kind:        Eskalationsart bei reason "escalation" (config.ESCALATION_KINDS), sonst leer.
    step:        Name des wartenden Steps ("" beim Tier-Gate).
    condition:   erfüllte `approval_when`-Bedingung, z. B. "label = needs approval".
    why:         fachliche Begründung (Step-Feld `approval_reason`, z. B. LLM-reasoning).
    what:        was der Step tun wird (deterministisch aus der Spec abgeleitet).
    permissions: Agent-Rechte des Steps, lesbar formatiert.
    issue:       Link zum auslösenden GitHub-Issue.
    pr:          Link zur PR des Runs (on_pr-Trigger bzw. `pr_url` aus dem pr-Step).
    pr_title:    Titel dieser PR — sagt dem Approver, WORUM es geht (z. B. welche ADR).
    tier:        Tier des Loops beim Tier-Gate.
    """

    reason: str
    kind: str = ""
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
        """Aus `record['approval']`; kein Dict (alte/defekte Records) → leere Anfrage.

        Felder mit falschem Typ werden verworfen (→ Default); `permissions` akzeptiert
        nur Listen, deren String-Elemente übernommen werden.
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
        """JSON-Form für den Run-Record; leere Felder entfallen (wie bisher)."""
        return {k: v for k, v in asdict(self).items() if v not in ("", [], None)}

    def merged(self, data: Mapping[str, object] | None) -> ApprovalRequest:
        """Kopie mit überschriebenen Feldern (z. B. Laufzeit-Kontext von human_gate)."""
        override = ApprovalRequest.from_dict(data).to_dict()
        if not override.get("reason"):
            override.pop("reason", None)
        return replace(self, **override)  # type: ignore[arg-type]

    def lines(self) -> list[str]:
        """Kontext als "key: wert"-Zeilen für Run-Log, Chat und CLI."""
        out = [f"{label}: {getattr(self, key)}" for key, label in _TEXT_LINES if getattr(self, key)]
        out += [f"rechte: {p}" for p in self.permissions]
        if self.issue:
            out.append(f"issue: {self.issue}")
        if self.pr:
            out.append(f"pr: {self.pr_title} — {self.pr}" if self.pr_title else f"pr: {self.pr}")
        if self.tier is not None:
            out.append(f"tier: {self.tier}")
        return out
