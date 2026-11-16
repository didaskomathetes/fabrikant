"""Trail-Archiv (ADR 0037, Nachtrag 2026-11-03): Hash, Methoden, archivieren + entfernen, wiederherstellen."""

import subprocess
from pathlib import Path

import pytest
from helpers import http_error, not_none
from test_relay_commands import (
    ISSUE,
    LABELS,
    NOW,
    SESSION_DIR,
    FakeGitHub,
    RecordingContext,
    fake_history,
    finished,
    git,
    init_repo,
    make_ctx,
    make_task,
    starter,
)

from fabrik.relay import archive, commands, status
from fabrik.relay.config import LOOP_SESSION, RelayConfig, TrailArchiveConfig
from fabrik.relay.model import Status, StatusState, TrailRef
from fabrik.relay.workspace import Workspace, handoff_path, relay_dir
from fabrik.runmodel import FailureKind

PASS = "## Summary\nAll good.\n\n<!-- relay-audit verdict=pass -->"
FAIL = "## Summary\nBroken.\n\n<!-- relay-audit verdict=fail -->\n"
GIT_REF = TrailArchiveConfig("git-ref")
LOCAL = TrailArchiveConfig("local")
TRAIL = relay_dir(ISSUE)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)


@pytest.fixture
def no_runs(monkeypatch):
    fake_history(monkeypatch, [])


@pytest.fixture
def local_base(tmp_path, monkeypatch) -> Path:
    base = tmp_path / "data"
    monkeypatch.setattr(archive.config, "DATA_DIR", base)
    return base / archive.LOCAL_ARCHIVE_DIR


def audit(repo: Path, gh: FakeGitHub, report: str | None = PASS, **cfg) -> tuple[RecordingContext, str]:
    ctx = make_ctx(repo, gh, **cfg)
    commands.audit_prepare(ctx, ISSUE)
    (repo / SESSION_DIR / "audit" / "blind" / "01-count.sql").write_text("select count(*)")
    commands.audit_reveal(ctx, ISSUE)
    if report is not None:
        (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text(report)
    return ctx, commands.audit_publish(ctx, ISSUE)


def tracked(repo: Path, rev: str = "HEAD") -> list[str]:
    return [p for p in git(repo, "ls-tree", "-r", "--name-only", rev, "--", TRAIL).splitlines() if p]


# ── Hash ─────────────────────────────────────────────────────────────────────


def test_digest_is_order_independent_and_content_sensitive():
    a = {"HANDOFF.md": b"h", "scripts/s1.sql": b"x"}
    assert archive.trail_digest(a) == archive.trail_digest(dict(reversed(a.items())))
    assert archive.trail_digest(a) != archive.trail_digest({**a, "scripts/s1.sql": b"y"})
    assert archive.trail_digest(a) != archive.trail_digest({"HANDOFF.md": b"h", "scripts/s2.sql": b"x"})


def test_digest_ignores_only_the_trailer_line_of_the_report():
    report = archive.normalize_report("ok")
    base = archive.trail_digest({"AUDIT.md": report.encode()})
    signed = report + archive.audit_trailer("a" * 64)
    assert archive.trail_digest({"AUDIT.md": signed.encode()}) == base
    assert archive.trail_digest({"AUDIT.md": (signed + "tampered\n").encode()}) != base
    # Nur in AUDIT.md: dieselbe Zeile in einer anderen Datei zählt mit.
    assert archive.trail_digest({"x.md": signed.encode()}) != archive.trail_digest({"x.md": report.encode()})


def test_trail_ref_from_dict_is_tolerant_and_state_round_trips():
    ref = TrailRef("local", ISSUE, 2, "/a/7/audit-2", "f" * 64, "abc", "2026-11-03T00:00:00Z")
    assert ref.name == "audit-2"
    assert TrailRef.from_dict({"method": "local"}) is None and TrailRef.from_dict("x") is None
    state = StatusState(audit="pass", archive=ref, trail_sha256="f" * 64, trail_warning="w")
    assert StatusState.from_dict(state.to_dict()) == state


# ── Config ───────────────────────────────────────────────────────────────────


def spec(trail_archive) -> dict:
    return {
        "name": "demo",
        "github": "o/r",
        "relay": {"allowed": ["me"], "bot_login": "bot", "trail_archive": trail_archive},
    }


def test_config_parses_known_methods_and_rejects_planned_and_unknown_ones():
    assert RelayConfig.from_spec(spec({"type": "git-ref"})).trail_archive == GIT_REF
    assert RelayConfig.from_spec(spec(None)).trail_archive is None
    with pytest.raises(ValueError, match="noch nicht umgesetzt"):
        RelayConfig.from_spec(spec({"type": "s3"}))
    with pytest.raises(ValueError, match="eins von"):
        RelayConfig.from_spec(spec({"type": "tag"}))
    with pytest.raises(ValueError, match="Mapping mit `type`"):
        RelayConfig.from_spec(spec({"type": "local", "bucket": "b"}))


# ── audit-publish: archivieren + entfernen ───────────────────────────────────


def test_pass_with_git_ref_archives_audit_commit_then_strips_trail(repo):
    gh = finished(repo)
    ctx, summary = audit(repo, gh, trail_archive=GIT_REF)
    state = gh.states[ISSUE]
    ref = not_none(state.archive)
    audit_commit = git(repo, "rev-parse", "HEAD~1").strip()
    assert gh.refs == {f"refs/fabrik/relay/{ISSUE}/audit-1": audit_commit} and ref.commit == audit_commit
    assert ref.sha256 == state.trail_sha256
    # Audit-Commit enthält den Trail inkl. Hash-Zeile; der Lösch-Commit danach nichts mehr davon.
    report = git(repo, "show", f"{audit_commit}:{TRAIL}/AUDIT.md")
    assert report.endswith(archive.audit_trailer(ref.sha256)) and "All good." in report
    assert f"{TRAIL}/scripts/s1-01.sql" in tracked(repo, audit_commit) and tracked(repo) == []
    assert not (repo / TRAIL).exists()
    assert len(ctx.pushed) == 2 and "Trail archiviert (audit-1" in summary
    comment = gh.comments[ISSUE]
    assert "`audit-1`" in comment and f"blob/{audit_commit}/{handoff_path(ISSUE)}" in comment
    assert "Trail:" not in comment  # keine Warnung


def test_pass_with_local_archive_writes_manifest_and_files(repo, local_base):
    gh = finished(repo)
    audit(repo, gh, trail_archive=LOCAL)
    ref = not_none(gh.states[ISSUE].archive)
    target = local_base / "demo" / str(ISSUE) / "audit-1"
    assert ref.uri == str(target) and (target / archive.LOCAL_MANIFEST).is_file()
    assert (target / "trail" / "scripts" / "s1-01.sql").read_text() == "insert …"
    assert archive.trail_digest(archive.read_tree(target / "trail")) == ref.sha256
    assert tracked(repo) == []


def test_existing_archive_number_is_never_overwritten(repo):
    gh = finished(repo)
    gh.refs[f"refs/fabrik/relay/{ISSUE}/audit-1"] = "0" * 40
    audit(repo, gh, trail_archive=GIT_REF)
    assert not_none(gh.states[ISSUE].archive).index == 2
    assert gh.refs[f"refs/fabrik/relay/{ISSUE}/audit-1"] == "0" * 40


def test_failed_archive_keeps_trail_and_warns(repo, monkeypatch):
    gh = finished(repo)

    def broken(ref, sha):
        raise http_error(500)

    monkeypatch.setattr(gh, "create_ref", broken)
    ctx, summary = audit(repo, gh, trail_archive=GIT_REF)
    state = gh.states[ISSUE]
    assert state.archive is None and "trail not archived" in state.trail_warning
    assert f"{TRAIL}/HANDOFF.md" in tracked(repo) and len(ctx.pushed) == 1
    assert "Trail nicht archiviert" in summary and "⚠️ **Trail:**" in gh.comments[ISSUE]
    assert state.audit == "pass"  # das Verdikt bleibt gültig


def test_unverifiable_archive_is_not_stripped(repo, monkeypatch):
    gh = finished(repo)
    monkeypatch.setattr(gh, "ref_sha", lambda ref: None)  # Ref nach dem Anlegen nicht auffindbar
    audit(repo, gh, trail_archive=GIT_REF)
    assert gh.states[ISSUE].archive is None and f"{TRAIL}/HANDOFF.md" in tracked(repo)


def test_fail_verdict_archives_nothing(repo):
    gh = finished(repo)
    audit(repo, gh, report=FAIL, trail_archive=GIT_REF)
    assert gh.refs == {} and gh.states[ISSUE].archive is None
    assert f"{TRAIL}/AUDIT.md" in tracked(repo)
    assert gh.states[ISSUE].trail_sha256  # auch ein fail ist an den Trail gebunden


def test_without_config_warns_when_a_pr_contains_the_trail(repo):
    gh = finished(repo)
    gh.prs[f"fabrik/relay/{ISSUE}"] = 42
    audit(repo, gh)
    assert "PR #42 still contains the relay trail" in gh.states[ISSUE].trail_warning
    assert f"{TRAIL}/AUDIT.md" in tracked(repo)


def test_without_config_and_without_pr_there_is_no_warning(repo):
    gh = finished(repo)
    audit(repo, gh)
    assert gh.states[ISSUE].trail_warning == ""


# ── Trail-Link im PR-Body ────────────────────────────────────────────────────

PR = 42
BRANCH = f"fabrik/relay/{ISSUE}"


def test_pass_links_the_archive_in_the_pr_body_and_keeps_its_text(repo):
    gh = finished(repo)
    gh.prs[BRANCH], gh.pr_bodies[PR] = PR, "## Included\n| boto3 |"
    _, summary = audit(repo, gh, trail_archive=GIT_REF)
    ref = not_none(gh.states[ISSUE].archive)
    body = gh.pr_bodies[PR]
    assert body.startswith("## Included\n| boto3 |\n\n" + status.PR_TRAIL_START)
    assert f"[`audit-1`]({ref.uri})" in body and f"`refs/fabrik/relay/{ISSUE}/audit-1`" in body
    assert f"blob/{ref.commit}/{TRAIL}/AUDIT.md" in body and ref.sha256 in body
    assert f"git fetch origin refs/fabrik/relay/{ISSUE}/audit-1" in body
    assert f"PR #{PR}: Trail-Link gesetzt" in summary


def test_pr_trail_block_is_replaced_not_duplicated():
    ref = TrailRef("git-ref", ISSUE, 1, "https://github.com/o/r/tree/abc", "a" * 64, "abc", "")
    newer = TrailRef("git-ref", ISSUE, 2, "https://github.com/o/r/tree/def", "b" * 64, "def", "")
    block = status.pr_trail_block("o/r", ISSUE, TRAIL, ref, "refs/x/1")
    once = status.with_pr_trail("Text", block)
    assert status.with_pr_trail(once, block) == once
    twice = status.with_pr_trail(once, status.pr_trail_block("o/r", ISSUE, TRAIL, newer, "refs/x/2"))
    assert twice.count(status.PR_TRAIL_START) == 1 and "audit-2" in twice and "audit-1" not in twice
    assert status.with_pr_trail(twice, None) == "Text\n"
    assert status.with_pr_trail("", block) == block + "\n"


def test_local_archive_block_names_the_path_without_a_link():
    ref = TrailRef("local", ISSUE, 1, "/data/relay-archive/x", "c" * 64, "abc", "")
    block = status.pr_trail_block("o/r", ISSUE, TRAIL, ref, "")
    assert "local `/data/relay-archive/x`" in block and "git fetch" not in block and "](" not in block


def test_without_a_pr_nothing_is_written(repo):
    gh = finished(repo)
    _, summary = audit(repo, gh, trail_archive=GIT_REF)
    assert gh.pr_body_writes == 0 and "PR #" not in summary


def test_a_failed_pr_update_keeps_the_archive(repo, monkeypatch):
    gh = finished(repo)
    gh.prs[BRANCH] = PR

    def broken(number, body):
        raise http_error(403)

    monkeypatch.setattr(gh, "set_pr_body", broken)
    _, summary = audit(repo, gh, trail_archive=GIT_REF)
    assert gh.states[ISSUE].archive is not None and tracked(repo) == []
    assert "PR-Body nicht aktualisiert" in summary


def test_restoring_the_trail_removes_the_pr_link(repo):
    gh = finished(repo)
    gh.prs[BRANCH], gh.pr_bodies[PR] = PR, "Body"
    audit(repo, gh, trail_archive=GIT_REF)
    reopen(gh)
    summary = commands.checkout(make_ctx(repo, gh, trail_archive=GIT_REF), ISSUE)
    assert gh.pr_bodies[PR] == "Body\n" and "Trail-Link entfernt" in summary


# ── Wiederherstellen ─────────────────────────────────────────────────────────


def reopen(gh: FakeGitHub) -> None:
    """Mensch entfernt `done`/`audited`: es soll weitergearbeitet werden."""
    gh.sync_labels(ISSUE, gh.task(ISSUE, "").labels, set(), {LABELS.done, LABELS.audited})


@pytest.mark.parametrize("method", [GIT_REF, LOCAL])
def test_next_session_restores_the_archived_trail_as_a_commit(repo, local_base, method):
    gh = finished(repo)
    audit(repo, gh, trail_archive=method)
    reopen(gh)
    ctx = make_ctx(repo, gh, trail_archive=method)
    summary = commands.checkout(ctx, ISSUE)
    assert "Trail aus audit-1 wiederhergestellt" in summary
    assert "restore trail from audit-1" in git(repo, "log", "-1", "--format=%s")
    assert f"{TRAIL}/scripts/s1-01.sql" in tracked(repo)
    h = not_none(Workspace(repo, ISSUE).read_handoff())
    assert (h.session, h.status) == (2, Status.ACTIVE)


def test_tampered_local_archive_fails_the_step_as_environment(repo, local_base):
    gh = finished(repo)
    audit(repo, gh, trail_archive=LOCAL)
    ref = not_none(gh.states[ISSUE].archive)
    (Path(ref.uri) / "trail" / "scripts" / "s1-01.sql").write_text("drop table x")
    reopen(gh)
    with pytest.raises(commands.RelayError, match="nicht wiederherstellbar.*Hash") as e:
        commands.checkout(make_ctx(repo, gh, trail_archive=LOCAL), ISSUE)
    assert e.value.failure.kind is FailureKind.ENVIRONMENT


def test_restore_is_a_no_op_while_the_trail_is_on_the_branch(repo):
    gh = finished(repo)
    gh.states[ISSUE] = StatusState(archive=TrailRef("local", ISSUE, 1, "/nowhere", "0" * 64, "abc", ""))
    ctx = make_ctx(repo, gh)
    assert commands.restore_trail(ctx, Workspace(repo, ISSUE), gh.states[ISSUE]) == ""


def test_dispatcher_reads_the_handoff_from_a_git_ref_archive(repo, no_runs):
    gh = finished(repo)
    audit(repo, gh, trail_archive=GIT_REF)
    reopen(gh)
    ref = not_none(gh.states[ISSUE].archive)
    gh.files[f"{ref.commit}:{handoff_path(ISSUE)}"] = git(repo, "show", f"{ref.commit}:{handoff_path(ISSUE)}")
    calls: list = []
    commands.dispatch(make_ctx(repo, gh, trail_archive=GIT_REF), start=starter(calls))
    assert calls == [("demo", LOOP_SESSION, ISSUE, f"relay #{ISSUE} s2")]  # nicht „erste Session“


def test_dispatcher_blocks_when_the_archive_is_unreadable(repo, no_runs, local_base):
    gh = FakeGitHub([make_task()])
    gh.states[ISSUE] = StatusState(archive=TrailRef("local", ISSUE, 1, str(local_base / "gone"), "0" * 64, "a", ""))
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls == [] and "Trail-Archiv nicht lesbar" in out
    assert LABELS.blocked in gh.task(ISSUE, "").labels


def test_unknown_archive_method_is_an_environment_error(repo):
    with pytest.raises(commands.RelayError, match="unbekannte Archiv-Methode"):
        make_ctx(repo, FakeGitHub([make_task()])).archive("s3")


def test_git_ref_restore_fetches_a_missing_commit(tmp_path):
    root = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    ref = TrailRef("git-ref", ISSUE, 1, "u", "0" * 64, "1" * 40, "")
    fetched: list[str] = []

    def fetch(spec: str) -> None:
        fetched.append(spec)

    gh = FakeGitHub([])
    with pytest.raises(archive.ArchiveError):  # Fetch ist ein Fake: Commit bleibt unbekannt
        archive.GitRefArchive(gh, "o/r", root, fetch).restore(ref)
    assert fetched == [f"+refs/fabrik/relay/{ISSUE}/audit-1:refs/fabrik/relay/{ISSUE}/audit-1"]
    with pytest.raises(archive.ArchiveError, match="kein Fetch"):
        archive.GitRefArchive(gh, "o/r", root).restore(ref)


def test_no_free_archive_number_raises(repo):
    class Full:
        method = "full"

        def archive(self, ws, commit, index, now):
            raise archive.ArchiveExists(str(index))

    with pytest.raises(archive.ArchiveError, match="keine freie Archiv-Nummer"):
        archive.archive_next(Full(), Workspace(repo, ISSUE), "abc", 1, NOW)  # type: ignore[arg-type]


def test_dispatcher_skips_the_archive_for_finished_tasks(repo, no_runs, local_base):
    gh = FakeGitHub([make_task(labels=(LABELS.task, LABELS.done, LABELS.audited))])
    gh.states[ISSUE] = StatusState(archive=TrailRef("local", ISSUE, 1, str(local_base / "gone"), "0" * 64, "a", ""))
    out = commands.dispatch(make_ctx(repo, gh), start=starter([]))
    assert f"#{ISSUE}: skip — fertig" in out
