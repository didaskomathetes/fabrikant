"""Spec-change proposals (MCP spec-write): validation, stale-hash refusal, confinement, apply/reject,
no secret echo, and the ``fabrikant proposals`` CLI plus the MCP tools on top."""

from __future__ import annotations

import json

import pytest
from mcp_support import call, text

from fabrikant import cli, config, specs
from fabrikant import mcp_server as mcp
from fabrikant import spec_proposals as sp

SPEC = """\
# keep me
name: p
root: {root}
description: old
loops:
  l:
    steps:
      - {{type: shell, cmd: 'true'}}
"""
NEW_LOOP = {"steps": [{"type": "shell", "cmd": "echo hi"}]}


@pytest.fixture
def proj(projects_dir, runs_dir, monkeypatch, tmp_path):
    """One valid project ``p`` in an isolated projects dir + state dir."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    path = projects_dir / "p.fabrikant.yaml"
    path.write_text(SPEC.format(root=root))
    return path


def _hash(path):
    return specs.spec_hash(path)


# ------------------------------------------------------------- save_spec options
def test_save_spec_dry_run_leaves_file_untouched(proj):
    before = proj.read_bytes()
    saved = specs.save_spec("p", specs.SpecUpdate(project_fields={"description": "new"}), dry_run=True)
    assert saved["description"] == "new" and proj.read_bytes() == before
    assert not list(proj.parent.glob("*.tmp"))


def test_save_spec_backup_keeps_previous_file(proj):
    before = proj.read_bytes()
    specs.save_spec("p", specs.SpecUpdate(project_fields={"description": "new"}), backup=True)
    assert (proj.parent / ("p.fabrikant.yaml" + specs.SPEC_BACKUP_SUFFIX)).read_bytes() == before
    assert "description: new" in proj.read_text() and "# keep me" in proj.read_text()


# ---------------------------------------------------------------------- propose
def test_propose_stores_a_pending_proposal_without_writing(proj):
    before = proj.read_bytes()
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    assert pr.status is sp.ProposalStatus.PENDING and pr.changes == ["field description: set"]
    assert proj.read_bytes() == before
    assert sp.get_proposal(pr.id).patch == {"description": "new"}
    assert [x.id for x in sp.list_proposals()] == [pr.id]
    assert sp.list_proposals(sp.ProposalStatus.APPLIED) == []


def test_propose_refuses_stale_hash_invalid_and_unknown(proj):
    with pytest.raises(sp.ProposalError, match="stale"):
        sp.propose("p", {"description": "x"}, "0" * 64, "mcp")
    with pytest.raises(sp.ProposalError, match="invalid change"):
        sp.propose("p", {"loops": {"l": {"steps": [{"type": "nope"}]}}}, _hash(proj), "mcp")
    with pytest.raises(sp.ProposalError, match="invalid change"):
        sp.propose("p", {"bogus_field": 1}, _hash(proj), "mcp")
    with pytest.raises(sp.ProposalError, match="unknown project"):
        sp.propose("ghost", {"description": "x"}, _hash(proj), "mcp")
    with pytest.raises(sp.ProposalError, match="base_hash required"):
        sp.propose("p", {"description": "x"}, "", "mcp")
    assert sp.list_proposals() == []


def test_changes_summary_never_contains_values():
    update = specs.SpecUpdate(
        loops={"l2": {"env": {"token": "s3cret"}}}, remove_loops=["old"], project_fields={"description": "s3cret"}
    )
    assert sp.summarize(update) == ["loop l2: set", "loop old: remove", "field description: set"]


def test_spec_outside_projects_dir_is_refused(proj, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", elsewhere)
    monkeypatch.setenv(config.PROJECTS_PATH_ENV, str(proj.parent))
    with pytest.raises(sp.ProposalError, match="outside the projects dir"):
        sp.propose("p", {"description": "x"}, _hash(proj), "mcp")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 80])
def test_proposal_ids_cannot_escape_the_store(bad):
    with pytest.raises(sp.ProposalError, match="invalid proposal id"):
        sp.get_proposal(bad)


def test_unknown_proposal(proj):
    with pytest.raises(sp.ProposalError, match="unknown proposal"):
        sp.get_proposal("nope")
    assert sp.list_proposals() == []


def test_corrupted_status_raises_proposal_error_not_crash(proj):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    path = sp.proposals_dir() / f"{pr.id}.json"
    data = json.loads(path.read_text())
    data["status"] = "bogus"
    path.write_text(json.dumps(data))
    with pytest.raises(sp.ProposalError, match="unknown status"):
        sp.get_proposal(pr.id)
    # one bad record must not crash the listing of the healthy ones
    ok = sp.propose("p", {"description": "other"}, _hash(proj), "mcp")
    listed = sp.list_proposals()
    assert [p.id for p in listed] == [ok.id]


# ------------------------------------------------------------------ apply/reject
def test_apply_writes_with_backup_and_audit(proj):
    before = proj.read_bytes()
    pr = sp.propose("p", {"loops": {"l2": NEW_LOOP}, "description": "new"}, _hash(proj), "mcp")
    applied = sp.apply(pr.id, "cli:me")
    assert (
        applied.status is sp.ProposalStatus.APPLIED and applied.decided_by == "cli:me" and applied.proposed_by == "mcp"
    )
    text = proj.read_text()
    assert "l2:" in text and "description: new" in text and "# keep me" in text
    assert (proj.parent / "p.fabrikant.yaml.bak").read_bytes() == before
    with pytest.raises(sp.ProposalError, match="already applied"):
        sp.apply(pr.id, "cli:me")
    with pytest.raises(sp.ProposalError, match="already applied"):
        sp.reject(pr.id, "cli:me")


def test_apply_refuses_when_spec_changed_since(proj):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    proj.write_text(proj.read_text() + "# edited by a human\n")
    with pytest.raises(sp.ProposalError, match="stale"):
        sp.apply(pr.id, "cli:me")
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.PENDING


def test_apply_maps_final_write_failures_to_proposal_error(proj, monkeypatch):
    # The dry-run check passes, then the real write fails (spec moved on / I/O error):
    # apply must raise a clean ProposalError, not a raw SpecConflict/OSError/traceback.
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    real_save = specs.save_spec

    def fail_on_write(*a, **k):
        if k.get("backup"):
            raise specs.SpecConflict("spec changed externally")
        return real_save(*a, **k)

    monkeypatch.setattr(sp, "save_spec", fail_on_write)
    with pytest.raises(sp.ProposalError, match="stale"):
        sp.apply(pr.id, "cli:me")
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.PENDING

    monkeypatch.setattr(sp, "save_spec", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(sp.ProposalError, match="could not apply"):
        sp.apply(pr.id, "cli:me")


def test_reject_keeps_the_record(proj):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    assert sp.reject(pr.id, "cli:me").status is sp.ProposalStatus.REJECTED
    assert "description: old" in proj.read_text()
    with pytest.raises(sp.ProposalError, match="already rejected"):
        sp.apply(pr.id, "cli:me")


# ---------------------------------------------------------------------- MCP tools
_call = call


WRITE = mcp.McpOptions(allow_spec_write=True)


def test_propose_tool_flow_and_no_secret_echo(proj):
    patch = {"loops": {"l2": {"steps": [{"type": "shell", "cmd": "echo s3cret-value"}]}}}
    res = _call(WRITE, "propose_spec_change", {"project": "p", "base_hash": _hash(proj), "patch": patch})
    assert not res.is_error
    out = res.structured_content
    assert out["status"] == "pending" and out["changes"] == ["loop l2: set"] and out["proposed_by"] == "mcp"
    assert out["id"] in out["next_step"] and "s3cret-value" not in json.dumps(res.structured_content) + text(res)
    assert "l2" not in proj.read_text()  # nothing written until a human applies it
    listed = _call(WRITE, "list_spec_proposals", {}).structured_content
    assert listed["count"] == 1 and listed["proposals"][0]["id"] == out["id"]


def test_propose_tool_refuses_stale_and_invalid(proj):
    stale = _call(WRITE, "propose_spec_change", {"project": "p", "base_hash": "0" * 64, "patch": {"description": "x"}})
    assert stale.is_error and "stale" in text(stale)
    invalid = _call(
        WRITE,
        "propose_spec_change",
        {"project": "p", "base_hash": _hash(proj), "patch": {"loops": {"l": {"steps": [{"type": "nope"}]}}}},
    )
    assert invalid.is_error and "invalid change" in text(invalid)
    extra = _call(WRITE, "propose_spec_change", {"project": "p", "base_hash": _hash(proj), "patch": {"name": "evil"}})
    assert extra.is_error  # patch schema forbids unknown fields (name/root stay untouchable)


def test_no_direct_write_tool_exists(proj):
    res = _call(WRITE, "write_spec_change", {"project": "p", "base_hash": _hash(proj), "patch": {"description": "d"}})
    assert res.is_error and "Unknown tool" in text(res)
    assert "description: old" in proj.read_text()


def test_read_only_hides_spec_write_tools(proj):
    res = _call(
        mcp.McpOptions(read_only=True, allow_spec_write=True),
        "propose_spec_change",
        {"project": "p", "base_hash": "x", "patch": {}},
    )
    assert res.is_error and "Unknown tool" in text(res)


def test_patch_payload_omits_unset_fields():
    assert mcp.SpecPatch(description="d").to_payload() == {"description": "d"}
    assert mcp.SpecPatch(remove_loops=["a"], max_parallel=2).to_payload() == {"remove_loops": ["a"], "max_parallel": 2}


# --------------------------------------------------------------------------- CLI
def test_cli_proposals_list_show_apply_reject(proj, capsys):
    pr = sp.propose("p", {"description": "via-cli"}, _hash(proj), "mcp")
    cli.main(["proposals", "list"])
    assert pr.id in capsys.readouterr().out
    cli.main(["proposals", "show", pr.id])
    assert json.loads(capsys.readouterr().out)["patch"] == {"description": "via-cli"}
    cli.main(["proposals", "apply", pr.id])
    assert "description: via-cli" in proj.read_text()
    pr2 = sp.propose("p", {"description": "again"}, _hash(proj), "mcp")
    cli.main(["proposals", "reject", pr2.id])
    assert sp.get_proposal(pr2.id).status is sp.ProposalStatus.REJECTED
    assert pr2.id in capsys.readouterr().out


def test_cli_proposals_error_exits_1(proj, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["proposals", "apply", "nope"])
    assert e.value.code == 1 and "unknown proposal" in capsys.readouterr().out


def test_cli_mcp_passes_flags_and_env_to_the_server(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp, "serve_stdio", lambda options=None: seen.update(options=options))
    monkeypatch.setenv(mcp.ALLOW_APPROVALS_ENV, "1")
    cli.main(["mcp", "--read-only", "--allow-spec-write"])
    assert seen["options"] == mcp.McpOptions(read_only=True, allow_approvals=True, allow_spec_write=True)


def test_propose_refuses_a_patch_echoing_the_redaction_mask(proj):
    # get_spec masks secrets; an agent echoing a whole loop back must not overwrite the real value.
    echoed = {"loops": {"l": {"steps": [{"type": "shell", "cmd": "x", "env": {"api_key": "***redacted***"}}]}}}
    with pytest.raises(sp.ProposalError, match="masked"):
        sp.propose("p", echoed, _hash(proj), "mcp")
    assert sp.list_proposals() == []


def test_apply_refuses_a_stored_patch_with_the_mask(proj):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    record = sp.get_proposal(pr.id)
    record.patch = {"description": "***redacted***"}  # e.g. written by an older version
    sp._write(record)
    with pytest.raises(sp.ProposalError, match="masked"):
        sp.apply(pr.id, "cli:me")
    assert "old" in proj.read_text()
    assert sp.get_proposal(pr.id).status is sp.ProposalStatus.PENDING


def test_write_leaves_no_temp_file_on_failure(proj, monkeypatch):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sp.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        sp._write(sp.get_proposal(pr.id))
    assert not list(sp.proposals_dir().glob("*.tmp"))


def test_apply_and_reject_are_serialised_by_a_lock(proj, monkeypatch):
    pr = sp.propose("p", {"description": "new"}, _hash(proj), "mcp")
    held: list[bool] = []
    real_locked = sp.locked

    def spy(path):
        held.append(True)
        return real_locked(path)

    monkeypatch.setattr(sp, "locked", spy)
    sp.reject(pr.id, "cli:me")
    assert held and not (proj.parent / "p.fabrikant.yaml.bak").exists()
    with pytest.raises(sp.ProposalError, match="already rejected"):
        sp.apply(pr.id, "cli:me")
