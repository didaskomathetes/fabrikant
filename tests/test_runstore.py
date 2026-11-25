"""runstore: Record → Modell (Defaults, Fehlergründe), RunStore-Lesepfad, Ladereport,
typisierte Funktionen in runs, Write-Operationen mit Vorbedingung (ADR 0034, Schritt 2)."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import pytest

from fabrikant import runs
from fabrikant.runmodel import ApprovalReason, ApprovalStatus, RunStatus, StepStatus
from fabrikant.runstore import (
    PreconditionError,
    RecordError,
    RunStore,
    RunStoreError,
    attempt_of,
    parse_run,
    valid_project,
    valid_run_id,
)


def _rec(**kw):
    return {"id": "r1", "project": "p", "loop": "l", "status": "pass", **kw}


def _save(runs_dir, rec, project="p"):
    d = runs_dir / project
    d.mkdir(exist_ok=True)
    (d / f"{rec['id']}.json").write_text(json.dumps(rec))


def test_minimal_record_gets_documented_defaults():
    run = parse_run({"id": "r1", "status": "running"}, "p")
    assert (run.project, run.loop, run.trigger, run.note, run.started, run.finished) == ("p", "", "", "", "", "")
    assert run.steps == () and run.approval is None and dict(run.vars) == {}
    assert run.started_by is None and run.approved_by is None and run.rejected_by is None
    assert run.resumed is False and run.error == "" and run.approval_queued == ""


def test_directory_name_wins_over_record_project():
    assert parse_run(_rec(project="other"), "p").project == "p"
    assert parse_run(_rec(project="other")).project == "other"


@pytest.mark.parametrize(
    ("rec", "reason"),
    [
        ({"status": "pass", "project": "p"}, "id"),
        ({"id": "", "status": "pass", "project": "p"}, "id"),
        ({"id": 3, "status": "pass", "project": "p"}, "id"),
        ({"id": "r", "status": "pass"}, "project"),
        ({"id": "r", "project": "p", "status": "weird"}, "unbekannter Status 'weird'"),
        ({"id": "r", "project": "p"}, "unbekannter Status None"),
        ({"id": "r", "project": "p", "status": "pass", "steps": [{"name": "a"}]}, "Step 1"),
    ],
)
def test_unloadable_records_name_the_reason(rec, reason):
    with pytest.raises(RecordError, match=reason):
        parse_run(rec)


def test_wrongly_typed_fields_fall_back_to_defaults():
    run = parse_run(_rec(started=5, trigger=None, resumed="yes", vars={"a": 1, "b": [1], "c": "x"}, started_by=7))
    assert run.started == "" and run.trigger == "" and run.resumed is False
    assert dict(run.vars) == {"a": 1, "c": "x"} and run.started_by is None


def test_run_vars_is_read_only():
    """Run ist frozen; vars ist schreibgeschützt (MappingProxyType) — keine shallow-Mutation."""
    run = parse_run(_rec(vars={"a": 1}))
    assert run.vars["a"] == 1
    # cast: der Test prüft gerade, dass die (als Mapping typisierte) Sicht zur Laufzeit nicht mutierbar ist.
    with pytest.raises(TypeError):
        cast(dict, run.vars)["x"] = 1
    with pytest.raises(TypeError):
        del cast(dict, run.vars)["a"]
    # Default (kein vars-Feld) ist ebenfalls schreibgeschützt.
    empty = parse_run(_rec())
    with pytest.raises(TypeError):
        cast(dict, empty.vars)["x"] = 1


def test_steps_usage_review_verify():
    run = parse_run(
        _rec(
            steps=[
                "kaputt",
                {
                    "name": "a",
                    "type": "agent",
                    "status": "pass",
                    "duration_s": 1.5,
                    "usage": {"model": "m", "total_tokens": 3, "cost_usd": 0.5, "prompt_tokens": True},
                },
                {
                    "name": "r",
                    "type": "pr_review",
                    "status": "fail",
                    "usage": None,
                    "output": "o",
                    "summary": "s",
                    "review": {
                        "verdict": "changes_requested",
                        "pr": "12",
                        "findings": [{"severity": "high", "text": "x"}, 1],
                    },
                },
                {"name": "v", "type": "verify", "status": "timeout", "verify": [{"name": "c", "ok": True}, "x"]},
                {"name": "w", "type": "shell", "status": "waiting", "review": {"pr": 4, "round": 2}},
            ]
        )
    )
    a, r, v, w = run.steps
    assert a.status is StepStatus.PASS and a.duration_s == 1.5
    assert a.usage and a.usage.total_tokens == 3 and a.usage.cost_usd == 0.5 and a.usage.prompt_tokens == 0
    assert r.usage is None and r.output == "o" and r.summary == "s"
    assert r.review and r.review.pr == 12 and r.review.round == 1 and len(r.review.findings) == 1
    assert r.review.findings[0].severity == "high"
    assert v.status is StepStatus.TIMEOUT and v.verify[0].ok and len(v.verify) == 1
    assert w.review and w.review.pr == 4 and w.review.round == 2
    assert parse_run(_rec(steps=[{"status": "pass", "review": {"pr": "x"}}])).steps[0].review.pr is None


@pytest.mark.parametrize(
    ("status", "extra", "expected"),
    [
        ("needs_approval", {}, ApprovalStatus.PENDING),
        ("needs_approval", {"approval_queued": "2026-01-01T00:00:00"}, ApprovalStatus.QUEUED),
        ("rejected", {}, ApprovalStatus.REJECTED),
        ("pass", {"resumed": True}, ApprovalStatus.APPROVED),
        ("running", {"approved_by": "ann"}, ApprovalStatus.APPROVED),
        ("error", {}, ApprovalStatus.PENDING),
    ],
)
def test_approval_status_is_derived(status, extra, expected):
    run = parse_run(_rec(status=status, approval={"reason": "step", "step": "s", "what": "w"}, **extra))
    assert run.approval and run.approval.status is expected


def test_approval_fields_and_reason_fallback():
    rec = _rec(status="needs_approval", waiting_step="tier-approval", approval={"tier": 2, "why": "w", "issue": "i"})
    run = parse_run(rec)
    assert run.approval and run.approval.reason is ApprovalReason.TIER and run.approval.tier == 2
    assert run.awaiting_tier_approval
    step = parse_run(_rec(approval={"reason": "bogus", "condition": "c", "tier": True}))
    assert step.approval and step.approval.reason is ApprovalReason.STEP and step.approval.tier is None
    assert step.approval.condition == "c"
    assert parse_run(_rec(approval={"reason": "gate"})).approval.reason is ApprovalReason.GATE  # type: ignore[union-attr]
    assert parse_run(_rec(approval="x")).approval is None


def test_store_load_all_report_and_get(runs_dir):
    _save(runs_dir, _rec(id="a"))
    _save(runs_dir, _rec(id="b", status="queued"), "q")
    (runs_dir / "p" / "broken.json").write_text("{")
    (runs_dir / "p" / "list.json").write_text("[]")
    (runs_dir / "p" / "bad.json").write_text(json.dumps({"id": "bad", "status": "x"}))
    (runs_dir / "stray.txt").write_text("not a dir")
    store = RunStore()
    loaded, report = store.load_report()
    assert sorted(r.id for r in loaded) == ["a", "b"] and report.loaded == 2 and report.total == 5
    reasons = {f.path.name: f.reason for f in report.failures}
    assert "kein gültiges JSON" in reasons["broken.json"]
    assert "erwartet Object, ist list" in reasons["list.json"]
    assert "unbekannter Status" in reasons["bad.json"]
    assert [r.id for r in store.load_all("q")] == ["b"] and store.load_all("../x") == []
    assert store.get("a").status is RunStatus.PASS  # type: ignore[union-attr]
    assert store.get("a", "q") is None and store.get("b").project == "q"  # type: ignore[union-attr]
    assert store.get("../a") is None and store.get("bad", "p") is None and store.get("zzz") is None
    assert store.get("a", "../p") is None


def test_store_missing_root_and_explicit_dir(tmp_path, runs_dir):
    assert RunStore(tmp_path / "nope").load_all() == []
    _save(tmp_path, _rec(id="x"))
    assert [r.id for r in RunStore(tmp_path).load_all()] == ["x"]
    assert RunStore(tmp_path).root == tmp_path


def test_store_race_with_delete(runs_dir, monkeypatch):
    _save(runs_dir, _rec(id="a"))
    store = RunStore()
    monkeypatch.setattr(type(runs_dir), "read_text", lambda self, *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    loaded, report = store.load_report()
    assert loaded == [] and report.failures[0].reason == "Datei verschwunden"


def test_store_reuses_parsed_run_until_file_changes(runs_dir):
    """Parse-Cache: unveränderte Datei → derselbe Run; Write (neue Inode) oder externe Änderung → neu geparst."""
    _save(runs_dir, _rec(id="a"))
    store = RunStore()
    first = store.load_all()[0]
    assert store.load_all()[0] is first
    store.update_fields("p", "a", {"note": "per Store"})
    assert store.load_all()[0].note == "per Store"
    f = runs_dir / "p" / "a.json"
    f.write_text(json.dumps(_rec(id="a", note="extern, längerer Inhalt")))  # in-place, gleiche Inode
    assert store.load_all()[0].note == "extern, längerer Inhalt"


def test_store_forgets_deleted_records(runs_dir):
    _save(runs_dir, _rec(id="a"))
    _save(runs_dir, _rec(id="b"))
    _save(runs_dir, _rec(id="c"), "q")
    store = RunStore()
    store.load_all()
    (runs_dir / "p" / "a.json").unlink()
    (runs_dir / "q" / "c.json").unlink()
    assert [r.id for r in store.load_all("p")] == ["b"]
    assert {f.name for f in store._parsed} == {"b.json", "c.json"}  # nur gescannte Projekte bereinigt
    assert [r.id for r in store.load_all()] == ["b"]
    assert {f.name for f in store._parsed} == {"b.json"}


def test_store_stat_race_with_delete(runs_dir, monkeypatch):
    _save(runs_dir, _rec(id="a"))
    f = runs_dir / "p" / "a.json"
    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if self == f:  # nur die Record-Datei verschwindet, das Projekt-Verzeichnis nicht
            raise FileNotFoundError()
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    loaded, report = RunStore().load_report("p")
    assert loaded == [] and report.failures[0].reason == "Datei verschwunden"


def test_runs_typed_readers(runs_dir):
    _save(runs_dir, _rec(id="a", started="2026-01-01T00:00:00", status="needs_approval"))
    _save(runs_dir, _rec(id="b", started="2026-01-02T00:00:00", status="needs_approval"))
    _save(runs_dir, _rec(id="c", status="pass"))
    assert [r.id for r in runs.pending_approval_runs()] == ["b", "a"]
    assert runs.pending_approval_runs("zzz") == []
    assert len(runs.load_runs("p")) == 3
    found = runs.find_run("c", "p")
    assert found and found.status is RunStatus.PASS
    assert runs.find_run("nope") is None


def test_validators():
    assert valid_run_id("a-b") and not valid_run_id("a/b") and not valid_run_id("")
    assert valid_project("p") and not valid_project("..") and not valid_project("a\\b")


@pytest.mark.parametrize(("raw", "expected"), [(2, 2), (-1, 0), ("3", 0), (True, 0), (None, 0)])
def test_parse_run_attempt_only_non_negative_ints(raw, expected):
    assert parse_run({"id": "r1", "status": "pass", "attempt": raw}, "p").attempt == expected


@pytest.mark.parametrize(("raw", "expected"), [(0, 0), (2, 2), (-1, 0), ("3", 0), (True, 0), (None, 0)])
def test_attempt_of_only_non_negative_ints(raw, expected):
    assert attempt_of({"attempt": raw}) == expected


def test_attempt_of_missing_record_is_zero():
    assert attempt_of(None) == 0
    assert attempt_of({}) == 0


# ---------------------------------------------------------------- Write-Operationen (ADR 0034, Schritt 2)
# Gemeinsame Suite: läuft gegen das JSON- und das SQL-Backend (Fixture `store`); geprüft wird über
# die Store-API (`get_record`), nicht über Dateien.
def _needs_approval(**kw):
    return {"id": "r1", "project": "p", "loop": "l", "status": "needs_approval", **kw}


def _stored(store, run_id="r1"):
    return store.get_record(run_id, "p")


def test_create_new_record(store):
    rec = store.create("p", "r1", _rec(id="r1", status="running", note="n"))
    on_disk = _stored(store)
    assert on_disk["status"] == rec["status"] == "running" and on_disk["note"] == "n" and on_disk["loop"] == "l"


def test_create_fails_if_record_exists(store):
    store.create("p", "r1", _rec(id="r1"))
    with pytest.raises(PreconditionError, match="existiert bereits"):
        store.create("p", "r1", _rec(id="r1"))


def test_write_allows_legal_transition(store):
    # Der Übergang wird laut RUN_TRANSITIONS geprüft: running → needs_approval ist erlaubt.
    store.create("p", "r1", _rec(id="r1", status="running"))
    store.write("p", "r1", _rec(id="r1", status="needs_approval"))
    assert _stored(store)["status"] == "needs_approval"


def test_write_allows_same_status_checkpoint(store):
    # Checkpoint-Updates schreiben denselben Status (running → running).
    store.create("p", "r1", _rec(id="r1", status="running"))
    store.write("p", "r1", _rec(id="r1", status="running", note="checkpoint"))
    assert _stored(store)["note"] == "checkpoint"


@pytest.mark.parametrize(
    ("on_disk", "target"),
    [
        ("pass", "running"),  # Terminal-Status: kein Ausgang
        ("fail", "running"),
        ("error", "running"),
        ("rejected", "running"),
        ("running", "queued"),  # kein Rückwärts-Übergang
        ("needs_approval", "pass"),  # nur über running
    ],
)
def test_write_rejects_illegal_transition(store, on_disk, target):
    store.create("p", "r1", _rec(id="r1", status=on_disk))
    with pytest.raises(PreconditionError, match="unerlaubter Übergang"):
        store.write("p", "r1", _rec(id="r1", status=target))
    # Record bleibt unverändert.
    assert _stored(store)["status"] == on_disk


def test_write_missing_record_allowed_with_flag(store):
    # allow_missing=True: Neuanlage (z. B. queued-Platzhalter, den run_loop überschreibt).
    store.write("p", "r1", _rec(id="r1", status="queued"), allow_missing=True)
    assert _stored(store)["status"] == "queued"


def test_write_fails_for_missing_record(store):
    # `write` erwartet einen bestehenden Record; eine Neuanlage ist `create`.
    with pytest.raises(PreconditionError, match="existiert nicht"):
        store.write("p", "r1", _rec(id="r1", status="running"))


def test_write_redacts_in_place(store, monkeypatch):
    # Redaction erfolgt in-place: der In-Memory-Record verliert seine Secrets ebenfalls.
    store.create("p", "r1", _rec(id="r1", status="running"))

    def _fake_redact(r):
        r["note"] = "***"  # in-place, wie der echte `_redact_record`

    monkeypatch.setattr(f"{type(store).__module__}._redact_record", _fake_redact)
    rec = {"id": "r1", "loop": "l", "status": "running", "note": "secret"}
    result = store.write("p", "r1", rec)
    assert result["note"] == "***"  # In-Memory-Record ist redigiert
    assert _stored(store)["note"] == "***"


def test_approve_sets_actor_and_keeps_status(store):
    store.create("p", "r1", _needs_approval())
    rec = store.approve("p", "r1", _needs_approval(), actor="ann")
    on_disk = _stored(store)
    assert rec["approved_by"] == "ann"
    assert on_disk["approved_by"] == "ann" and on_disk["status"] == "needs_approval"


def test_approve_fails_from_other_status(store):
    store.create("p", "r1", _rec(id="r1", status="running"))
    with pytest.raises(PreconditionError):
        store.approve("p", "r1", _rec(id="r1", status="running"), actor="ann")


def test_reject_sets_status_and_actor(store):
    store.create("p", "r1", _needs_approval())
    rec = store.reject("p", "r1", _needs_approval(), actor="bob")
    on_disk = _stored(store)
    assert rec["status"] == "rejected" and rec["rejected_by"] == "bob"
    assert on_disk["status"] == "rejected" and on_disk["rejected_by"] == "bob"


def test_reject_after_approval_that_did_not_resume(store):
    """approved_by ist Audit, keine Sperre: eine (wartende oder gescheiterte) Freigabe lässt sich
    noch ablehnen, solange der Run needs_approval ist; der Queue-Marker verschwindet."""
    store.create("p", "r1", _needs_approval(approved_by="ann", approval_queued="2026-01-01T00:00:00"))
    rec = store.reject("p", "r1", _needs_approval(approved_by="ann", approval_queued="x"), actor="bob")
    on_disk = _stored(store)
    assert rec["status"] == on_disk["status"] == "rejected" and on_disk["rejected_by"] == "bob"
    assert "approval_queued" not in on_disk


def test_reject_fails_after_resume(store):
    store.create("p", "r1", _needs_approval(approved_by="ann", status="running"))
    with pytest.raises(PreconditionError):
        store.reject("p", "r1", _needs_approval(), actor="bob")


def _resume(store, actor: str) -> None:
    """Resume-Write wie `engine._init_record`: needs_approval → running mit Compare-and-Set."""
    rec = _needs_approval(status="running", resumed=True, approved_by=actor)
    store.write("p", "r1", rec, expect=RunStatus.NEEDS_APPROVAL)


@pytest.mark.parametrize(
    "ops",
    [
        (("resume", "ann"), ("reject", "bob")),
        (("resume", "ann"), ("resume", "bob")),
    ],
)
def test_concurrent_resume_is_exclusive(store, ops):
    """Gleichzeitiger Resume + Reject bzw. zwei Resumes desselben Runs: genau einer gewinnt."""
    store.create("p", "r1", _needs_approval())
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(len(ops))

    def attempt(op: str, actor: str) -> None:
        barrier.wait()
        try:
            if op == "resume":
                _resume(store, actor)
            else:
                store.reject("p", "r1", _needs_approval(), actor=actor)
            results.append((op, actor))
        except PreconditionError:
            pass

    threads = [threading.Thread(target=attempt, args=op) for op in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1
    (op, actor), on_disk = results[0], _stored(store)
    if op == "resume":
        assert on_disk["status"] == "running" and on_disk["approved_by"] == actor and on_disk["resumed"] is True
    else:
        assert on_disk["status"] == "rejected" and on_disk["rejected_by"] == actor


def test_write_expect_mismatch_leaves_record(store):
    """`expect`: queued → error überschreibt keinen inzwischen laufenden Run."""
    store.create("p", "r1", _rec(id="r1", status="running", note="live"))
    with pytest.raises(PreconditionError, match="erwartet 'queued'"):
        store.write("p", "r1", _rec(id="r1", status="error"), expect=RunStatus.QUEUED)
    assert _stored(store)["note"] == "live"


@pytest.mark.parametrize("status", ["pass", "fail", "error", "rejected"])
def test_terminal_record_is_not_rewritten(store, status):
    """Gleicher Status ist für Terminal-Status kein Übergang: z. B. überschreibt die Engine nach
    einem Reaper (running → error) dessen Record nicht mit error → error."""
    store.create("p", "r1", _rec(id="r1", status=status, note="first"))
    with pytest.raises(PreconditionError, match="unerlaubter Übergang"):
        store.write("p", "r1", _rec(id="r1", status=status, note="second"))
    assert _stored(store)["note"] == "first"


def test_revoke_approval_clears_audit_and_queue_marker(store):
    store.create("p", "r1", _needs_approval(approved_by="ann", approval_queued="2026-01-01T00:00:00", note="keep"))
    rec = store.revoke_approval("p", "r1")
    on_disk = _stored(store)
    assert rec == on_disk
    assert on_disk["approved_by"] is None and "approval_queued" not in on_disk and on_disk["note"] == "keep"


@pytest.mark.parametrize("status", ["running", "rejected"])
def test_revoke_approval_leaves_decided_run(store, status):
    store.create("p", "r1", _needs_approval(approved_by="ann", status=status))
    with pytest.raises(PreconditionError):
        store.revoke_approval("p", "r1")
    assert _stored(store)["approved_by"] == "ann"


def test_revoke_approval_missing_run(store):
    with pytest.raises(PreconditionError, match="existiert nicht"):
        store.revoke_approval("p", "r1")


def test_mark_approval_queued_sets_marker_and_actor(store):
    store.create("p", "r1", _needs_approval())
    rec = store.mark_approval_queued("p", "r1", _needs_approval(), actor="ann")
    on_disk = _stored(store)
    assert rec["approval_queued"] and rec["approved_by"] == "ann"
    assert on_disk["approval_queued"] and on_disk["approved_by"] == "ann" and on_disk["status"] == "needs_approval"


def test_reap_running_run_sets_error(store):
    store.create("p", "r1", _rec(id="r1", status="running"))
    rec = store.reap("p", "r1", error="stale: >6h")
    on_disk = _stored(store)
    assert rec["status"] == "error" and rec["error"] == "stale: >6h"
    assert on_disk["status"] == "error" and on_disk["failure_kind"] == "interrupted" and on_disk["finished"]


def test_reap_preserves_concurrent_checkpoint(store):
    """Reap ist Read-Modify-Write: ein zwischenzeitlicher Engine-Checkpoint geht nicht verloren."""
    steps = [{"name": "a", "type": "shell", "status": "pass"}]
    store.create("p", "r1", _rec(id="r1", status="running", note="checkpoint", steps=steps))
    rec = store.reap("p", "r1", error="stale")
    assert rec["status"] == "error" and rec["note"] == "checkpoint" and rec["steps"]
    on_disk = _stored(store)
    assert on_disk["note"] == "checkpoint" and on_disk["steps"] == steps and on_disk["status"] == "error"


def test_reap_does_not_overwrite_finished_run(store):
    """Reaper darf einen Run nicht überschreiben, den die Engine bereits beendet hat."""
    store.create("p", "r1", _rec(id="r1", status="pass", finished="2026-01-01T00:00:00"))
    with pytest.raises(PreconditionError):
        store.reap("p", "r1", error="stale")
    on_disk = _stored(store)
    assert on_disk["status"] == "pass" and "error" not in on_disk and "failure_kind" not in on_disk


def test_reap_missing_run(store):
    with pytest.raises(PreconditionError, match="existiert nicht"):
        store.reap("p", "r1", error="stale")


def test_update_fields_is_read_modify_write(store):
    """update_fields setzt nur die übergebenen Felder; zwischenzeitliche Änderungen bleiben erhalten."""
    store.create("p", "r1", _needs_approval(approved_by="ann", approval_queued="2026-01-01T00:00:00"))
    rec = store.update_fields("p", "r1", {"state_merge_error": "boom"})
    assert rec["state_merge_error"] == "boom"
    assert rec["approved_by"] == "ann" and rec["approval_queued"]  # nicht überschrieben
    on_disk = _stored(store)
    assert on_disk["state_merge_error"] == "boom" and on_disk["approved_by"] == "ann"


def test_update_fields_missing_run(store):
    with pytest.raises(PreconditionError, match="existiert nicht"):
        store.update_fields("p", "r1", {"note": "x"})


def test_approve_again_after_resume_did_not_start(store):
    """Scheiterte der Resume nach einem Approve (Run noch needs_approval), darf erneut
    freigegeben werden — approved_by ist Audit der letzten Freigabe, keine Sperre."""
    store.create("p", "r1", _needs_approval(approved_by="ann"))
    store.approve("p", "r1", _needs_approval(approved_by="ann"), actor="bob")
    on_disk = _stored(store)
    assert on_disk["approved_by"] == "bob" and on_disk["status"] == "needs_approval"


def test_write_ops_reject_invalid_ids(store):
    with pytest.raises(RunStoreError):
        store.create("../x", "r1", _rec(id="r1"))
    with pytest.raises(RunStoreError):
        store.write("p", "../r1", _rec(id="r1"))


def test_store_readers(store):
    """get/get_record/load_records/load_all/loop_triggers über beide Backends."""
    store.create("p", "a", _rec(id="a", status="running", trigger="on_issue:#1", started="2026-01-01T00:00:00"))
    store.create("p", "b", _rec(id="b", status="pass", trigger="on_issue:#2"))
    store.create("q", "c", _rec(id="c", status="running", loop="other"))
    assert store.get("a", "p").status is RunStatus.RUNNING  # type: ignore[union-attr]
    assert store.get("a", "q") is None and store.get("c").project == "q"  # type: ignore[union-attr]
    assert store.get_record("zzz") is None and store.get_record("../a") is None
    assert sorted(r["id"] for r in store.load_records()) == ["a", "b", "c"]
    assert sorted(r["id"] for r in store.load_records(status=RunStatus.RUNNING)) == ["a", "c"]
    assert [r["project"] for r in store.load_records("q")] == ["q"] and store.load_records("../x") == []
    assert sorted(r.id for r in store.load_all("p")) == ["a", "b"]
    assert store.loop_triggers("p", "l") == {"on_issue:#1", "on_issue:#2"}
    assert store.loop_triggers("p", "l", ("running",)) == {"on_issue:#1"}
    loaded, report = store.load_report()
    assert len(loaded) == 3 and report.failures == []


def test_full_record_round_trip(store):
    """Alle Felder, die die Engine schreibt, kommen unverändert zurück (Steps, Usage, Review, Verify,
    Approval, Variablen)."""
    rec = {
        "id": "r1",
        "project": "p",
        "loop": "l",
        "trigger": "on_pr:#3",
        "note": "n",
        "spec_hash": "abc",
        "started": "2026-01-01T00:00:00+00:00",
        "finished": "2026-01-01T00:01:00+00:00",
        "status": "fail",
        "failure_kind": "outcome",
        "error": "boom",
        "started_by": "ann",
        "approved_by": "bob",
        "rejected_by": None,
        "resumed": True,
        "waiting_step": "",
        "attempt": 2,
        "retry_of": "r0",
        "trigger_key": "on_pr:#3",
        "worktree_cleanup_error": "wt",
        "scratch_cleanup_error": "sc",
        "state_merge_error": "sm",
        "steps": [
            {
                "name": "a",
                "type": "agent",
                "status": "pass",
                "duration_s": 1.5,
                "summary": "s",
                "usage": {
                    "model": "m",
                    "provider": "p",
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "cost_usd": 0.5,
                },
            },
            {
                "name": "r",
                "type": "pr_review",
                "status": "fail",
                "output": "o",
                "failure_kind": "transient",
                "retry_after": "2026-01-01T01:00:00+00:00",
                "review": {
                    "verdict": "changes_requested",
                    "model": "m",
                    "summary": "x",
                    "pr": 12,
                    "round": 2,
                    "findings": [{"severity": "high", "text": "t"}],
                },
            },
            {
                "name": "v",
                "type": "verify",
                "status": "pass",
                "verify": [{"name": "c1", "ok": True, "output": "ok"}, {"name": "c2", "ok": False}],
            },
            {"name": "z", "type": "shell", "status": "skipped", "summary": "übersprungen"},
        ],
        "approval": {"reason": "criticality", "what": "w", "permissions": ["edit", "bash"], "tier": 2, "pr": "u"},
        "vars": {"s": "x", "i": 3, "f": 1.25, "b": False},
        "queued_vars": {"issue_number": "7"},
    }
    store.create("p", "r1", json.loads(json.dumps(rec)))
    assert _stored(store) == rec
    run = store.get("r1", "p")
    assert run.steps[3].status is StepStatus.SKIPPED and run.approval.reason is ApprovalReason.CRITICALITY  # type: ignore[union-attr]


# ---------------------------------------------------------------- nur JSON-Backend (Dateien)
def test_writes_use_one_lock_file_per_project(runs_dir):
    """Kein Lock-File (und kein Thread-Lock) je Run-Id — nur eins je Projekt, nie als Record geladen."""
    store = RunStore()
    for i in range(3):
        store.create("p", f"r{i}", _rec(id=f"r{i}", status="running"))
        store.reap("p", f"r{i}", error="x")
    assert sorted(f.name for f in (runs_dir / "p").iterdir() if f.name.endswith(".lock")) == [".runs.lock"]
    assert store.load_report("p")[1].total == 3


def test_read_raw_is_utf8_independent_of_locale(runs_dir, tmp_path):
    """Records werden als UTF-8 geschrieben und gelesen — auch unter LANG=C (Container, systemd)."""
    _save(runs_dir, _rec(id="r1", status="running", note="Dead-Man’s-Switch — ü"))
    script = (
        "import sys; from pathlib import Path; from fabrikant.runstore import RunStore; "
        "s = RunStore(Path(sys.argv[1])); s.reap('p', 'r1', error='x'); print(s.get('r1', 'p').note)"
    )
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run(
        [sys.executable, "-c", script, str(runs_dir)], env=env, capture_output=True, text=True, encoding="utf-8"
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Dead-Man’s-Switch — ü"


@pytest.mark.parametrize("content", ["{", "[]"])
def test_write_fails_on_corrupt_on_disk_record(runs_dir, content):
    """Defekter On-Disk-Stand (kein gültiges JSON-Object) → RunStoreError, Record unverändert."""
    d = runs_dir / "p"
    d.mkdir(exist_ok=True)
    (d / "r1.json").write_text(content)
    with pytest.raises(RunStoreError, match="defekter Run-Record"):
        RunStore().write("p", "r1", _rec(id="r1", status="running"))
    assert (d / "r1.json").read_text() == content


def test_atomic_write_keeps_old_record_on_crash(runs_dir, monkeypatch):
    """Crash zwischen Temp-Write und Rename: alter Record bleibt intakt (kein 0-Byte-File)."""
    from pathlib import Path

    _save(runs_dir, _rec(id="r1", status="running"))

    def _boom(self, target):
        raise OSError("simulated crash")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError):
        RunStore().write("p", "r1", _rec(id="r1", status="pass"))
    # Alter Record unverändert — der defekte Zustand landet nur in der (ignorierten) .tmp.
    assert json.loads((runs_dir / "p" / "r1.json").read_text())["status"] == "running"


def test_parse_run_golden_round_trip():
    assert parse_run({"id": "r1", "status": "pass", "golden": "nightly"}, "p").golden == "nightly"
    assert parse_run({"id": "r1", "status": "pass"}, "p").golden == ""
    assert parse_run({"id": "r1", "status": "pass", "golden": 5}, "p").golden == ""
