# ruff: noqa: F811  (fixtures imported from test_server_api / test_auth)
"""Spec-proposal diff model and the /api/spec-proposals endpoints (web review, ADR 0066)."""

from __future__ import annotations

import json

import pytest
from test_auth import _login, local_url  # noqa: F401
from test_server_api import _req, fabrikant_home, fabrikant_url  # noqa: F401

from fabrikant import auth, proposal_diff, specs
from fabrikant import spec_proposals as sp
from fabrikant.secrets import MASKED_VALUE

PROPOSALS = "/api/spec-proposals"
NEW_LOOP = {"steps": [{"type": "shell", "cmd": "echo hi", "env": {"API_TOKEN": "s3cret-value"}}]}


@pytest.fixture
def spec_file(fabrikant_home):
    return fabrikant_home / "projects" / "uitest.fabrikant.yaml"


def _propose(spec_file, patch, actor="mcp"):
    return sp.propose("uitest", patch, specs.spec_hash(spec_file), actor)


# ------------------------------------------------------------------ diff model
def test_line_diff_drops_file_headers():
    assert proposal_diff.line_diff("a\nb\n", "a\nc\n") == ["@@ -1,2 +1,2 @@", " a", "-b", "+c"]


def test_diff_added_changed_removed_and_fields(spec_file):
    pr = _propose(
        spec_file,
        {
            "loops": {"t": {"description": "changed"}, "new": NEW_LOOP},
            "description": "other",
            "max_parallel": 2,
        },
    )
    diff = proposal_diff.compute_diff(pr)
    kinds = {c.name: c.kind for c in diff.loops}
    assert kinds == {"new": proposal_diff.ChangeKind.ADDED, "t": proposal_diff.ChangeKind.CHANGED}
    changed = next(c for c in diff.loops if c.name == "t")
    assert "-description: loop-t" in changed.diff and "+description: changed" in changed.diff
    assert "description: loop-t" in changed.before and "description: changed" in changed.after
    assert [(f.name, f.before, f.after) for f in diff.fields] == [
        ("description", "d", "other"),
        ("max_parallel", "", "2"),
    ]
    assert not diff.stale and not diff.error and diff.status == "pending"
    assert diff.to_dict()["counts"] == {"added": 1, "removed": 0, "changed": 1}


def test_diff_removed_loop(spec_file):
    specs.save_spec("uitest", specs.SpecUpdate(loops={"keep": NEW_LOOP}))
    pr = _propose(spec_file, {"remove_loops": ["t"]})
    (removed,) = proposal_diff.compute_diff(pr).loops
    assert removed.kind is proposal_diff.ChangeKind.REMOVED and removed.after == "" and removed.before


def test_diff_masks_secret_keys_everywhere(spec_file):
    diff = proposal_diff.compute_diff(_propose(spec_file, {"loops": {"new": NEW_LOOP}}))
    dumped = json.dumps(diff.to_dict())
    assert "s3cret-value" not in dumped, dumped
    assert MASKED_VALUE[:-3] in dumped


def test_diff_marks_stale_but_still_computes(spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    specs.save_spec("uitest", specs.SpecUpdate(project_fields={"description": "moved on"}))
    diff = proposal_diff.compute_diff(pr)
    assert diff.stale and [c.name for c in diff.loops] == ["new"]


def test_diff_errors_when_patch_no_longer_validates(spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    pr.patch = {"loops": {"new": {"steps": [{"type": "nope"}]}}}
    assert "invalid change" in proposal_diff.compute_diff(pr).error
    pr.project = "ghost"
    assert "unknown" in proposal_diff.compute_diff(pr).error


def test_decided_proposal_has_metadata_only(spec_file):
    pr = sp.reject(_propose(spec_file, {"loops": {"new": NEW_LOOP}}).id, "ui:x")
    diff = proposal_diff.compute_diff(pr)
    assert diff.status == "rejected" and diff.decided_by == "ui:x" and diff.loops == [] and diff.fields == []


# ------------------------------------------------------------------ endpoints
def test_list_and_get_endpoints(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    status, body, _ = _req(fabrikant_url + PROPOSALS)
    listed = json.loads(body)["proposals"]
    assert status == 200 and [p["id"] for p in listed] == [pr.id] and "patch" not in listed[0]
    assert listed[0]["changes"] == ["loop new: set"] and listed[0]["loops"] == []
    assert json.loads(_req(fabrikant_url + PROPOSALS + "?project=other")[1])["proposals"] == []
    assert json.loads(_req(fabrikant_url + PROPOSALS + "?status=applied")[1])["proposals"] == []
    assert _req(fabrikant_url + PROPOSALS + "?status=bogus")[0] == 400
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}")
    assert status == 200 and json.loads(body)["loops"][0]["name"] == "new"
    assert _req(f"{fabrikant_url}{PROPOSALS}/nope")[0] == 404
    assert _req(f"{fabrikant_url}{PROPOSALS}/..%2Fx")[0] in (400, 404)


def test_projects_payload_counts_pending_proposals(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    (project,) = json.loads(_req(fabrikant_url + "/api/projects")[1])
    assert [p["id"] for p in project["pending_proposals"]] == [pr.id]


def test_apply_endpoint_writes_spec_and_audits_actor(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/apply", "POST", {})
    assert status == 200 and json.loads(body)["proposal"]["status"] == "applied"
    assert "new:" in spec_file.read_text() and spec_file.with_name(spec_file.name + ".bak").exists()
    assert sp.get_proposal(pr.id).decided_by.startswith("ui:")
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/apply", "POST", {})
    assert status == 409 and "already applied" in json.loads(body)["error"]
    assert _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/reject", "POST", {})[0] == 409


def test_apply_endpoint_refuses_stale(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    specs.save_spec("uitest", specs.SpecUpdate(project_fields={"description": "moved on"}))
    before = spec_file.read_bytes()
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/apply", "POST", {})
    assert status == 409 and "stale" in json.loads(body)["error"]
    assert spec_file.read_bytes() == before and sp.get_proposal(pr.id).status is sp.ProposalStatus.PENDING


def test_reject_endpoint_and_unknown_action(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    before = spec_file.read_bytes()
    assert _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/explode", "POST", {})[0] == 404
    assert _req(f"{fabrikant_url}{PROPOSALS}/nope/apply", "POST", {})[0] == 404
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/reject", "POST", {})
    assert status == 200 and json.loads(body)["proposal"]["status"] == "rejected"
    assert spec_file.read_bytes() == before


def test_list_filters_by_matching_project_and_status(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    # ?project= matches the proposal's project
    assert [p["id"] for p in json.loads(_req(f"{fabrikant_url}{PROPOSALS}?project=uitest")[1])["proposals"]] == [pr.id]
    # after a decision the record is listed under its status, not under pending
    _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/apply", "POST", {})
    assert json.loads(_req(fabrikant_url + PROPOSALS)[1])["proposals"] == []
    applied = json.loads(_req(f"{fabrikant_url}{PROPOSALS}?status=applied")[1])["proposals"]
    assert [p["id"] for p in applied] == [pr.id] and applied[0]["decided_by"].startswith("ui:")


def test_get_returns_error_field_when_patch_no_longer_validates(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    pr.patch = {"loops": {"new": {"steps": [{"type": "nope"}]}}}
    sp._write(pr)  # persist the broken patch
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}")
    out = json.loads(body)
    assert status == 200 and "invalid change" in out["error"] and out["loops"] == []


def test_get_decided_proposal_carries_decision_metadata(fabrikant_url, spec_file):
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/reject", "POST", {})
    status, body, _ = _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}")
    out = json.loads(body)
    assert status == 200 and out["status"] == "rejected"
    assert out["decided_by"].startswith("ui:") and out["decided"]
    assert out["loops"] == [] and out["fields"] == []  # metadata only, no diff


def test_roles_and_csrf(local_url, spec_file):  # noqa: F811
    pr = _propose(spec_file, {"loops": {"new": NEW_LOOP}})
    url = f"{local_url}{PROPOSALS}/{pr.id}"
    assert _req(local_url + PROPOSALS)[0] == 401
    viewer = _login(local_url, auth.VIEWER)
    assert _req(local_url + PROPOSALS, headers=viewer)[0] == 200 and _req(url, headers=viewer)[0] == 200
    assert _req(url + "/apply", "POST", {}, viewer)[0] == 403
    assert _req(url + "/reject", "POST", {}, viewer)[0] == 403
    operator = _login(local_url, auth.OPERATOR)
    assert _req(url + "/apply", "POST", {}, {**operator, auth.CSRF_HEADER: "wrong"})[0] == 403
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.PENDING
    assert _req(url + "/apply", "POST", {}, operator)[0] == 200
    assert sp.get_proposal(pr.id).decided_by == "ui:operator"


def test_changed_secret_is_visible_without_revealing_it():
    a, b = proposal_diff._yaml_text({"api_token": "old-secret"}), proposal_diff._yaml_text({"api_token": "new-secret"})
    assert a != b and "secret" not in a + b and MASKED_VALUE[:-3] in a
    assert proposal_diff._yaml_text({"api_token": "old-secret"}) == a  # stable within a process


def test_secret_patterns_in_strings_are_scrubbed():
    out = proposal_diff._yaml_text(
        {"cmd": "curl -H 'Authorization: Bearer abcdefgh12345678' postgres://u:hunter2@h/db"}
    )
    assert "abcdefgh12345678" not in out and "hunter2" not in out


def test_list_by_status_separates_applied_and_rejected_and_ignores_legacy_records(spec_file):
    applied = _propose(spec_file, {"loops": {"a": NEW_LOOP}})
    sp.apply(applied.id, "t")
    rejected = _propose(spec_file, {"loops": {"b": NEW_LOOP}})
    sp.reject(rejected.id, "t")
    pending = _propose(spec_file, {"loops": {"c": NEW_LOOP}})
    # legacy layout: a decided record that still sits in the top-level (pending) dir
    legacy = sp.get_proposal(rejected.id)
    sp._write(legacy)
    ids = lambda st: {p.id for p in sp.list_proposals(st)}  # noqa: E731
    assert ids(sp.ProposalStatus.APPLIED) == {applied.id}
    assert ids(sp.ProposalStatus.REJECTED) == {rejected.id}
    assert ids(sp.ProposalStatus.PENDING) == {pending.id}
    assert ids(None) == {applied.id, rejected.id, pending.id}


def test_get_prefers_decided_record_when_a_crash_left_both(spec_file):
    pr = _propose(spec_file, {"loops": {"a": NEW_LOOP}})
    stale_pending = sp.get_proposal(pr.id)
    sp.reject(pr.id, "t")
    sp._write(stale_pending)  # simulate: decided file written, pending not yet unlinked
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.REJECTED


def test_unknown_proposal_is_not_found_and_corrupt_is_400(fabrikant_url, spec_file):
    assert _req(f"{fabrikant_url}{PROPOSALS}/nope")[0] == 404
    pr = _propose(spec_file, {"loops": {"a": NEW_LOOP}})
    sp._path(pr.id).write_text("{broken")
    assert _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}")[0] == 400
    assert _req(f"{fabrikant_url}{PROPOSALS}/{pr.id}/apply", "POST", {})[0] == 409
