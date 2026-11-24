"""Step: pr_merge (ADR 0010) — PR mergen via Developer-Token."""

from __future__ import annotations

import pytest
from helpers import http_error

from fabrikant import gitops
from fabrikant.steps import pr_merge


def _patch(monkeypatch, response=None, exc=None):
    seen = {}

    def _api(method, url, token, payload=None, accept=None):
        seen.update(method=method, url=url, token=token, payload=payload)
        if exc is not None:
            raise exc
        return response or {}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": f"tok-{role}")
    monkeypatch.setattr(gitops, "_gh_api", _api)
    return seen


def test_merge_ok_squash(monkeypatch):
    seen = _patch(monkeypatch, {"merged": True, "html_url": "https://gh/commit/x"})
    res = pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})
    assert res["ok"] is True
    assert "gemerged" in res["summary"]
    assert seen["method"] == "PUT"
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls/7/merge"
    assert seen["token"] == "tok-dev"  # Developer-Identity (App)
    assert seen["payload"] == {"merge_method": "squash"}


def test_merge_method_rebase(monkeypatch):
    seen = _patch(monkeypatch, {"merged": True})
    res = pr_merge._step_pr_merge({"pr_number": "3"}, {"repo": "o/r", "method": "rebase"})
    assert res["ok"] is True
    assert seen["payload"] == {"merge_method": "rebase"}


def test_merge_invalid_method(monkeypatch):
    _patch(monkeypatch)
    res = pr_merge._step_pr_merge({"pr_number": "3"}, {"repo": "o/r", "method": "fast-forward"})
    assert res["ok"] is False
    assert "ungültig" in res["summary"]


def test_merge_http_error(monkeypatch):
    err = http_error(
        405,
        "https://api.github.com/repos/o/r/pulls/7/merge",
        "Method Not Allowed",
        body=b'{"message": "Not mergeable"}',
    )
    _patch(monkeypatch, exc=err)
    res = pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})
    assert res["ok"] is False
    assert "HTTP 405" in res["summary"]
    assert "Not mergeable" in res["summary"]


def test_merge_merged_false(monkeypatch):
    _patch(monkeypatch, {"merged": False, "message": "merge commit already exists"})
    res = pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})
    assert res["ok"] is False
    assert "nicht gemerged" in res["summary"]


def test_merge_no_pr_number(monkeypatch):
    _patch(monkeypatch)
    res = pr_merge._step_pr_merge({}, {"repo": "o/r"})
    assert res["ok"] is False
    assert "nicht auflösbar" in res["summary"]


def test_merge_pr_from_step_def(monkeypatch):
    seen = _patch(monkeypatch, {"merged": True})
    res = pr_merge._step_pr_merge({"pr_number": "1"}, {"repo": "o/r", "pr": "4"})
    assert res["ok"] is True
    assert "/pulls/4/merge" in seen["url"]


def test_merge_missing_repo_raises():
    with pytest.raises(ValueError, match="repo"):
        pr_merge._step_pr_merge({"pr_number": "1"}, {})


# ---------------------------------------------------------------- Fehlerklasse (ADR 0035)
@pytest.mark.parametrize(
    ("code", "kind"), [(405, "outcome"), (409, "outcome"), (502, "transient"), (403, "environment")]
)
def test_merge_http_error_kind(monkeypatch, code, kind):
    err = http_error(code, "https://api.github.com/x", "x", body=b"{}")
    _patch(monkeypatch, exc=err)
    assert pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})["failure_kind"] == kind


def test_merge_config_and_refusal_kinds(monkeypatch):
    _patch(monkeypatch, {"merged": False, "message": "nope"})
    assert pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})["failure_kind"] == "outcome"
    # PR-Number fehlt im Kontext (z. B. kein PR im Vorlauf) — kein Umgebungsfehler
    assert pr_merge._step_pr_merge({}, {"repo": "o/r"})["failure_kind"] == "unknown"
    assert pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r", "method": "x"})["failure_kind"] == "environment"


# ---------------------------------------------------------------- update_branch
OLD, NEW = "old111", "new222"


def _pr(head, mergeable: bool | None = True, state="clean"):  # GitHub: mergeable=null = noch nicht berechnet
    return {"head": {"sha": head}, "base": {"ref": "main"}, "mergeable": mergeable, "mergeable_state": state}


def _patch_update(monkeypatch, *, pr_seq, behind_by=2, update_exc=None):
    """Fake-GitHub: GET pull liefert ``pr_seq`` nacheinander (letzter bleibt), compare ``behind_by``."""
    calls = []
    seq = list(pr_seq)

    def _api(method, url, token, payload=None, accept=None):
        calls.append((method, url.split("/repos/o/r")[1], payload))
        if url.endswith("/update-branch"):
            if update_exc:
                raise update_exc
            return {"message": "Updating pull request branch."}
        if "/compare/" in url:
            return {"behind_by": behind_by}
        if method == "GET":
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return {"merged": True}

    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(gitops, "_gh_api", _api)
    monkeypatch.setattr(pr_merge, "_UPDATE_POLL_INTERVAL_S", 0)
    return calls


STEP = {"repo": "o/r", "update_branch": True}


def test_update_branch_behind_updates_then_merges(monkeypatch):
    calls = _patch_update(monkeypatch, pr_seq=[_pr(OLD), _pr(OLD, None, "unknown"), _pr(NEW)])
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is True
    assert ("PUT", "/pulls/7/update-branch", {"expected_head_sha": OLD}) in calls
    assert calls[-1] == ("PUT", "/pulls/7/merge", {"merge_method": "squash", "sha": NEW})


def test_update_branch_not_behind_skips_update(monkeypatch):
    calls = _patch_update(monkeypatch, pr_seq=[_pr(OLD)], behind_by=0)
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is True
    assert not any(u.endswith("/update-branch") for _, u, _ in calls)
    assert calls[-1][2] == {"merge_method": "squash"}


def test_update_branch_disabled_by_default(monkeypatch):
    calls = _patch_update(monkeypatch, pr_seq=[_pr(OLD)])
    pr_merge._step_pr_merge({"pr_number": "7"}, {"repo": "o/r"})
    assert [c[0] for c in calls] == ["PUT"]  # nur der Merge


def test_update_branch_422_is_conflict(monkeypatch):
    err = http_error(422, "u", "Unprocessable", body=b"{}")
    calls = _patch_update(monkeypatch, pr_seq=[_pr(OLD)], update_exc=err)
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is False and res["failure_kind"] == "outcome"
    assert "PR #7" in res["summary"] and "Konflikt" in res["summary"]
    assert not any(u.endswith("/merge") for _, u, _ in calls)


def test_update_branch_conflict_before_update(monkeypatch):
    calls = _patch_update(monkeypatch, pr_seq=[_pr(OLD, False, "dirty")])
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is False and res["failure_kind"] == "outcome"
    assert len(calls) == 1  # kein compare/update/merge


def test_update_branch_unmergeable_after_update(monkeypatch):
    _patch_update(monkeypatch, pr_seq=[_pr(OLD), _pr(NEW, False, "dirty")])
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is False and res["failure_kind"] == "outcome"
    assert "nach update-branch" in res["summary"]


def test_update_branch_wait_timeout_is_transient(monkeypatch):
    _patch_update(monkeypatch, pr_seq=[_pr(OLD)])  # Head ändert sich nie
    monkeypatch.setattr(pr_merge, "_UPDATE_WAIT_TIMEOUT_S", 0)
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is False and res["failure_kind"] == "transient"


def test_update_branch_other_http_error_classified(monkeypatch):
    err = http_error(403, "u", "Forbidden", body=b"{}")
    _patch_update(monkeypatch, pr_seq=[_pr(OLD)], update_exc=err)
    res = pr_merge._step_pr_merge({"pr_number": "7"}, STEP)
    assert res["ok"] is False and "HTTP 403" in res["summary"]
