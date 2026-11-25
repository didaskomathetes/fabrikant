"""Step: escalation — waiting gate run for an item escalated to a human (ADR 0059).

The label (`needs human`, spec field `label`) is set by `scripts/pr-status.py` (`escalate` /
`escalation-check --escalate`) together with a marker comment. This step makes the
escalation visible and actionable in the fabrikant UI:

1. First run: reads the item (PR or issue) and the latest `fabrikant-escalate` marker,
   pauses the run with ``ApprovalRequest(reason="escalation", kind=…, why=…)``.
   If the item no longer carries the label, the step ends without pausing (``done``).
2. Approve = **resume**: remove the label; review-pr/pr-reply/develop pick the item up again.
   The approval also grants the run a fresh round budget
   (``grant_rounds``, default ``DEFAULT_GRANT_ROUNDS``) as the ctx variable
   ``GRANT_ROUNDS_CTX`` — so a resumed run does not immediately exhaust its budget and
   escalate again (ADR 0059; consumed by pr_review and the lapsed
   ``exhausted`` path). **Re-scope** is the same, after the human has adjusted the ACs/PR body
   and said so in a PR comment (regular input for pr-reply).
3. Reject = **give up**: the run ends, the label stays — loops leave the item alone;
   closing the PR is left to the human.

Step-Def:
    type: escalation
    repo: owner/name
    item: "{pr_number}"   # optional; default: ctx pr_number, else issue_number
    label: needs human    # optional; Default config.HUMAN_LABEL
    grant_rounds: 2       # optional; fresh round budget per approval (ADR 0059)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..context import _substitute
from ..failures import ConfigError
from ..hosting import Provider, get_provider

COMMENTS_PER_PAGE = 100
MAX_WHY_CHARS = 1500  # keep the reason in the gate short; the link leads to the full comment
RESUME_FLAG = "escalation_acknowledged"
# ADR 0059: an approval grants the run a fresh round budget so that a resumed run does not
# immediately exhaust it and escalate again. The grant is a ctx variable that the
# round-budgeting steps (pr_review and the lapsed `exhausted` path) read and add to their own budget.
GRANT_ROUNDS_CTX = "escalation_granted_rounds"
DEFAULT_GRANT_ROUNDS = 2  # fresh budget per approval (matches the pr_review default)
# Gleiches Format wie scripts/pr-status.py ESCALATE_MARKER_RE (tests/test_escalation.py checks this).
ESCALATE_MARKER_RE = re.compile(r"<!-- fabrikant-escalate kind=(\w+)(?: re=([\w,-]+))? -->")
_ANY_MARKER_RE = re.compile(r"<!--\s*fabrikant-[^>]*-->")
MANUAL_KIND = "blocked"  # a label without a marker (set by hand) counts as a block
ACTIONS = (
    "Approve = resume: remove the label, loops continue. Beforehand, leave hints as a "
    "PR comment (e.g. “upstream PR merged, please verify again”) or adjust the ACs/PR body "
    "(re-scope). Reject = give up: the label stays, no loop picks the item up."
)


@dataclass(frozen=True)
class Escalation:
    """Latest escalation of an item, read from the marker comment."""

    kind: str
    why: str
    url: str = ""

    @classmethod
    def from_comments(cls, comments: list[dict]) -> Escalation:
        """Latest comment with a marker; unknown kind or no marker → manual block."""
        for comment in reversed(comments):
            body = str(comment.get("body") or "")
            if m := ESCALATE_MARKER_RE.search(body):
                kind = m.group(1) if m.group(1) in config.ESCALATION_KINDS else MANUAL_KIND
                why = _ANY_MARKER_RE.sub("", body).strip()
                if len(why) > MAX_WHY_CHARS:
                    why = why[:MAX_WHY_CHARS].rstrip() + " …"
                return cls(kind, why, str(comment.get("html_url") or ""))
        return cls(MANUAL_KIND, "Label set by hand (no fabrikant-escalate comment found)")


def _item_number(ctx: dict, step: dict) -> int:
    raw = _substitute(str(step.get("item") or ""), ctx).strip()
    raw = raw or str(ctx.get("pr_number") or ctx.get("issue_number") or "").strip()
    if not raw.isdigit():
        raise ConfigError(f"escalation: item number not resolvable (item: {raw!r}, no pr_number/issue_number)")
    return int(raw)


def _comments(provider: Provider, repo: str, number: int) -> list[dict]:
    """All issue/PR comments (chronological, paginated) as raw dicts."""
    out: list[dict] = []
    page = 1
    while True:
        batch = provider.list_comments(repo, number, page=page, per_page=COMMENTS_PER_PAGE)
        if not batch:
            return out
        out.extend(c.raw for c in batch)
        if len(batch) < COMMENTS_PER_PAGE:
            return out
        page += 1


def _remove_label(provider: Provider, repo: str, number: int, label: str) -> bool:
    """Remove the label; already gone (404) → False instead of an error (provider is idempotent)."""
    return provider.remove_label(repo, number, label)


def _step_escalation(ctx: dict, step: dict, emit) -> dict:
    repo = str(step.get("repo") or "").strip()
    if not repo:
        raise ConfigError("escalation: 'repo: owner/name' fehlt")
    label = str(step.get("label") or config.HUMAN_LABEL)
    number = _item_number(ctx, step)
    provider = get_provider(repo)

    if str(ctx.get(RESUME_FLAG, "")) == "true":
        # ADR 0059: approval = fresh round budget (hook for pr_review and the lapsed
        # `exhausted` path), not just a resume with the old budget.
        granted = max(0, int(step.get("grant_rounds", DEFAULT_GRANT_ROUNDS)))
        ctx[GRANT_ROUNDS_CTX] = granted
        removed = _remove_label(provider, repo, number, label)
        state = "removed" if removed else "was already removed"
        return {
            "ok": True,
            "summary": f"escalation: #{number} resumed — label '{label}' {state}, +{granted} rounds",
        }

    item = provider.get_issue(repo, number)
    if label not in item.labels:
        return {
            "ok": True,
            "done": True,
            "summary": f"escalation: #{number} does not (or no longer) carry '{label}' — nothing to do",
        }

    esc = Escalation.from_comments(_comments(provider, repo, number))
    ctx[RESUME_FLAG] = "true"  # set before the pause: persisted with record["vars"]
    link = item.url
    approval = {"reason": "escalation", "kind": esc.kind, "why": esc.why, "what": ACTIONS}
    approval["pr" if item.is_pr else "issue"] = link
    if item.is_pr:
        approval["pr_title"] = item.title
    emit(f"  ⚠ eskaliert ({esc.kind}): {esc.url or link}")
    return {
        "ok": True,
        "needs_approval": True,
        "approval": approval,
        "summary": f"escalation: #{number} ({esc.kind}) wartet auf einen Menschen",
    }
