"""Step: pr_merge — PR mergen (Developer-Token, Default squash).

Dual-Identity-Pipeline (ADR 0010): Die App (Developer) mergt nach der
Reviewer-Approval. Branch-Protection ("require 1 review") ist damit die
harte Merge-Gate, die GitHub selbst erzwingt.

Step-Def:
    type: pr_merge
    repo: owner/name
    pr: {pr_number}        # optional; Default: ctx-Variable pr_number (pr-Step)
    method: squash         # squash | merge | rebase (Default: squash)
    update_branch: false   # true: PR-Branch vor dem Merge auf die Base nachziehen (Default false)

`update_branch`: Fällt der PR-Branch hinter die Base zurück (z. B. während ein human_gate wartet),
ruft der Step `PUT /pulls/{n}/update-branch` (mit `expected_head_sha`) auf, wartet begrenzt, bis
GitHub den neuen Head und die Mergebarkeit meldet, und mergt dann. Ein echter Konflikt (HTTP 422
bzw. `mergeable: false`) macht den Step rot (`outcome`) mit Nachricht samt PR — ein Mensch löst
ihn auf, es gibt kein automatisches Routing. Trade-off: der dabei entstehende Update-Merge-Commit
wird **nicht** erneut gelintet/getestet; die Branch-Protection der Base bleibt das Gate.
"""

from __future__ import annotations

import contextlib
import time
import urllib.error

from ..context import _substitute
from ..failures import ConfigError, Failure, classify_http, failed
from ..gitops import _gh_api, _gh_token
from ..runmodel import FailureKind

_MERGE_METHODS = ("squash", "merge", "rebase")
_MERGE_REFUSED_STATUSES = frozenset({405, 409})  # GitHub: nicht mergebar bzw. Konflikt/Head geändert
_UPDATE_CONFLICT_STATUS = 422  # update-branch: Merge-Konflikt bzw. expected_head_sha veraltet
_UPDATE_WAIT_TIMEOUT_S = 120  # GitHub berechnet Head/Mergebarkeit nach update-branch asynchron
_UPDATE_POLL_INTERVAL_S = 3
_API = "https://api.github.com/repos"


class _UpdateBranchError(Exception):
    """update-branch gescheitert; ``failure`` ist das fertige Step-Result."""

    def __init__(self, failure: dict):
        super().__init__(failure.get("summary", ""))
        self.failure = failure


def _conflict(repo: str, pr_number: int, why: str) -> dict:
    return failed(
        f"pr_merge: PR #{pr_number} ({repo}) hat einen Merge-Konflikt mit der Base — {why}; "
        "manuell auflösen (Branch aktualisieren/rebasen)",
        FailureKind.OUTCOME,
    )


def _wait_for_updated_head(repo: str, pr_number: int, old_sha: str, token: str) -> str:
    """Pollt den PR, bis GitHub einen neuen Head mit berechneter Mergebarkeit meldet; liefert den neuen Head.

    Konflikt (``mergeable: false``) → :class:`_UpdateBranchError`; Timeout → transient."""
    deadline = time.monotonic() + _UPDATE_WAIT_TIMEOUT_S
    while True:
        pr = _gh_api("GET", f"{_API}/{repo}/pulls/{pr_number}", token) or {}
        head = (pr.get("head") or {}).get("sha") or ""
        if head and head != old_sha and pr.get("mergeable") is not None:
            if pr["mergeable"] is False:
                raise _UpdateBranchError(_conflict(repo, pr_number, "nach update-branch nicht mergebar"))
            return head
        if time.monotonic() >= deadline:
            raise _UpdateBranchError(
                failed(
                    f"pr_merge: PR #{pr_number} meldet nach update-branch nicht innerhalb "
                    f"{_UPDATE_WAIT_TIMEOUT_S}s einen neuen, mergebaren Head",
                    FailureKind.TRANSIENT,
                )
            )
        time.sleep(_UPDATE_POLL_INTERVAL_S)


def _update_branch_if_behind(repo: str, pr_number: int, token: str) -> str:
    """PR-Branch auf die Base nachziehen, falls er dahinter liegt. Liefert den neuen Head ("" = unverändert).

    Konflikt/Timeout → :class:`_UpdateBranchError`."""
    pr = _gh_api("GET", f"{_API}/{repo}/pulls/{pr_number}", token) or {}
    head = (pr.get("head") or {}).get("sha") or ""
    if pr.get("mergeable") is False or pr.get("mergeable_state") == "dirty":
        raise _UpdateBranchError(_conflict(repo, pr_number, "Konflikt vor update-branch"))
    base_ref = (pr.get("base") or {}).get("ref") or ""
    if not (head and base_ref):
        return ""
    cmp = _gh_api("GET", f"{_API}/{repo}/compare/{base_ref}...{head}", token) or {}
    if not cmp.get("behind_by"):
        return ""
    try:
        _gh_api("PUT", f"{_API}/{repo}/pulls/{pr_number}/update-branch", token, {"expected_head_sha": head})
    except urllib.error.HTTPError as e:
        if e.code == _UPDATE_CONFLICT_STATUS:
            raise _UpdateBranchError(_conflict(repo, pr_number, "update-branch abgelehnt (HTTP 422)")) from e
        raise
    return _wait_for_updated_head(repo, pr_number, head, token)


def _step_pr_merge(ctx: dict, step: dict) -> dict:
    repo = str(step.get("repo") or "").strip()
    if not repo:
        raise ConfigError("pr_merge: 'repo: owner/name' fehlt")
    method = str(step.get("method") or "squash").strip().lower()
    if method not in _MERGE_METHODS:
        return failed(
            f"pr_merge: method '{method}' ungültig (erlaubt: {', '.join(_MERGE_METHODS)})", FailureKind.ENVIRONMENT
        )
    token = _gh_token("dev")
    raw = _substitute(str(step.get("pr") or ""), ctx).strip() or str(ctx.get("pr_number") or "").strip()
    if not raw.isdigit():
        # Kommt meist aus dem Kontext (z. B. kein PR im Vorlauf erstellt) — kein Umgebungsfehler.
        return failed(f"pr_merge: PR-Number nicht auflösbar (pr: {raw!r})", FailureKind.UNKNOWN)
    pr_number = int(raw)
    payload: dict = {"merge_method": method}
    if step.get("update_branch"):
        try:
            if new_head := _update_branch_if_behind(repo, pr_number, token):
                payload["sha"] = new_head  # nur den frisch aktualisierten Head mergen
        except _UpdateBranchError as e:
            return e.failure
        except urllib.error.HTTPError as e:
            return failed(f"pr_merge: update-branch fehlgeschlagen (HTTP {e.code})", classify_http(e.code, e.headers))
    try:
        res = _gh_api("PUT", f"{_API}/{repo}/pulls/{pr_number}/merge", token, payload)
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):  # Body-Read ist best-effort
            detail = e.read().decode()[:300]
        return failed(
            f"pr_merge: Merge abgelehnt (HTTP {e.code}) — z. B. fehlende Approval, "
            f"fehlende Checks oder Konflikt: {detail}",
            _merge_refusal(e),
        )
    if res.get("merged") is False:
        return failed(f"pr_merge: PR #{pr_number} nicht gemerged: {res.get('message', '?')}", FailureKind.OUTCOME)
    return {"ok": True, "summary": f"PR #{pr_number} gemerged ({method}) {res.get('html_url', '')}"}


def _merge_refusal(e: urllib.error.HTTPError) -> Failure:
    """Merge-API-Fehler → Klasse: 405 (nicht mergebar) / 409 (Konflikt, Head geändert) sind ein echtes
    Ergebnis (``outcome``); sonst die HTTP-Klasse (5xx/429 transient, 401/403 environment)."""
    return Failure(FailureKind.OUTCOME) if e.code in _MERGE_REFUSED_STATUSES else classify_http(e.code, e.headers)
