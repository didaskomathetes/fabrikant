"""Relay-Subcommands gegen ein echtes Git-Repo (tmp) und einen Fake-GitHub."""

import io
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import http_error, not_none

from fabrikant import runs
from fabrikant.config import HARNESS_LOCAL_FILE
from fabrikant.failures import EX_CONFIG, EX_TEMPFAIL, ConfigError
from fabrikant.relay import __main__ as relay_main
from fabrikant.relay import commands
from fabrikant.relay import workspace as workspace_mod
from fabrikant.relay.config import LOOP_AUDIT, LOOP_SESSION, RelayConfig
from fabrikant.relay.github import GitHubError
from fabrikant.relay.model import Brief, Handoff, Labels, Status, StatusState, Task
from fabrikant.relay.status import parse_state
from fabrikant.relay.workspace import PRIVATE_DIR, SESSION_DIR, Workspace, handoff_path
from fabrikant.retry import RetryPolicy
from fabrikant.runmodel import FailureKind

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ISSUE = 7
LABELS = Labels()
BODY = """## Goal
Import rows.

## Session
Batches of 100.

## Verify
- count matches
```verify
cat count.txt
```

## Settings
max_sessions: 5
session_minutes: 30
scope: src/**
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)


def init_repo(tmp_path: Path) -> Path:
    """Arbeitskopie auf `fabrikant/relay/<ISSUE>` mit lokalem Bare-Origin (auch für andere Testmodule)."""
    origin, wt = tmp_path / "origin.git", tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    git(wt, "config", "user.email", "t@example.com")
    git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("x\n")
    git(wt, "add", "README.md")
    git(wt, "commit", "-qm", "init")
    git(wt, "remote", "add", "origin", str(origin))
    git(wt, "push", "-q", "origin", "main")
    git(wt, "fetch", "-q", "origin")
    git(wt, "checkout", "-qb", f"fabrikant/relay/{ISSUE}")
    return wt


class FakeGitHub:
    def __init__(self, tasks: list[Task]):
        self._tasks = {t.number: t for t in tasks}
        self.files: dict[str, str] = {}
        self.file_errors: dict[int, Exception] = {}
        self.states: dict[int, StatusState] = {}
        self.comments: dict[int, str] = {}
        self.label_calls: list[tuple[int, set, set]] = []
        self.refs: dict[str, str] = {}
        self.prs: dict[str, int] = {}
        self.pr_bodies: dict[int, str] = {}
        self.pr_body_writes = 0

    def create_ref(self, ref, sha):
        if ref in self.refs:
            raise http_error(422)
        self.refs[ref] = sha

    def ref_sha(self, ref):
        return self.refs.get(ref)

    def open_pr(self, branch):
        return self.prs.get(branch)

    def pr_body(self, number):
        return self.pr_bodies.get(number, "")

    def set_pr_body(self, number, body):
        self.pr_body_writes += 1
        self.pr_bodies[number] = body

    def tasks(self, label):
        return list(self._tasks.values())

    def task(self, number, label):
        return self._tasks[number]

    def file(self, path, ref):
        tail = ref.rsplit("/", 1)[-1]
        if tail.isdigit() and (err := self.file_errors.get(int(tail))):
            raise err
        return self.files.get(f"{ref}:{path}")

    def status_state(self, number):
        return self.states.get(number, StatusState())

    def upsert_status(self, number, body):
        self.comments[number] = body
        self.states[number] = parse_state(body)

    def sync_labels(self, number, current, add, remove):
        self.label_calls.append((number, set(add), set(remove)))
        t = self._tasks[number]
        self._tasks[number] = Task(**{**t.__dict__, "labels": frozenset((current | add) - remove)})

    def token(self):
        return "tok"

    def commit_identity(self):
        return self.identity

    identity: tuple[str, str] = ("relay-bot[bot]", "1+relay-bot[bot]@users.noreply.github.com")


def make_task(number=ISSUE, labels=(LABELS.task,), body=BODY, **kw) -> Task:
    base = dict(
        number=number,
        title="Import",
        body=body,
        labels=frozenset(labels),
        author="me",
        created_at=NOW - timedelta(days=2),
        label_actor="me",
        label_at=NOW - timedelta(days=1),
    )
    return Task(**{**base, **kw})


@dataclass
class RecordingContext(commands.RelayContext):
    """Push ohne Remote: zeichnet nur den gepushten HEAD auf."""

    pushed: list[str] = field(default_factory=list)

    def push(self, ws: Workspace) -> None:
        self.pushed.append(ws.git("rev-parse", "HEAD").strip())


def make_ctx(repo: Path, gh: FakeGitHub, now: datetime = NOW, **cfg) -> RecordingContext:
    config = RelayConfig(project="demo", repo="o/r", allowed=frozenset({"me"}), forbid=("src/secret/**",), **cfg)
    return RecordingContext(cfg=config, github=gh, now=now, workdir=repo)


def fake_verify(result: str):
    return lambda cmd, cwd, timeout: result


# ── checkout ─────────────────────────────────────────────────────────────────


def test_checkout_first_session_writes_handoff_and_session_files(repo):
    gh = FakeGitHub([make_task()])
    summary = commands.checkout(make_ctx(repo, gh), ISSUE)
    assert summary.startswith(f"relay #{ISSUE} s1/5: Checkout (erste Session)")
    h = Workspace(repo, ISSUE).read_handoff()
    assert (h.session, h.last_session, h.verify_sha256) == (1, NOW, Brief("t", BODY).verify_sha256)
    session_md = (repo / SESSION_DIR / "SESSION.md").read_text()
    assert "2026-10-21T12:30:00Z" in session_md and "2026-10-21T12:20:00Z" in session_md
    assert "cat count.txt" in session_md and "`src/**`" in session_md
    assert "Batches of 100" in (repo / SESSION_DIR / "BRIEF.md").read_text()
    # Session-Dateien sind lokal ignoriert, das Relay-Verzeichnis nicht.
    status = git(repo, "status", "--porcelain", "--untracked-files=all")
    assert SESSION_DIR not in status and PRIVATE_DIR not in status and handoff_path(ISSUE) in status


def test_agent_facing_session_dir_holds_no_harness_state(repo):
    # Die Sandbox trägt SESSION_DIR zum Agenten und zurück: Plan (Scope, Verify) und Snapshot
    # dürfen dort nicht liegen, sonst könnte der Agent seinen eigenen Scope erweitern.
    commands.checkout(make_ctx(repo, FakeGitHub([make_task()])), ISSUE)
    agent_side = sorted(p.name for p in (repo / SESSION_DIR).iterdir())
    assert agent_side == [HARNESS_LOCAL_FILE, "BRIEF.md", "SESSION.md"]
    assert (repo / SESSION_DIR / HARNESS_LOCAL_FILE).read_text() == "audit/work/\n"
    assert {p.name for p in (repo / PRIVATE_DIR).iterdir()} == {"plan.json", "HANDOFF.before.md"}


def test_checkout_session_minutes_are_capped(repo):
    commands.checkout(make_ctx(repo, FakeGitHub([make_task()]), max_session_minutes=10), ISSUE)
    assert "2026-10-21T12:10:00Z" in (repo / SESSION_DIR / "SESSION.md").read_text()


def test_checkout_refuses_unauthorized_task(repo):
    gh = FakeGitHub([make_task(label_actor="mallory")])
    with pytest.raises(commands.RelayError, match="mallory"):
        commands.checkout(make_ctx(repo, gh), ISSUE)


def test_checkout_resumes_blocked_handoff_and_repins_verify(repo):
    ws = Workspace(repo, ISSUE)
    h = Handoff.new(ISSUE, "Import", "old-pin")
    h.session = 2
    h.block("checkpoint")
    ws.write_handoff(h)
    gh = FakeGitHub([make_task()])
    gh.states[ISSUE] = StatusState(verify_pending=Brief("t", BODY).verify_sha256)
    summary = commands.checkout(make_ctx(repo, gh), ISSUE)
    assert "neue `## Verify`-Fassung übernommen" in summary and "fortgesetzt nach `blocked` (Label entfernt)" in summary
    h2 = ws.read_handoff()
    assert (h2.status, h2.blocked_reason, h2.session) == (Status.ACTIVE, "", 3)
    assert h2.verify_sha256 == Brief("t", BODY).verify_sha256


# ── checkin ──────────────────────────────────────────────────────────────────


def checked_out(repo: Path, gh: FakeGitHub | None = None, **cfg) -> tuple[RecordingContext, Workspace]:
    ctx = make_ctx(repo, gh or FakeGitHub([make_task()]), **cfg)
    commands.checkout(ctx, ISSUE)
    return ctx, Workspace(repo, ISSUE)


def agent_writes(ws: Workspace, progress: str = "", status: Status = Status.ACTIVE, log: bool = True) -> None:
    h = not_none(ws.read_handoff())
    h.progress, h.status = progress, status
    ws.write_handoff(h)
    if log:
        ws.session_log(h.session).write_text("# s\nran scripts/s1-01.sql\n")


def test_checkin_records_progress_and_log_line(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="100/300")
    summary = commands.checkin(ctx, ISSUE, verify=fake_verify("100/300 rows"))
    assert summary == f"relay #{ISSUE} s1: active"
    h = not_none(ws.read_handoff())
    assert (h.verify_result, h.stalled) == ("100/300 rows", 0)
    assert "s1 · 2026-10-21T12:00:00Z · active · progress: 100/300 · verify: 100/300 rows" in h.section("log")


def test_checkin_resets_harness_fields_and_writes_missing_session_log(repo):
    ctx, ws = checked_out(repo)
    h = not_none(ws.read_handoff())
    h.session, h.verify_sha256, h.progress = 42, "forged", "1"
    ws.write_handoff(h)
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert (h.session, h.verify_sha256) == (1, Brief("t", BODY).verify_sha256)
    assert "Harness-Felder zurückgesetzt: session, verify_sha256" in h.section("log")
    assert "No session log written" in ws.session_log(1).read_text()


def test_checkin_restores_broken_handoff_and_blocks(repo):
    ctx, ws = checked_out(repo)
    ws.handoff_file.write_text("garbage")
    calls: list = []

    def verify(cmd, cwd, timeout):
        calls.append(cmd)
        return "x"

    summary = commands.checkin(ctx, ISSUE, verify=verify)
    assert "blocked" in summary and "Handoff nach der Session ungültig" in summary
    assert ws.read_handoff().session == 1
    assert calls == []  # ungültiger Handoff → Verify (teuer, Seiteneffekte) entfällt


def test_checkin_blocks_after_two_sessions_without_progress(repo):
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws)
    commands.checkin(ctx, ISSUE, verify=fake_verify("same"))
    assert ws.read_handoff().stalled == 0  # "" → "same" ist Fortschritt
    for expected in (1, 2):
        commands.checkout(make_ctx(repo, gh), ISSUE)
        agent_writes(ws)
        commands.checkin(ctx, ISSUE, verify=fake_verify("same"))
        assert ws.read_handoff().stalled == expected
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and "kein messbarer Fortschritt in 2 Sessions" in h.blocked_reason


def test_checkin_uses_progress_when_brief_has_no_verify_command(repo):
    body = BODY.replace("```verify\ncat count.txt\n```\n", "")
    ctx, ws = checked_out(repo, FakeGitHub([make_task(body=body)]))
    agent_writes(ws, progress="5/9")
    commands.checkin(ctx, ISSUE, verify=fake_verify("unused"))
    h = not_none(ws.read_handoff())
    assert (h.verify_result, h.stalled) == ("", 0)


def test_checkin_done_is_not_stalled(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, status=Status.DONE)
    commands.checkin(ctx, ISSUE, verify=fake_verify(""))
    assert ws.read_handoff().status is Status.DONE


def test_checkin_blocks_on_out_of_scope_and_forbidden_changes(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    (repo / "src" / "secret").mkdir(parents=True)
    (repo / "src" / "ok.py").write_text("x")
    (repo / "src" / "secret" / "k.py").write_text("x")
    (repo / "README.md").write_text("changed")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED
    assert "README.md" in h.blocked_reason and "src/secret/k.py" in h.blocked_reason
    assert "src/ok.py" not in h.blocked_reason
    # Ausgeblendet: verworfen, damit nach dem Entblocken nicht dieselben Pfade blockieren.
    assert (repo / "README.md").read_text() == "x\n"
    assert not (repo / "src" / "secret" / "k.py").exists()
    assert (repo / "src" / "ok.py").read_text() == "x"  # in Scope bleibt


def test_checkin_ignores_untracked_agent_tool_state(repo):
    # Der Serena-MCP-Server legt `.serena/` im Arbeitsverzeichnis an — das ist kein Agent-Diff
    # und darf die Session nicht blockieren (#312 s1).
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text("x")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    assert ws.read_handoff().status is Status.ACTIVE
    assert ".serena/project.yml" not in ws.changed_paths()


def test_checkin_still_sees_tracked_files_in_agent_tool_dirs(repo):
    # Nur ungetrackter Werkzeug-Zustand ist ausgeblendet: eine getrackte Datei dort bleibt ein Diff.
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text("x")
    git(repo, "add", ".serena/project.yml")
    git(repo, "commit", "-qm", "track serena")
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    (repo / ".serena" / "project.yml").write_text("changed")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = ws.read_handoff()
    assert h.status is Status.BLOCKED and ".serena/project.yml" in h.blocked_reason


def test_checkin_blocks_when_last_allowed_session_ends_active(repo):
    gh = FakeGitHub([make_task(body=BODY.replace("max_sessions: 5", "max_sessions: 1"))])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="1")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    assert "max_sessions (1) erreicht" in ws.read_handoff().blocked_reason


def test_checkin_without_checkout_fails_clearly(repo):
    with pytest.raises(RuntimeError, match="plan.json fehlt"):
        commands.checkin(make_ctx(repo, FakeGitHub([make_task()])), ISSUE)


# ── publish ──────────────────────────────────────────────────────────────────


def test_publish_commits_in_scope_only_and_updates_github(repo):
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="100/300")
    (repo / "src").mkdir()
    (repo / "src" / "imp.py").write_text("x")
    commands.checkin(ctx, ISSUE, verify=fake_verify("100/300"))
    summary = commands.publish(ctx, ISSUE)
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "src/imp.py" in committed and handoff_path(ISSUE) in committed
    assert not any(p.startswith(SESSION_DIR) for p in committed)
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    assert summary == f"relay #{ISSUE} s1/5 active · progress: 100/300 · verify: 100/300 · {sha}"
    assert ctx.pushed and gh.states[ISSUE].last_commit == sha
    assert gh.label_calls[-1] == (ISSUE, set(), {LABELS.blocked, LABELS.done})
    assert "100/300" in gh.comments[ISSUE]


@pytest.mark.parametrize(("st", "label"), [(Status.BLOCKED, LABELS.blocked), (Status.DONE, LABELS.done)])
def test_publish_mirrors_status_as_label(repo, st, label):
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, status=st)
    commands.checkin(ctx, ISSUE, verify=fake_verify("x"))
    assert commands.publish(ctx, ISSUE).verdict == str(st)
    assert label in gh.task(ISSUE, "").labels


def test_publish_leaves_out_of_scope_changes_uncommitted(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    (repo / "README.md").write_text("changed")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    commands.publish(ctx, ISSUE)
    assert "README.md" not in git(repo, "show", "--name-only", "--format=", "HEAD")


# ── Merge der Base / Agent-Git-Zustand (#1159 s3) ─────────────────────────────


def commit_on(repo: Path, branch: str, files: dict[str, str | None], message: str = "upstream") -> None:
    """Commit auf `branch` (eigener Worktree) pushen + fetchen; `None` löscht die Datei."""
    other = repo.parent / f"wt-{branch.replace('/', '-')}"
    if not other.exists():
        git(repo, "worktree", "add", "-q", "-B", branch, str(other), f"origin/{branch}" if branch == "main" else "HEAD")
    for rel, text in files.items():
        if text is None:
            git(other, "rm", "-q", rel)
        else:
            (other / rel).parent.mkdir(parents=True, exist_ok=True)
            (other / rel).write_text(text)
            git(other, "add", rel)
    git(other, "commit", "-qm", message)
    git(other, "push", "-q", "origin", f"HEAD:{branch}")
    git(repo, "fetch", "-q", "origin")


@pytest.fixture
def behind_main(repo: Path) -> Path:
    """Branch mit `old.md`, danach ändert main README, legt `new.md` an und löscht `old.md`."""
    commit_on(repo, "main", {"old.md": "old\n"})
    git(repo, "merge", "-q", "--ff-only", "origin/main")
    commit_on(repo, "main", {"README.md": "main\n", "new.md": "new\n", "old.md": None})
    return repo


def parents(repo: Path) -> list[str]:
    return git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]


def test_merge_of_base_is_not_out_of_scope_and_commits_complete(behind_main):
    repo = behind_main
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    assert ws.read_handoff().status is Status.ACTIVE
    commands.publish(ctx, ISSUE)
    assert parents(repo)[1] == git(repo, "rev-parse", "origin/main").strip()
    # Nichts von main zurückgedreht: der Branch weicht von main nur im Scope ab.
    diff = git(repo, "diff", "--name-only", "origin/main", "HEAD").split()
    assert all(p.startswith(("src/", ".fabrikant/")) for p in diff), diff


def test_out_of_scope_edit_during_merge_falls_back_to_base_version(behind_main):
    repo = behind_main
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
    (repo / "README.md").write_text("hand edit\n")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and h.blocked_reason.endswith("README.md")
    assert "new.md" not in h.blocked_reason  # kam unverändert aus main
    commands.publish(ctx, ISSUE)
    assert len(parents(repo)) == 2
    assert git(repo, "show", "HEAD:README.md") == "main\n"  # Stand von main, nicht der alte Branch-Stand
    assert git(repo, "ls-tree", "--name-only", "HEAD", "new.md", "old.md").split() == ["new.md"]


def test_merge_take_ours_of_base_file_is_out_of_scope_and_falls_back_to_base(behind_main):
    """Agent stellt eine von main geänderte Datei auf den alten Branch-Stand zurück ("take ours"):
    unsichtbar für `diff HEAD`, muss trotzdem blockieren — sonst dreht der Merge-Commit main zurück."""
    repo = behind_main
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "origin/main")
    git(repo, "checkout", "-q", "HEAD", "--", "README.md")  # "take ours": alter Branch-Stand
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and h.blocked_reason.endswith("README.md")
    assert "new.md" not in h.blocked_reason  # kam unverändert aus main
    commands.publish(ctx, ISSUE)
    assert len(parents(repo)) == 2
    assert git(repo, "show", "HEAD:README.md") == "main\n"  # Base-Stand überlebt den Merge-Commit


def test_merge_of_foreign_branch_is_dropped_and_its_files_reverted(repo):
    commit_on(repo, "other", {"README.md": "other\n", "extra.md": "x\n"})
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "origin/other")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and "extra.md" in h.blocked_reason
    assert "kein Stand von origin/main" in h.section("log")
    commands.publish(ctx, ISSUE)
    assert len(parents(repo)) == 1
    assert git(repo, "ls-tree", "--name-only", "HEAD", "extra.md") == ""
    assert git(repo, "show", "HEAD:README.md") == "x\n"


def test_agent_commit_does_not_bypass_scope_check(repo):
    ctx, ws = checked_out(repo)
    start = ws.head_sha()
    agent_writes(ws, progress="1")
    (repo / "README.md").write_text("sneaky\n")
    git(repo, "commit", "-qam", "agent")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and "README.md" in h.blocked_reason
    assert "Agent-Commits" in h.section("log")
    commands.publish(ctx, ISSUE)
    assert parents(repo) == [start]
    assert git(repo, "show", "HEAD:README.md") == "x\n"


def test_agent_commit_with_in_progress_merge_is_settled_and_blocked(repo):
    """Agent committet UND lässt einen Base-Merge laufen: Merge wird verworfen, der Commit in den
    Worktree zurückgeholt, die fremden Dateien blockieren."""
    commit_on(repo, "main", {"README.md": "main\n", "new.md": "new\n"})
    ctx, ws = checked_out(repo)
    start = ws.head_sha()
    agent_writes(ws, progress="1")
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x")
    git(repo, "add", "src/ok.py")
    git(repo, "commit", "-qm", "agent")
    git(repo, "merge", "-q", "--no-commit", "origin/main")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.status is Status.BLOCKED and "README.md" in h.blocked_reason and "new.md" in h.blocked_reason
    assert "Agent-Commits" in h.section("log")  # Merge lief mit dem Soft-Reset unter
    commands.publish(ctx, ISSUE)
    assert parents(repo) == [start]  # kein Merge-Commit, Agent-Commit gefaltet
    assert git(repo, "show", "HEAD:README.md") == "x\n"
    assert git(repo, "ls-tree", "--name-only", "HEAD", "new.md") == ""
    assert git(repo, "show", "HEAD:src/ok.py") == "x"


def test_staged_new_out_of_scope_file_is_unstaged_and_not_committed(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    (repo / "stray.md").write_text("x")
    git(repo, "add", "stray.md")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    assert not (repo / "stray.md").exists()
    commands.publish(ctx, ISSUE)
    assert git(repo, "ls-tree", "--name-only", "HEAD", "stray.md") == ""


def test_publish_does_not_commit_prestaged_paths_outside_the_given_ones(repo):
    ws = Workspace(repo, ISSUE)
    (repo / "a.md").write_text("a")
    (repo / "b.md").write_text("b")
    git(repo, "add", "a.md", "b.md")
    ws.commit(["a.md"], "only a", ("bot", "bot@example.com"))
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.md"]


def test_scope_block_reason_counts_paths_beyond_the_listed_ones(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    for i in range(commands.SCOPE_REASON_PATHS + 2):
        (repo / f"f{i}.md").write_text("x")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    assert ws.read_handoff().blocked_reason.endswith("(+2 weitere)")


def test_push_refuses_other_branches(repo):
    git(repo, "checkout", "-q", "main")
    with pytest.raises(RuntimeError, match="Push verweigert"):
        Workspace(repo, ISSUE).push("o/r", "tok")


# ── Audit ────────────────────────────────────────────────────────────────────


def finished(repo: Path) -> FakeGitHub:
    """Eine committete, abgeschlossene Session auf dem Branch."""
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="300/300", status=Status.DONE)
    h = not_none(ws.read_handoff())
    h.body = h.body.replace("_Targets, source snapshot/watermark, backup location — for the auditor._", "db=staging")
    ws.write_handoff(h)
    (ws.relay / "scripts" / "s1-01.sql").write_text("insert …")
    commands.checkin(ctx, ISSUE, verify=fake_verify("300/300"))
    commands.publish(ctx, ISSUE)
    return gh


def test_audit_prepare_hides_branch_files_and_reveal_restores(repo):
    gh = finished(repo)
    ctx = make_ctx(repo, gh)
    summary = commands.audit_prepare(ctx, ISSUE)
    script = repo / ".fabrikant" / "relay" / str(ISSUE) / "scripts" / "s1-01.sql"
    assert not script.exists() and "ausgeblendet" in summary
    brief = (repo / SESSION_DIR / "AUDIT_BRIEF.md").read_text()
    assert "count matches" in brief and "db=staging" in brief
    assert (repo / SESSION_DIR / "audit" / "blind").is_dir()
    commands.audit_reveal(ctx, ISSUE)
    assert script.read_text() == "insert …"


def test_audit_prepare_restores_deleted_files_and_reveal_removes_them_again(repo):
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="300/300", status=Status.DONE)
    h = not_none(ws.read_handoff())
    h.body = h.body.replace("_Targets, source snapshot/watermark, backup location — for the auditor._", "db=staging")
    ws.write_handoff(h)
    commands.checkin(ctx, ISSUE, verify=fake_verify("300/300"))
    commands.publish(ctx, ISSUE)
    git(repo, "rm", "-q", "README.md")
    git(repo, "commit", "-q", "-m", "delete readme")
    readme = repo / "README.md"
    assert not readme.exists()
    commands.audit_prepare(ctx, ISSUE)
    assert readme.exists()  # Löschung ist in der Blind-Phase nicht sichtbar
    commands.audit_reveal(ctx, ISSUE)
    assert not readme.exists() and git(repo, "status", "--porcelain", "README.md").strip() == ""


def test_audit_prepare_requires_done_label(repo):
    gh = finished(repo)
    gh.sync_labels(ISSUE, gh.task(ISSUE, "").labels, set(), {LABELS.done})
    with pytest.raises(commands.RelayError, match="kein Audit"):
        commands.audit_prepare(make_ctx(repo, gh), ISSUE)


def run_audit(repo: Path, report: str | None) -> tuple[FakeGitHub, str]:
    gh = finished(repo)
    ctx = make_ctx(repo, gh)
    commands.audit_prepare(ctx, ISSUE)
    blind = repo / SESSION_DIR / "audit" / "blind"
    (blind / "01-count.sql").write_text("select count(*)")
    commands.audit_reveal(ctx, ISSUE)
    if report is not None:
        (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text(report)
    return gh, commands.audit_publish(ctx, ISSUE)


def test_audit_publish_pass_commits_report_and_labels_audited(repo):
    gh, summary = run_audit(repo, "ok\n<!-- relay-audit verdict=pass -->\n")
    assert summary.startswith(f"relay #{ISSUE} audit: pass")
    assert isinstance(summary, commands.StepSummary) and summary.verdict == "pass"
    committed = git(repo, "show", "--name-only", "--format=", "HEAD")
    assert f".fabrikant/relay/{ISSUE}/AUDIT.md" in committed and "audit/blind/01-count.sql" in committed
    assert {LABELS.done, LABELS.audited} <= gh.task(ISSUE, "").labels
    assert gh.states[ISSUE].audit == "pass"


def test_audit_publish_fail_or_missing_report_reopens_as_blocked(repo):
    gh, summary = run_audit(repo, None)
    assert "fail (kein Report)" in summary
    assert summary.verdict == "fail"  # type: ignore[attr-defined]
    labels = gh.task(ISSUE, "").labels
    assert LABELS.blocked in labels and LABELS.done not in labels
    assert Workspace(repo, ISSUE).read_handoff().status is Status.BLOCKED


def test_audit_publish_ignores_auditor_edits_outside_the_report(repo):
    gh = finished(repo)
    ctx = make_ctx(repo, gh)
    commands.audit_prepare(ctx, ISSUE)
    commands.audit_reveal(ctx, ISSUE)
    ws = Workspace(repo, ISSUE)
    session_before = ws.read_handoff().session
    forged = not_none(ws.read_handoff())
    forged.session, forged.status = 99, Status.ACTIVE
    ws.write_handoff(forged)  # Prüfer fälscht den Handoff …
    (ws.relay / "scripts" / "evil.sql").write_text("drop table x")  # … und legt ein Skript ab
    (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text("ok\n<!-- relay-audit verdict=pass -->\n")
    commands.audit_publish(ctx, ISSUE)
    h = not_none(ws.read_handoff())
    assert (h.session, h.status) == (session_before, Status.DONE)
    assert not (ws.relay / "scripts" / "evil.sql").exists()
    assert "evil.sql" not in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_audit_publish_drops_in_progress_merge(repo):
    """Prüfer lässt einen Merge im Worktree: wird verworfen, sonst committete `commit()`
    den ganzen Merge als "audit"-Commit."""
    commit_on(repo, "main", {"README.md": "main\n"})
    gh = finished(repo)
    ctx = make_ctx(repo, gh)
    commands.audit_prepare(ctx, ISSUE)
    commands.audit_reveal(ctx, ISSUE)
    git(repo, "merge", "-q", "--no-commit", "origin/main")
    (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text("ok\n<!-- relay-audit verdict=pass -->\n")
    commands.audit_publish(ctx, ISSUE)
    assert len(parents(repo)) == 1  # kein Merge-Commit
    assert git(repo, "show", "HEAD:README.md") == "x\n"  # Base-Änderung nicht mitcommittet


def test_audit_publish_commits_only_top_level_evidence(repo, monkeypatch):
    """Arbeitskopien des Prüfers (ganze Trees, #1159/#312: 1 275 bzw. 2 552 Dateien) gehören nicht
    in den PR: nur Dateien direkt in `blind/` bis zur Größengrenze, der Rest steht in SKIPPED.md."""
    monkeypatch.setattr(workspace_mod, "MAX_EVIDENCE_FILE_BYTES", 10)
    gh = finished(repo)
    ctx = make_ctx(repo, gh)
    commands.audit_prepare(ctx, ISSUE)
    blind = repo / SESSION_DIR / "audit" / "blind"
    (blind / "01-count.sql").write_text("select 1")
    (blind / "02-dump.out").write_text("x" * 11)
    (blind / "tree-main" / "src").mkdir(parents=True)
    (blind / "tree-main" / "src" / "app.py").write_text("print()")
    commands.audit_reveal(ctx, ISSUE)
    (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text("ok\n<!-- relay-audit verdict=pass -->\n")
    commands.audit_publish(ctx, ISSUE)
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    rel = f".fabrikant/relay/{ISSUE}/audit/blind"
    assert f"{rel}/01-count.sql" in committed and f"{rel}/{workspace_mod.SKIPPED_EVIDENCE_FILE}" in committed
    assert not [c for c in committed if "tree-main" in c or "02-dump" in c]
    skipped = (repo / rel / workspace_mod.SKIPPED_EVIDENCE_FILE).read_text()
    assert "`tree-main/`" in skipped and "`02-dump.out`: 11 B" in skipped


def test_copy_evidence_without_skips_writes_no_manifest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "01.sql").write_text("q")
    assert workspace_mod.copy_evidence(tmp_path / "src", tmp_path / "dest") == []
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["01.sql"]


def test_copy_evidence_skips_symlinks(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "secret").write_text("s")
    (tmp_path / "src" / "link").symlink_to(tmp_path / "secret")
    assert workspace_mod.copy_evidence(tmp_path / "src", tmp_path / "dest") == ["`link`: Symlink"]


def test_copy_evidence_skips_non_regular_files(tmp_path):
    """FIFOs & Co. dürfen den Publish-Step nicht crashen (shutil.copy2 wirft SpecialFileError)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "01.sql").write_text("q")
    os.mkfifo(src / "pipe")
    assert workspace_mod.copy_evidence(src, tmp_path / "dest") == ["`pipe`: keine reguläre Datei"]
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["01.sql", workspace_mod.SKIPPED_EVIDENCE_FILE]


def test_copy_evidence_replaces_stale_manifest(tmp_path):
    """Neuer Lauf ohne Skips entfernt das Manifest des vorherigen Laufs."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "sub").mkdir()
    (src / "01.sql").write_text("q")
    workspace_mod.copy_evidence(src, dest)
    (src / "sub").rmdir()
    assert workspace_mod.copy_evidence(src, dest) == []
    assert not (dest / workspace_mod.SKIPPED_EVIDENCE_FILE).exists()


def test_copy_evidence_skips_auditor_manifest_name(tmp_path):
    """Ein vom Prüfer angelegtes SKIPPED.md wird nicht kopiert, sondern durch die Liste ersetzt."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / workspace_mod.SKIPPED_EVIDENCE_FILE).write_text("auditor")
    (src / "big.bin").write_bytes(b"x" * 2_000_000)
    skipped = workspace_mod.copy_evidence(src, dest)
    assert any(workspace_mod.SKIPPED_EVIDENCE_FILE in s for s in skipped)
    assert "auditor" not in (dest / workspace_mod.SKIPPED_EVIDENCE_FILE).read_text()


LINT_REL = f".fabrikant/relay/{ISSUE}/{workspace_mod.LINT_CONFIG_FILE}"


def test_session_commits_lint_exclusion_for_relay_dir(repo):
    """Relay-Skripte/Evidenz dürfen den Lint des Zielrepos nicht brechen (#1160/#313: ruff auf audit/blind/*.py)."""
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    commands.publish(ctx, ISSUE)
    assert LINT_REL in git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert (repo / LINT_REL).read_text() == workspace_mod.LINT_CONFIG


def test_prepare_keeps_existing_lint_config(repo):
    ws = Workspace(repo, ISSUE)
    ws.relay.mkdir(parents=True)
    (repo / LINT_REL).write_text("# team-owned\n")
    ws.prepare()
    assert (repo / LINT_REL).read_text() == "# team-owned\n"


def test_audit_adds_lint_exclusion_to_tasks_from_before(repo):
    gh = finished(repo)
    git(repo, "rm", "-q", LINT_REL)
    git(repo, "commit", "-qm", "task from before the lint exclusion")
    ctx = make_ctx(repo, gh)
    commands.audit_prepare(ctx, ISSUE)
    commands.audit_reveal(ctx, ISSUE)
    (repo / SESSION_DIR / "audit" / "AUDIT.md").write_text("ok\n<!-- relay-audit verdict=pass -->\n")
    commands.audit_publish(ctx, ISSUE)
    assert LINT_REL in git(repo, "show", "--name-only", "--format=", "HEAD").split()


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff nicht installiert")
def test_lint_config_takes_relay_dir_out_of_ruff(tmp_path):
    """Echte ruff-Semantik: verschachtelte Config schließt das Relay-Verzeichnis aus, Rest bleibt geprüft."""
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["E", "S"]\n')
    relay = tmp_path / ".fabrikant" / "relay" / "1" / "audit" / "blind"
    relay.mkdir(parents=True)
    (relay / "08-sample.py").write_text("import os, random\nx = random.Random(1); y = 2\n")
    (tmp_path / ".fabrikant" / "relay" / "1" / workspace_mod.LINT_CONFIG_FILE).write_text(workspace_mod.LINT_CONFIG)
    (tmp_path / "app.py").write_text("import os, sys\nx = 1; y = 2\n")  # Lint- und Format-Befunde
    for cmd in (["check", "--no-cache", "."], ["format", "--check", "--no-cache", "."]):
        out = subprocess.run(["ruff", *cmd], cwd=tmp_path, capture_output=True, text=True)
        report = out.stdout + out.stderr
        assert out.returncode == 1 and "app.py" in report, (cmd, report)  # Produktcode wird weiter geprüft
        assert ".fabrikant" not in report, (cmd, report)


def test_parse_verdict_takes_last_marker():
    assert commands.parse_verdict("<!-- relay-audit verdict=pass -->\n<!-- relay-audit verdict=fail -->") == "fail"
    assert commands.parse_verdict("x\n<!--relay-audit verdict=pass-->") == "pass"
    assert commands.parse_verdict(None) == "fail"


# ── Dispatch ─────────────────────────────────────────────────────────────────


def remote_handoff(gh: FakeGitHub, issue: int, **kw) -> None:
    h = Handoff.new(issue, "t", Brief("t", BODY).verify_sha256)
    for k, v in kw.items():
        setattr(h, k, v)
    gh.files[f"fabrikant/relay/{issue}:{handoff_path(issue)}"] = h.render()


def fake_history(monkeypatch, records: list[dict]) -> None:
    """Run-Historie aus rohen Records (wie im Store) — typisiert über den echten Parser."""
    monkeypatch.setattr(commands.runs, "load_runs", lambda project: runs.parse_runs(records, project))


@pytest.fixture
def no_runs(monkeypatch):
    fake_history(monkeypatch, [])


def starter(calls):
    def start(project, loop, issue, note):
        calls.append((project, loop, issue, note))
        return "run-1"

    return start


def test_dispatch_starts_longest_waiting_session(repo, no_runs):
    gh = FakeGitHub([make_task(1), make_task(2), make_task(3)])
    remote_handoff(gh, 1, session=4, last_session=NOW - timedelta(hours=1))
    remote_handoff(gh, 2, session=2, last_session=NOW - timedelta(hours=5))
    remote_handoff(gh, 3, session=1, last_session=NOW - timedelta(hours=9), not_before=NOW + timedelta(hours=1))
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls == [("demo", LOOP_SESSION, 2, "relay #2 s3")]
    assert out.splitlines()[-1] == "relay #2 s3: gestartet (relay-session, run-1)"
    assert "#3: skip — nicht vor" in out


def test_dispatch_prefers_new_tasks_and_starts_audits(repo, no_runs):
    gh = FakeGitHub([make_task(1, labels=(LABELS.task, LABELS.done)), make_task(2)])
    remote_handoff(gh, 1, session=3, last_session=NOW - timedelta(days=1))
    calls: list = []
    commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls[0][1:] == (LOOP_SESSION, 2, "relay #2 s1")  # kein Handoff = wartet am längsten
    gh2 = FakeGitHub([make_task(1, labels=(LABELS.task, LABELS.done))])
    remote_handoff(gh2, 1, session=3)
    commands.dispatch(make_ctx(repo, gh2), start=starter(calls))
    assert calls[1][1:] == (LOOP_AUDIT, 1, "relay #1 audit")


def test_dispatch_applies_blocks_and_notes_without_starting(repo, no_runs):
    gh = FakeGitHub([make_task(1, body="## Goal\nx"), make_task(2, label_actor="mallory"), make_task(3)])
    remote_handoff(gh, 3, verify_sha256="old-pin")
    gh.files[f"fabrikant/relay/4:{handoff_path(4)}"] = "broken"
    gh._tasks[4] = make_task(4)
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls == [] and out.endswith("relay: nichts fällig")
    for blocked in (1, 3, 4):
        assert LABELS.blocked in gh.task(blocked, "").labels
    assert gh.states[3].verify_pending == Brief("t", BODY).verify_sha256
    assert "mallory" in gh.states[2].note and LABELS.blocked not in gh.task(2, "").labels
    assert "Handoff auf dem Branch ungültig" in gh.states[4].note
    # Zweiter Tick: Notiz unverändert → kein erneutes Schreiben.
    gh.comments.clear()
    commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert 2 not in gh.comments


def test_dispatch_isolates_transient_github_error_per_task(repo, no_runs):
    """Ein GitHubError beim Handoff-Lesen blockiert nur den betroffenen Task —
    die übrigen Tasks im Tick werden weiterverarbeitet (nächster Tick erneut)."""
    gh = FakeGitHub([make_task(1), make_task(2)])
    remote_handoff(gh, 1, session=1, last_session=NOW - timedelta(hours=2))
    remote_handoff(gh, 2, session=1, last_session=NOW - timedelta(hours=1))
    gh.file_errors[1] = GitHubError("502 Bad Gateway")
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls == [("demo", LOOP_SESSION, 2, "relay #2 s2")]
    assert "#1: skip — GitHub-Fehler beim Handoff-Lesen" in out
    assert "#2: session" in out
    assert 1 not in gh.comments  # kein Status-Kommentar für den übersprungenen Task


def test_dispatch_waits_while_relay_run_is_active(repo, monkeypatch):
    fake_history(
        monkeypatch,
        [
            {"id": "r1", "loop": LOOP_SESSION, "status": "running"},
            {"id": "r0", "loop": "other", "status": "running"},
        ],
    )
    out = commands.dispatch(make_ctx(repo, FakeGitHub([make_task()])), start=starter([]))
    assert out == "relay: läuft bereits — r1 (running)"


def test_dispatch_busy_guard_scans_full_history_not_recent_window(repo, monkeypatch):
    # >50 neuere Runs anderer Loops: der laufende Relay-Run fällt aus jedem
    # "neueste 50"-Fenster — der Guard muss ihn trotzdem sehen.
    records = [{"id": f"new-{i}", "loop": "develop", "status": "pass"} for i in range(60)]
    records.append({"id": "r1", "loop": LOOP_SESSION, "status": "running"})
    fake_history(monkeypatch, records)
    out = commands.dispatch(make_ctx(repo, FakeGitHub([make_task()])), start=starter([]))
    assert out == "relay: läuft bereits — r1 (running)"


# ── Helfer ───────────────────────────────────────────────────────────────────


def test_run_verify_success_failure_and_timeout(tmp_path):
    assert commands.run_verify("echo a; echo; echo 'b c'", tmp_path, 5) == "b c"
    assert commands.run_verify("true", tmp_path, 5) == "(no output)"
    assert commands.run_verify("echo out; echo boom >&2; exit 3", tmp_path, 5) == "exit 3: boom"
    assert commands.run_verify("sleep 2", tmp_path, 1) == "timeout after 1s"


def test_start_run_posts_vars_and_bearer(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
        return Resp(b'{"run_id": "20261021-x"}')

    monkeypatch.setenv("FABRIKANT_TOKEN", "secret")
    monkeypatch.setattr(commands.urllib.request, "urlopen", fake_urlopen)
    assert commands.start_run("demo", LOOP_SESSION, 7, "relay #7 s1") == "20261021-x"
    assert seen["url"].endswith("/api/runs") and seen["auth"] == "Bearer secret"
    assert seen["body"] == {
        "project": "demo",
        "loop": LOOP_SESSION,
        "note": "relay #7 s1",
        "vars": {"issue_number": "7"},
    }


def test_start_run_wraps_api_errors_in_relay_error(monkeypatch):
    import urllib.error

    def http_500(req, timeout):
        raise http_error(500, "u", "Internal Server Error", body=b'{"detail": "no loop"}')

    monkeypatch.setattr(commands.urllib.request, "urlopen", http_500)
    with pytest.raises(commands.RelayError, match="HTTP 500.*no loop") as err:
        commands.start_run("demo", LOOP_SESSION, 7, "x")
    assert err.value.failure.kind is FailureKind.TRANSIENT

    def no_network(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(commands.urllib.request, "urlopen", no_network)
    with pytest.raises(commands.RelayError, match="nicht erreichbar.*connection refused"):
        commands.start_run("demo", LOOP_SESSION, 7, "x")


def test_main_validates_issue_and_maps_errors(monkeypatch, capsys):
    cfg = RelayConfig(project="demo", repo="o/r", allowed=frozenset({"me"}), bot_login="my-builder-app")
    monkeypatch.setattr(relay_main, "load", lambda project: ({}, cfg))
    with pytest.raises(SystemExit, match="--issue"):
        relay_main.main(["checkout", "--project", "demo", "--issue", "{issue_number}"])

    def boom(ctx, issue):
        raise commands.RelayError("nope")

    monkeypatch.setitem(relay_main._ISSUE_COMMANDS, "checkout", boom)
    assert relay_main.main(["checkout", "--project", "demo", "--issue", "7"]) == 1
    assert "relay: nope" in capsys.readouterr().err

    def unauthorized(ctx, issue):
        raise http_error(401, "u", "Unauthorized", body=b"")

    monkeypatch.setitem(relay_main._ISSUE_COMMANDS, "checkout", unauthorized)
    assert relay_main.main(["checkout", "--project", "demo", "--issue", "7"]) == EX_CONFIG  # environment
    assert "401" in capsys.readouterr().err
    monkeypatch.setitem(
        relay_main._ISSUE_COMMANDS, "checkout", lambda ctx, issue: commands.StepSummary("relay #7 audit: fail", "fail")
    )
    assert relay_main.main(["checkout", "--project", "demo", "--issue", "7"]) == 0
    assert capsys.readouterr().out == "::fabrikant-verdict::fail\nrelay #7 audit: fail\n"  # Verdikt vor der Summary
    monkeypatch.setattr(commands, "dispatch", lambda ctx: "relay: nichts fällig")
    assert relay_main.main(["dispatch", "--project", "demo"]) == 0
    assert "nichts fällig" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (http_error(502, "u", "Bad Gateway", body=b""), EX_TEMPFAIL),
        (commands.urllib.error.URLError("dns"), EX_TEMPFAIL),
        (ConfigError("bot_login passt nicht"), EX_CONFIG),
        (commands.RelayError("api down", FailureKind.TRANSIENT), EX_TEMPFAIL),
        (KeyError("bug"), 1),
    ],
)
def test_main_reports_failure_class_as_exit_code(monkeypatch, capsys, exc, code):
    # ADR 0035: Exit-Code = Fehlerklasse, damit relay.retry (on: transient) überhaupt greift.
    cfg = RelayConfig(project="demo", repo="o/r", allowed=frozenset({"me"}), bot_login="my-builder-app")
    monkeypatch.setattr(relay_main, "load", lambda project: ({}, cfg))

    def boom(ctx):
        raise exc

    monkeypatch.setattr(commands, "dispatch", boom)
    assert relay_main.main(["dispatch", "--project", "demo"]) == code


def run_record(rid, issue, status="fail", published=False, loop=LOOP_SESSION, kind=None, finished=None):
    steps = [{"name": "session", "status": "fail"}, {"name": "publish", "status": "pass" if published else "fail"}]
    rec = {"id": rid, "loop": loop, "status": status, "vars": {"issue_number": str(issue)}, "steps": steps}
    if kind:
        rec["failure_kind"] = kind
    if finished:
        rec["finished"] = finished
    return rec


def test_recent_outcomes_groups_finished_relay_runs_by_issue(monkeypatch):
    records = [
        run_record("r5", 7, status="fail", published=True, kind="transient", finished="2026-10-21T10:00:00+00:00"),
        run_record("r4", 7, status="error"),
        run_record("r3", 7, status="running"),  # nicht beendet
        run_record("r2", 8, loop="develop"),  # kein Relay-Loop
        {"id": "r1", "loop": LOOP_SESSION, "status": "fail", "vars": {"issue_number": "{issue_number}"}},
        run_record("r0", 9, status="pass", published=True, loop=LOOP_AUDIT),
    ]
    fake_history(monkeypatch, records)
    got = commands.recent_outcomes(commands.relay_runs("demo"))
    assert got == {
        7: [
            commands.RunOutcome("r5", True, FailureKind.TRANSIENT, datetime(2026, 10, 21, 10, tzinfo=UTC)),
            commands.RunOutcome("r4", False, FailureKind.UNKNOWN),  # Altbestand ohne Klasse
        ],
        9: [commands.RunOutcome("r0", True, None)],
    }


def test_dispatch_blocks_task_after_unclassified_failure_until_unblocked(repo, monkeypatch):
    records = [run_record("20261021T000000Z-relay-session", ISSUE)]
    fake_history(monkeypatch, records)
    gh = FakeGitHub([make_task()])
    remote_handoff(gh, ISSUE, session=2)
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert calls == [] and "keine automatische Wiederholung" in out
    assert LABELS.blocked in gh.task(ISSUE, "").labels
    assert gh.states[ISSUE].failures_seen == "20261021T000000Z-relay-session"
    # Mensch entfernt das Label → quittiert, nächster Tick startet wieder.
    gh.sync_labels(ISSUE, gh.task(ISSUE, "").labels, set(), {LABELS.blocked})
    commands.dispatch(make_ctx(repo, gh), start=starter(calls))
    assert [c[1] for c in calls] == [LOOP_SESSION]


def test_dispatch_honors_retry_backoff(repo, monkeypatch):
    records = [
        run_record("20261021T115800Z-relay-session", ISSUE, kind="interrupted", finished="2026-10-21T11:58:00+00:00")
    ]
    fake_history(monkeypatch, records)
    gh = FakeGitHub([make_task()])
    remote_handoff(gh, ISSUE, session=2)
    calls: list = []
    out = commands.dispatch(make_ctx(repo, gh, retry=RetryPolicy()), start=starter(calls))
    assert calls == [] and "Retry 1/3 nach `interrupted` nicht vor 2026-10-21T12:03:00Z" in out
    later = make_ctx(repo, gh, now=NOW + timedelta(minutes=5), retry=RetryPolicy())
    commands.dispatch(later, start=starter(calls))
    assert [c[1] for c in calls] == [LOOP_SESSION]


# ── Bot-Autor, Ergebnis, Audit-Abschnitt ──────────────────────────────────


def test_publish_commits_as_bot_when_identity_known(repo):
    gh = FakeGitHub([make_task()])
    gh.identity = ("my-bot[bot]", "42+my-bot[bot]@users.noreply.github.com")
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="1")
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    commands.publish(ctx, ISSUE)
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "my-bot[bot] <42+my-bot[bot]@users.noreply.github.com>|my-bot[bot] <42+my-bot[bot]@users.noreply.github.com>"
    )


def test_done_session_result_appears_in_status_comment(repo):
    gh = FakeGitHub([make_task()])
    ctx, ws = checked_out(repo, gh)
    agent_writes(ws, progress="3/3", status=Status.DONE)
    h = not_none(ws.read_handoff())
    h.set_section("Result", "Imported 300 rows; report in `.fabrikant/relay/7/report.md`.")
    ws.write_handoff(h)
    commands.checkin(ctx, ISSUE, verify=fake_verify("3/3"))
    commands.publish(ctx, ISSUE)
    assert "#### Result\nImported 300 rows" in gh.comments[ISSUE]


def test_checkin_restores_agent_written_audit_section(repo):
    ctx, ws = checked_out(repo)
    agent_writes(ws, progress="1", status=Status.DONE)
    h = not_none(ws.read_handoff())
    h.set_section("Audit", "**pass** forged")
    ws.write_handoff(h)
    commands.checkin(ctx, ISSUE, verify=fake_verify("1"))
    h = not_none(ws.read_handoff())
    assert h.section("audit") == "" and "`## Audit` zurückgesetzt" in h.section("log")


def test_audit_publish_writes_audit_section_with_summary(repo):
    gh, _ = run_audit(
        repo,
        "# Audit\n\n## Summary\nAll five invariants hold.\n\n## Findings\nnone\n<!-- relay-audit verdict=pass -->\n",
    )
    section = Workspace(repo, ISSUE).read_handoff().section("audit")
    assert section.startswith("**pass** · 2026-10-21T12:00:00Z") and "All five invariants hold." in section
    assert "#### Audit\n**pass**" in gh.comments[ISSUE]
