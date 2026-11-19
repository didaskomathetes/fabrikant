"""Step: pr_merge — merge a PR (developer token, default squash).

Dual-identity pipeline (ADR 0010): the app (developer) merges after the
reviewer approval. Branch protection ("require 1 review") is thus the
hard merge gate, which GitHub itself enforces.

Step def:
    type: pr_merge
    repo: owner/name
    pr: {pr_number}        # optional; default: ctx variable pr_number (pr step)
    method: squash         # squash | merge | rebase (default: squash)
    update_branch: false   # true: bring the PR branch up to the base before merging (default false)

`update_branch`: if the PR branch falls behind the base (e.g. while a human_gate waits),
the step calls `PUT /pulls/{n}/update-branch` (with `expected_head_sha`), waits a bounded time until
GitHub reports the new head and mergeability, and then merges. A real conflict (HTTP 422
or `mergeable: false`) makes the step red (`outcome`) with a message including the PR — a human resolves
it, there is no automatic routing. Trade-off: the resulting update-merge commit
is **not** linted/tested again; the base's branch protection remains the gate.
"""

from __future__ import annotations

import contextlib
import time
import urllib.error

from ..context import _substitute
from ..failures import ConfigError, Failure, classify_http, failed
from ..gitops import _gh_api, _gh_token
from ..i18n import t
from ..runmodel import FailureKind

_MERGE_METHODS = ("squash", "merge", "rebase")
_MERGE_REFUSED_STATUSES = frozenset({405, 409})  # GitHub: not mergeable or conflict/head changed
_UPDATE_CONFLICT_STATUS = 422  # update-branch: Merge-Konflikt bzw. expected_head_sha veraltet
_UPDATE_WAIT_TIMEOUT_S = 120  # GitHub berechnet Head/Mergebarkeit nach update-branch asynchron
_UPDATE_POLL_INTERVAL_S = 3
_API = "https://api.github.com/repos"


class _UpdateBranchError(Exception):
    """update-branch failed; ``failure`` is the finished step result."""

    def __init__(self, failure: dict):
        super().__init__(failure.get("summary", ""))
        self.failure = failure


def _conflict(repo: str, pr_number: int, why: str) -> dict:
    return failed(
        t("steps.pr_merge.pr_merge_pr_has_a", pr_number=pr_number, repo=repo, why=why),
        FailureKind.OUTCOME,
    )


def _wait_for_updated_head(repo: str, pr_number: int, old_sha: str, token: str) -> str:
    """Polls the PR until GitHub reports a new head with computed mergeability; returns the new head.

    Conflict (``mergeable: false``) → :class:`_UpdateBranchError`; timeout → transient."""
    deadline = time.monotonic() + _UPDATE_WAIT_TIMEOUT_S
    while True:
        pr = _gh_api("GET", f"{_API}/{repo}/pulls/{pr_number}", token) or {}
        head = (pr.get("head") or {}).get("sha") or ""
        if head and head != old_sha and pr.get("mergeable") is not None:
            if pr["mergeable"] is False:
                raise _UpdateBranchError(
                    _conflict(repo, pr_number, t("steps.pr_merge.not_mergeable_after_update_branch"))
                )
            return head
        if time.monotonic() >= deadline:
            raise _UpdateBranchError(
                failed(
                    t("steps.pr_merge.pr_merge_after_update_branch", pr_number=pr_number, v2=_UPDATE_WAIT_TIMEOUT_S),
                    FailureKind.TRANSIENT,
                )
            )
        time.sleep(_UPDATE_POLL_INTERVAL_S)


def _update_branch_if_behind(repo: str, pr_number: int, token: str) -> str:
    """Bring the PR branch up to the base if it is behind. Returns the new head ("" = unchanged).

    Conflict/timeout → :class:`_UpdateBranchError`."""
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
        raise ConfigError(t("steps.pr_merge.pr_merge_repo_owner_name"))
    method = str(step.get("method") or "squash").strip().lower()
    if method not in _MERGE_METHODS:
        return failed(
            t("steps.pr_merge.pr_merge_method_invalid_allowed", method=method, v2=", ".join(_MERGE_METHODS)),
            FailureKind.ENVIRONMENT,
        )
    token = _gh_token("dev")
    raw = _substitute(str(step.get("pr") or ""), ctx).strip() or str(ctx.get("pr_number") or "").strip()
    if not raw.isdigit():
        # Mostly comes from the context (e.g. no PR created beforehand) — no environment error.
        return failed(t("steps.pr_merge.pr_merge_pr_number_cannot", raw=raw), FailureKind.UNKNOWN)
    pr_number = int(raw)
    payload: dict = {"merge_method": method}
    if step.get("update_branch"):
        try:
            if new_head := _update_branch_if_behind(repo, pr_number, token):
                payload["sha"] = new_head  # merge only the freshly updated head
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
            t("steps.pr_merge.pr_merge_merge_rejected_http", v1=e.code, detail=detail),
            _merge_refusal(e),
        )
    if res.get("merged") is False:
        return failed(
            t("steps.pr_merge.pr_merge_pr_not_merged", pr_number=pr_number, v2=res.get("message", "?")),
            FailureKind.OUTCOME,
        )
    return {"ok": True, "summary": f"PR #{pr_number} gemerged ({method}) {res.get('html_url', '')}"}


def _merge_refusal(e: urllib.error.HTTPError) -> Failure:
    """Merge API error → class: 405 (not mergeable) / 409 (conflict, head changed) are a real
    result (``outcome``); otherwise the HTTP class (5xx/429 transient, 401/403 environment)."""
    return Failure(FailureKind.OUTCOME) if e.code in _MERGE_REFUSED_STATUSES else classify_http(e.code, e.headers)
