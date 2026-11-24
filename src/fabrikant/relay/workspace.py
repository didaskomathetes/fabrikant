"""Relay files in the worktree: handoff, session files, scope, commit/push, blind phase.

Layout on the branch `fabrikant/relay/<N>`:
    .fabrikant/relay/<N>/HANDOFF.md        state + memory (committed)
    .fabrikant/relay/<N>/scripts/          every executed ad-hoc script / SQL (committed)
    .fabrikant/relay/<N>/sessions/s<k>.md  what session k executed + results (committed)
    .fabrikant/relay/<N>/AUDIT.md, audit/  audit report + blind evidence (committed, without subfolders)
    .fabrikant/relay/<N>/ruff.toml         excludes the directory from the target repo's ruff lint (committed)
    .fabrikant-session/                    brief, SESSION.md, audit work state (local, git-ignored;
                                        the sandbox carries it to the agent and back)
    .fabrikant-harness/                    plan, snapshot, check-in (local, git-ignored; never at the agent)
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config, gitops
from ..apicommit import ApiPusher
from ..failures import GitError
from ..i18n import t
from ..secrets import _redact
from .model import Handoff, HandoffError

SESSION_DIR = config.HARNESS_DIR
# Harness-internal state (scope, verify command, handoff snapshot, blind list) lives separately
# from the exchange directory: the sandbox carries SESSION_DIR to the agent and back — what lies there
# the agent can change.
PRIVATE_DIR = config.HARNESS_PRIVATE_DIR
# Stays with the agent, does not come back: working copies of the audit (clones, venvs).
HARNESS_LOCAL = "audit/work/\n"
# State that tools of the agent environment write into the working directory unasked (e.g. the
# Serena MCP server: `.serena/project.yml`). Ignored locally so it does not count as a change outside
# the scope and block the session. Untracked files only — tracked ones stay visible.
AGENT_TOOL_DIRS = (".serena",)
_PLAN_FILE = "plan.json"
_BEFORE_FILE = "HANDOFF.before.md"
_CHECKIN_FILE = "checkin.json"
_HIDDEN_FILE = "hidden.json"
AUDIT_REPORT = "AUDIT.md"
HANDOFF_FILE = "HANDOFF.md"
# Only evidence is published from the blind audit: files directly in `audit/blind/` (check scripts and
# raw outputs, `<nn>-<invariant>.<ext>`). Subfolders (checked-out trees, venvs) and large files
# stay local: they do not belong in the PR, and `commit_via: api` uploads every file individually.
MAX_EVIDENCE_FILE_BYTES = 1_000_000
SKIPPED_EVIDENCE_FILE = "SKIPPED.md"
# Relay artifacts (worker scripts, audit evidence) are a record, not product code: a nested
# ruff config excludes `.fabrikant/relay/<N>/` from the target repo's lint/format check without touching
# its config. The scripts stay byte-exact (their `.out` files are their output).
LINT_CONFIG_FILE = "ruff.toml"
LINT_CONFIG = (
    "# fabrikant relay: worker scripts and audit evidence are records of what ran, not product code.\n"
    "# Keep them byte-for-byte and out of the repo's lint/format checks.\n"
    'exclude = ["*"]\n'
)
GIT_TIMEOUT_S = 300
# Credential helper reads the token from the env — never in argv/URL (process list, logs).
# No hardcoded password: `$RELAY_PUSH_TOKEN` is an env variable; the git credential helper
# must literally echo `password=…` (hence NOSONAR).
_CRED_HELPER = '!f() { echo username=x-access-token; echo "password=$RELAY_PUSH_TOKEN"; }; f'  # NOSONAR


def branch_name(issue: int) -> str:
    return f"fabrikant/relay/{issue}"


def relay_dir(issue: int) -> str:
    return f".fabrikant/relay/{issue}"


def handoff_path(issue: int) -> str:
    return f"{relay_dir(issue)}/{HANDOFF_FILE}"


def matches(path: str, pattern: str) -> bool:
    """Path matches glob; `dir/` and `dir/**` match everything below."""
    stem = pattern.removesuffix("**").rstrip("/")
    if pattern.endswith(("/", "/**")):
        return path == stem or path.startswith(stem + "/")
    return fnmatch.fnmatchcase(path, pattern)


@dataclass(frozen=True)
class SessionPlan:
    """Recorded by `checkout`, read by `checkin`/`publish` (the agent does not change it)."""

    issue: int
    session: int
    max_sessions: int
    started: str
    soft_deadline: str
    hard_deadline: str
    verify_command: str | None
    scope: tuple[str, ...]
    forbid: tuple[str, ...]
    head: str = ""  # Commit at checkout — basis of the scope check ("" = plan before this field)

    @classmethod
    def from_dict(cls, data: dict) -> SessionPlan:
        return cls(**{**data, "scope": tuple(data["scope"]), "forbid": tuple(data["forbid"])})


@dataclass(frozen=True)
class CheckinResult:
    status: str
    notes: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()


class Workspace:
    """Relay view of a worktree for issue `issue`."""

    def __init__(self, root: Path, issue: int):
        self.root = Path(root)
        self.issue = issue
        self.relay = self.root / relay_dir(issue)
        self.session = self.root / SESSION_DIR
        self.private = self.root / PRIVATE_DIR

    # ── Git ──────────────────────────────────────────────────────────────────
    def git(self, *args: str, env: dict | None = None, check: bool = True) -> str:
        p = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env
        )
        if check and p.returncode != 0:
            output = _redact((p.stderr or p.stdout).strip())
            raise GitError(f"git {args[0]}: {output}", output)  # Netz → transient, Auth → environment
        return p.stdout

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head_sha(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def merge_head(self) -> str | None:
        """SHA of `MERGE_HEAD` while a merge (uncommitted) is in progress in the worktree."""
        return self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip() or None

    def _diff_names(self, rev: str, other: str | None = None) -> list[str]:
        """Changed paths: `rev` against worktree, or `rev` against `other` (two revisions)."""
        # `--no-renames`: a rename counts with both sides (otherwise the deletion would vanish).
        args = [rev, other] if other is not None else [rev]
        return [p for p in self.git("diff", "--name-only", "--no-renames", "-z", *args).split("\0") if p]

    def settle_session(self, start: str, base: str) -> list[str]:
        """Bring the git state after the agent session to what the scope check can evaluate.

        - The agent's own commits (HEAD ≠ `start`) are pulled back into the worktree by soft reset:
          committing is the harness's job, otherwise they would escape the scope check.
        - A running merge stays only if it merges in a state of `origin/<base>`; any
          other is forgotten (`merge --quit`), its changes then count as session changes.
        Returns notes for the log line."""
        notes: list[str] = []
        if start and self.head_sha() != start:
            if self.merge_head():
                self.git("merge", "--quit")  # Soft reset is not allowed in the middle of a merge
            self.git("reset", "-q", "--soft", start)
            notes.append(t("relay.workspace.agent_commits_pulled_back_into"))
        merge = self.merge_head()
        if merge and not self._is_ancestor(merge, f"origin/{base}"):
            self.git("merge", "--quit")
            notes.append(t("relay.workspace.merge_of_discarded_no_state", v1=merge[:8], base=base))
        return notes

    def _is_ancestor(self, rev: str, of: str) -> bool:
        p = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", rev, of], timeout=GIT_TIMEOUT_S)
        return p.returncode == 0

    def changed_paths(self) -> list[str]:
        """Paths changed + untracked by the session (repo-relative, without ignored).

        If a merge is running, a path counts only if it deviates from BOTH sides: what comes unchanged
        from `MERGE_HEAD` was brought by the merge, not the session. Exception: if the base
        changed a path relative to the merge base, the branch did not, and the worktree has the
        HEAD version, the session reverted the base change ("take ours") — this is
        invisible to `diff HEAD` but counts as a change."""
        head = self._diff_names("HEAD")
        if merge := self.merge_head():
            theirs = set(self._diff_names(merge))
            tracked = [p for p in head if p in theirs]
            base = self.git("merge-base", "HEAD", merge).strip()
            main_only = set(self._diff_names(base, merge)) - set(self._diff_names(base, "HEAD"))
            # Only paths whose worktree state is exactly the HEAD state (otherwise e.g. a file added by the
            # base that the merge correctly placed into the worktree).
            tracked += [p for p in main_only if p not in head]
        else:
            tracked = head
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return sorted({p for p in tracked + untracked if p})

    def split_changes(self, allowed: tuple[str, ...], forbid: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
        """Changed paths → (matches `allowed` and not `forbid`, rest)."""
        inside: list[str] = []
        outside: list[str] = []
        for path in self.changed_paths():
            ok = any(matches(path, p) for p in allowed) and not any(matches(path, p) for p in forbid)
            (inside if ok else outside).append(path)
        return inside, outside

    def partition(self, scope: tuple[str, ...], forbid: tuple[str, ...]) -> tuple[list[str], list[str]]:
        """(in scope, outside) of a session — relay directory + `scope`, minus `forbid`."""
        return self.split_changes((relay_dir(self.issue) + "/", *scope), forbid)

    def audit_paths(self) -> tuple[str, ...]:
        """What an audit may commit: report, blind evidence, handoff and lint config (both from the harness)."""
        rel = relay_dir(self.issue)
        return (handoff_path(self.issue), f"{rel}/{AUDIT_REPORT}", f"{rel}/audit/", f"{rel}/{LINT_CONFIG_FILE}")

    def commit(self, paths: list[str], message: str, author: tuple[str, str]) -> str:
        """Stage + commit `paths` (no-op without change); returns the short SHA of HEAD.

        Exactly `paths` is committed — pre-staged things outside are dropped from the index. Exception:
        if a merge is running, the entire merge state is committed (a merge commit with a partial state
        would silently revert the other side's missing changes); foreign things have been discarded
        by the check-in up to then. `author` = (name, email) for author and committer."""
        merging = self.merge_head() is not None
        if merging:
            self.git("add", "-A")
        else:
            self.git("reset", "-q")
            if paths:
                self.git("add", "-A", "--", *paths)
        if merging or self._staged():
            name, email = author
            self.git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def _staged(self) -> bool:
        p = subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--quiet"], timeout=GIT_TIMEOUT_S)
        return p.returncode == 1

    def push(self, repo: str, token: str) -> None:
        """HEAD → `fabrikant/relay/<N>` as builder app; any other branch is refused.

        Project with `commit_via: api`: GitHub-signed API commits instead of `git push`."""
        branch = branch_name(self.issue)
        if self.current_branch() != branch:
            raise RuntimeError(
                t("relay.workspace.push_refused_worktree_is_on", v1=self.current_branch(), branch=branch)
            )
        if gitops.commit_via() == gitops.COMMIT_VIA_API:

            def fetch(name: str) -> None:
                self.fetch(repo, token, gitops.tracking_refspec(name))

            ApiPusher(self.root, repo, token, fetch=fetch).push(branch)
            return
        self._authed(repo, token, "push", f"HEAD:refs/heads/{branch}")

    def fetch(self, repo: str, token: str, refspec: str) -> None:
        """Fetch `refspec` from the GitHub repo (authenticated like the push)."""
        self._authed(repo, token, "fetch", refspec)

    def _authed(self, repo: str, token: str, command: str, *args: str) -> None:
        """`git <command> https://github.com/<repo>.git <args>` with the token via credential helper."""
        env = {**os.environ, "RELAY_PUSH_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}
        self.git(
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={_CRED_HELPER}",
            command,
            f"https://github.com/{repo}.git",
            *args,
            env=env,
        )

    def branch_changes(self, base: str) -> list[str]:
        """Paths the relay branch added/changed/deleted relative to `origin/<base>`.

        `--no-renames`: a rename counts as deletion + addition, both sides are visible."""
        out = self.git("diff", "--name-only", "--no-renames", "--diff-filter=AMD", "-z", f"origin/{base}...HEAD")
        return sorted(p for p in out.split("\0") if p)

    def revert(self, paths: list[str]) -> None:
        """Discard changes to `paths` (index + worktree).

        The target is HEAD; during a merge `MERGE_HEAD`: the branch never changes paths outside the scope,
        so their correct merge result is the state of the merged-in side.
        Paths that do not exist in the target are removed from the index and deleted."""
        target = self.merge_head() or "HEAD"
        gone = [p for p in paths if not self._in_tree(target, p)]
        if gone:
            self.git("rm", "-q", "-r", "-f", "--cached", "--ignore-unmatch", "--", *gone)
            self.git("clean", "-f", "-q", "--", *gone)
        restore = [p for p in paths if p not in gone]
        if restore:
            self.git("checkout", target, "--", *restore)

    # ── Dateien ──────────────────────────────────────────────────────────────
    def prepare(self) -> None:
        self.session.mkdir(parents=True, exist_ok=True)
        self.private.mkdir(parents=True, exist_ok=True)
        for name in (SESSION_DIR, PRIVATE_DIR, *AGENT_TOOL_DIRS):
            gitops.exclude_dir_locally(self.root, name)
        (self.session / config.HARNESS_LOCAL_FILE).write_text(HARNESS_LOCAL, encoding="utf-8")
        for sub in ("scripts", "sessions"):
            (self.relay / sub).mkdir(parents=True, exist_ok=True)
        lint = self.relay / LINT_CONFIG_FILE
        if not lint.exists():  # an existing (also team-adjusted) config stays
            lint.write_text(LINT_CONFIG, encoding="utf-8")

    @property
    def handoff_file(self) -> Path:
        return self.root / handoff_path(self.issue)

    def read_handoff(self) -> Handoff | None:
        f = self.handoff_file
        return Handoff.parse(f.read_text(encoding="utf-8")) if f.is_file() else None

    def write_handoff(self, handoff: Handoff) -> None:
        self.handoff_file.parent.mkdir(parents=True, exist_ok=True)
        self.handoff_file.write_text(handoff.render(), encoding="utf-8")

    def session_log(self, session: int) -> Path:
        return self.relay / "sessions" / f"s{session}.md"

    def write_text(self, name: str, text: str) -> Path:
        """Write a file for the agent in the exchange directory (SESSION_DIR)."""
        return _write(self.session / name, text)

    def _private(self, name: str) -> Path:
        """Harness-internal file (PRIVATE_DIR); if it is missing, the previous step did not run."""
        f = self.private / name
        if not f.is_file():
            raise RuntimeError(t("relay.workspace.missing_did_the_previous_relay", v1=PRIVATE_DIR, name=name))
        return f

    def _write_json(self, name: str, data: object) -> None:
        _write(self.private / name, json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    def _read_json(self, name: str) -> object:
        return json.loads(self._private(name).read_text(encoding="utf-8"))

    def save_snapshot(self, handoff: Handoff) -> None:
        """Handoff state before the agent (session or audit); basis for the reset afterwards."""
        _write(self.private / _BEFORE_FILE, handoff.render())

    def load_snapshot(self) -> Handoff:
        return Handoff.parse(self._private(_BEFORE_FILE).read_text(encoding="utf-8"))

    def save_plan(self, plan: SessionPlan, before: Handoff) -> None:
        self._write_json(_PLAN_FILE, asdict(plan))
        self.save_snapshot(before)

    def load_plan(self) -> tuple[SessionPlan, Handoff]:
        plan = SessionPlan.from_dict(self._read_json(_PLAN_FILE))  # type: ignore[arg-type]
        return plan, self.load_snapshot()

    def save_checkin(self, result: CheckinResult) -> None:
        self._write_json(_CHECKIN_FILE, asdict(result))

    def load_checkin(self) -> CheckinResult:
        data = self._read_json(_CHECKIN_FILE)
        assert isinstance(data, dict)
        return CheckinResult(data["status"], tuple(data["notes"]), tuple(data["out_of_scope"]))

    def read_handoff_or_none(self) -> tuple[Handoff | None, str | None]:
        """(handoff, None) or (None, error) — for the check-in after the agent session."""
        try:
            handoff = self.read_handoff()
        except HandoffError as e:
            return None, str(e)
        return (handoff, None) if handoff else (None, t("relay.workspace.file_missing"))

    # ── Audit (Blind-Phase) ──────────────────────────────────────────────────
    def hide(self, paths: list[str], base: str) -> list[str]:
        """Take branch changes for the blind phase out of the worktree (content stays in git).

        Added/changed files are removed; files deleted on the branch
        are restored from `origin/<base>` so the deletion is not visible.
        The worktree is *deliberately* dirty (unstaged) in the blind phase — `reveal()`
        restores the branch state."""
        hidden = []
        for rel in paths:
            f = self.root / rel
            if f.is_file():
                f.unlink()
                hidden.append(rel)
            elif not self._in_head(rel):
                self.git("checkout", f"origin/{base}", "--", rel)
                hidden.append(rel)
        self._write_json(_HIDDEN_FILE, hidden)
        return hidden

    def reveal(self) -> list[str]:
        hidden = self._read_json(_HIDDEN_FILE)
        assert isinstance(hidden, list)
        for rel in hidden:
            if self._in_head(rel):
                self.git("checkout", "HEAD", "--", rel)
            else:  # deleted on the branch: remove the base restoration again
                self.git("rm", "-q", "-f", "--", rel)
        return hidden

    def _in_head(self, rel: str) -> bool:
        return self._in_tree("HEAD", rel)

    def _in_tree(self, rev: str, rel: str) -> bool:
        p = subprocess.run(["git", "-C", str(self.root), "cat-file", "-e", f"{rev}:{rel}"], timeout=GIT_TIMEOUT_S)
        return p.returncode == 0

    def publish_audit(self) -> str | None:
        """Report + Blind-Evidenz (:func:`copy_evidence`) aus `.fabrikant-session/audit/` ins Relay-Verzeichnis kopieren."""
        src = self.session / "audit"
        report = src / AUDIT_REPORT
        if not report.is_file():
            return None
        dest = self.relay / "audit"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, self.relay / AUDIT_REPORT)
        blind = src / "blind"
        if blind.is_dir():
            copy_evidence(blind, dest / "blind")
        return report.read_text(encoding="utf-8")


def _write(f: Path, text: str) -> Path:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


def copy_evidence(src: Path, dest: Path) -> list[str]:
    """Copy evidence files directly from ``src`` to ``dest``; returns the skipped entries.

    Skipped are subfolders, symlinks, non-regular files and files above
    :data:`MAX_EVIDENCE_FILE_BYTES`; they are listed with a reason in ``dest/SKIPPED.md`` so the
    report does not refer to missing things. A manifest from an earlier run is replaced."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / SKIPPED_EVIDENCE_FILE).unlink(missing_ok=True)
    skipped: list[str] = []
    for entry in sorted(src.iterdir()):
        if entry.name == SKIPPED_EVIDENCE_FILE:
            skipped.append(t("relay.workspace.manifest_name_replaced_by_this", v1=entry.name))
        elif entry.is_symlink():
            skipped.append(f"`{entry.name}`: Symlink")
        elif entry.is_dir():
            skipped.append(t("relay.workspace.directory_working_copy_not_published", v1=entry.name))
        elif not entry.is_file():
            skipped.append(t("relay.workspace.not_a_regular_file", v1=entry.name))
        elif (size := entry.stat().st_size) > MAX_EVIDENCE_FILE_BYTES:
            skipped.append(f"`{entry.name}`: {size} B > {MAX_EVIDENCE_FILE_BYTES} B")
        else:
            shutil.copy2(entry, dest / entry.name)
    if skipped:
        lines = "\n".join(f"- {s}" for s in skipped)
        (dest / SKIPPED_EVIDENCE_FILE).write_text(t("relay.workspace.not_published", lines=lines), encoding="utf-8")
    return skipped
