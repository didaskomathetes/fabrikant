"""Step: escalation — wartender Gate-Run für ein an einen Menschen eskaliertes Item (ADR 0059).

Das Label (`needs human`, Spec-Feld `label`) setzt `scripts/pr-status.py` (`escalate` /
`escalation-check --escalate`) mit Marker-Kommentar. Dieser Step macht
die Eskalation in der fabrik-UI sichtbar und handlungsfähig:

1. Erster Lauf: liest das Item (PR oder Issue) und den letzten `fabrik-escalate`-Marker,
   pausiert den Run mit ``ApprovalRequest(reason="escalation", kind=…, why=…)``.
   Trägt das Item das Label nicht (mehr), endet der Step ohne Pause (``done``).
2. Freigabe = **Fortsetzen**: Label entfernen; review-pr/pr-reply/develop greifen wieder.
   Die Freigabe gewährt dem Run zusätzlich ein frisches Runden-Budget
   (``grant_rounds``, Default ``DEFAULT_GRANT_ROUNDS``) als ctx-Variable
   ``GRANT_ROUNDS_CTX`` — so erschöpft ein fortgesetzter Run nicht sofort wieder und
   eskaliert nicht erneut (ADR 0059; konsumiert von pr_review und dem nachgelassenen
   ``exhausted``-Pfad). **Neu zuschneiden** ist dasselbe, nachdem der Mensch ACs/PR-Body
   angepasst und das in einem PR-Kommentar gesagt hat (regulärer Input für pr-reply).
3. Ablehnen = **Aufgeben**: der Run endet, das Label bleibt — Loops lassen das Item liegen;
   PR schließen bleibt dem Menschen überlassen.

Step-Def:
    type: escalation
    repo: owner/name
    item: "{pr_number}"   # optional; Default: ctx pr_number, sonst issue_number
    label: needs human    # optional; Default config.HUMAN_LABEL
    grant_rounds: 2       # optional; frisches Runden-Budget pro Freigabe (ADR 0059)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from ..context import _substitute
from ..failures import ConfigError
from ..hosting import Provider, get_provider

COMMENTS_PER_PAGE = 100
MAX_WHY_CHARS = 1500  # Begründung im Gate kurz halten; der Link führt zum vollen Kommentar
RESUME_FLAG = "escalation_acknowledged"
# ADR 0059: eine Freigabe gewährt dem Run ein frisches Runden-Budget, damit ein
# fortgesetzter Run nicht sofort wieder erschöpft und neu eskaliert. Der Grant ist
# eine ctx-Variable, die die Runden-budgetierenden Steps (pr_review und der
# nachgelassene `exhausted`-Pfad) lesen und zu ihrem eigenen Budget addieren.
GRANT_ROUNDS_CTX = "escalation_granted_rounds"
DEFAULT_GRANT_ROUNDS = 2  # frisches Budget pro Freigabe (entspricht pr_review-Default)
# Gleiches Format wie scripts/pr-status.py ESCALATE_MARKER_RE (tests/test_escalation.py prüft das).
ESCALATE_MARKER_RE = re.compile(r"<!-- fabrik-escalate kind=(\w+)(?: re=([\w,-]+))? -->")
_ANY_MARKER_RE = re.compile(r"<!--\s*fabrik-[^>]*-->")
MANUAL_KIND = "blocked"  # Label ohne Marker (von Hand gesetzt) gilt als Blockade
ACTIONS = (
    "Freigeben = Fortsetzen: Label entfernen, Loops laufen weiter. Vorher Hinweise als "
    "PR-Kommentar schreiben (z. B. „MAS-PR gemergt, bitte erneut verifizieren“) bzw. ACs/PR-Body "
    "anpassen (Neu zuschneiden). Ablehnen = Aufgeben: Label bleibt, kein Loop greift das Item auf."
)


@dataclass(frozen=True)
class Escalation:
    """Letzte Eskalation eines Items, aus dem Marker-Kommentar gelesen."""

    kind: str
    why: str
    url: str = ""

    @classmethod
    def from_comments(cls, comments: list[dict]) -> Escalation:
        """Letzter Kommentar mit Marker; unbekannte Art oder kein Marker → manuelle Blockade."""
        for comment in reversed(comments):
            body = str(comment.get("body") or "")
            if m := ESCALATE_MARKER_RE.search(body):
                kind = m.group(1) if m.group(1) in config.ESCALATION_KINDS else MANUAL_KIND
                why = _ANY_MARKER_RE.sub("", body).strip()
                if len(why) > MAX_WHY_CHARS:
                    why = why[:MAX_WHY_CHARS].rstrip() + " …"
                return cls(kind, why, str(comment.get("html_url") or ""))
        return cls(MANUAL_KIND, "Label von Hand gesetzt (kein fabrik-escalate-Kommentar gefunden)")


def _item_number(ctx: dict, step: dict) -> int:
    raw = _substitute(str(step.get("item") or ""), ctx).strip()
    raw = raw or str(ctx.get("pr_number") or ctx.get("issue_number") or "").strip()
    if not raw.isdigit():
        raise ConfigError(f"escalation: Item-Nummer nicht auflösbar (item: {raw!r}, kein pr_number/issue_number)")
    return int(raw)


def _comments(provider: Provider, repo: str, number: int) -> list[dict]:
    """Alle Issue-/PR-Kommentare (chronologisch, paginiert) als Raw-Dicts."""
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
    """Label entfernen; schon weg (404) → False statt Fehler (Provider ist idempotent)."""
    return provider.remove_label(repo, number, label)


def _step_escalation(ctx: dict, step: dict, emit) -> dict:
    repo = str(step.get("repo") or "").strip()
    if not repo:
        raise ConfigError("escalation: 'repo: owner/name' fehlt")
    label = str(step.get("label") or config.HUMAN_LABEL)
    number = _item_number(ctx, step)
    provider = get_provider(repo)

    if str(ctx.get(RESUME_FLAG, "")) == "true":
        # ADR 0059: Freigabe = frisches Runden-Budget (Hook für pr_review und den
        # nachgelassenen `exhausted`-Pfad), nicht nur Resume mit dem alten Budget.
        granted = max(0, int(step.get("grant_rounds", DEFAULT_GRANT_ROUNDS)))
        ctx[GRANT_ROUNDS_CTX] = granted
        removed = _remove_label(provider, repo, number, label)
        state = "entfernt" if removed else "war schon entfernt"
        return {
            "ok": True,
            "summary": f"escalation: #{number} fortgesetzt — Label '{label}' {state}, +{granted} Runden",
        }

    item = provider.get_issue(repo, number)
    if label not in item.labels:
        return {
            "ok": True,
            "done": True,
            "summary": f"escalation: #{number} trägt '{label}' nicht (mehr) — nichts zu tun",
        }

    esc = Escalation.from_comments(_comments(provider, repo, number))
    ctx[RESUME_FLAG] = "true"  # vor der Pause setzen: wird mit record["vars"] persistiert
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
