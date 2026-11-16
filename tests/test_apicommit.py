"""apicommit: lokale Commits per Git-Data-API nachbauen (signiert), Weg je Projekt (`commit_via`).

`FakeGitHub` spielt die Git-Data-API gegen ein lokales Bare-Repo (= origin) nach: Blobs per
`hash-object`, Trees per temporärem Index, Commits per `commit-tree` mit fester Bot-Identität.
So prüfen die Tests echte Tree-SHAs, Modi, Löschungen und Merge-Parents.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
from helpers import http_error, not_none

from fabrik import apicommit, gitops
from fabrik.apicommit import ApiPusher, Change, parse_diff_tree, push_branch, repo_from_remote
from fabrik.failures import GitError
from fabrik.relay.workspace import Workspace, branch_name

BOT = {"GIT_AUTHOR_NAME": "bot[bot]", "GIT_AUTHOR_EMAIL": "1+bot[bot]@users.noreply.github.com"}
BOT |= {"GIT_COMMITTER_NAME": BOT["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": BOT["GIT_AUTHOR_EMAIL"]}


def git(repo: Path, *args: str, env: dict | None = None, data: bytes | None = None) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, input=data, env={**os.environ, **(env or {})}
    )
    return p.stdout.decode().strip()


class FakeGitHub:
    """Git-Data-API auf einem Bare-Repo; zählt Calls, kann Tree-SHAs verfälschen."""

    def __init__(self, bare: Path, verified: bool = True):
        self.bare = bare
        self.verified = verified
        self.calls: list[tuple[str, str]] = []
        self.corrupt_tree = False

    def __call__(self, method, url, token, payload=None, accept=None):
        path = url.split("/repos/o/r", 1)[1]
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/git/ref/heads/"):
            ref = "refs/heads/" + path.removeprefix("/git/ref/heads/")
            p = subprocess.run(["git", "-C", str(self.bare), "rev-parse", "--verify", "-q", ref], capture_output=True)
            if p.returncode:
                raise http_error(404)
            return {"object": {"sha": p.stdout.decode().strip()}}
        if path == "/git/blobs":
            sha = git(self.bare, "hash-object", "-w", "--stdin", data=base64.b64decode(payload["content"]))
            return {"sha": sha}
        if path == "/git/trees":
            return {"sha": "0" * 40 if self.corrupt_tree else self._tree(not_none(payload))}
        if path == "/git/commits":
            parents = [a for p in payload["parents"] for a in ("-p", p)]
            sha = git(self.bare, "commit-tree", payload["tree"], *parents, "-m", payload["message"], env=BOT)
            return {"sha": sha, "verification": {"verified": self.verified}}
        if path == "/git/refs":
            git(self.bare, "update-ref", payload["ref"], payload["sha"])
            return {}
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            ref = "refs/heads/" + path.removeprefix("/git/refs/heads/")
            old = git(self.bare, "rev-parse", ref)
            ff = subprocess.run(["git", "-C", str(self.bare), "merge-base", "--is-ancestor", old, payload["sha"]])
            if ff.returncode and not payload["force"]:
                raise http_error(422)
            git(self.bare, "update-ref", ref, payload["sha"])
            return {}
        raise AssertionError(f"unerwarteter Call {method} {path}")

    def _tree(self, payload: dict) -> str:
        env = {"GIT_INDEX_FILE": str(self.bare / "fake-index")}
        git(self.bare, "read-tree", payload["base_tree"], env=env)
        lines = [f"{e['mode'] if e['sha'] else '0'} {e['sha'] or '0' * 40}\t{e['path']}\n" for e in payload["tree"]]
        git(self.bare, "update-index", "--index-info", env=env, data="".join(lines).encode())
        return git(self.bare, "write-tree", env=env)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """(Arbeits-Repo auf Branch `feat`, Bare-origin, FakeGitHub als gitops._gh_api)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("base\n")
    (repo / "gone.txt").write_text("bye\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "feat")
    fake = FakeGitHub(bare)
    monkeypatch.setattr(gitops, "_gh_api", fake)
    return repo, bare, fake


def commit(repo: Path, message: str, **files: str | None) -> None:
    for name, content in files.items():
        f = repo / name.replace("__", "/")
        if content is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def pusher(repo: Path) -> ApiPusher:
    return ApiPusher(repo, "o/r", "tok")


# ---------------------------------------------------------------- Parser
def test_parse_diff_tree_handles_add_modify_delete_and_spaces():
    raw = (
        ":000000 100644 " + "0" * 40 + " " + "a" * 40 + " A\0new file.txt\0"
        ":100644 100755 " + "b" * 40 + " " + "c" * 40 + " M\0bin/run\0"
        ":100644 000000 " + "d" * 40 + " " + "0" * 40 + " D\0old.txt\0"
    )
    changes = parse_diff_tree(raw)
    assert [(c.path, c.mode, c.deleted) for c in changes] == [
        ("new file.txt", "100644", False),
        ("bin/run", "100755", False),
        ("old.txt", "000000", True),
    ]
    assert changes[2].tree_entry()["sha"] is None
    sub = Change("vendor/lib", apicommit.MODE_SUBMODULE, "e" * 40, deleted=False)
    assert sub.tree_entry() == {"path": "vendor/lib", "mode": "160000", "type": "commit", "sha": "e" * 40}


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/o/r.git\n", "o/r"),
        ("https://x-access-token:secret@github.com/o/r", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
    ],
)
def test_repo_from_remote(url, repo):
    assert repo_from_remote(url) == repo


def test_repo_from_remote_rejects_other_hosts():
    with pytest.raises(ValueError, match="kein GitHub-Remote"):
        repo_from_remote("https://gitlab.com/o/r.git")


# ---------------------------------------------------------------- ApiPusher
def test_push_new_branch_recreates_commits_with_identical_trees(remote):
    repo, bare, fake = remote
    commit(repo, "feat: add", **{"src__a.py": "print(1)\n", "keep.txt": "changed\n", "gone.txt": None})
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("keep.txt")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: exec + symlink\n\nbody line")
    local_trees = git(repo, "log", "--format=%T", "origin/main..HEAD").split()

    result = pusher(repo).push("feat")

    assert result.commits == 2 and result.verified
    assert git(bare, "log", "--format=%T", "main..feat").split() == local_trees
    assert git(bare, "log", "--format=%an|%B", "-1", "feat").startswith("bot[bot]|feat: exec + symlink\n\nbody line")
    # Lokal auf die signierten Commits umgesetzt, Arbeitsbaum sauber.
    assert git(repo, "rev-parse", "HEAD") == git(bare, "rev-parse", "feat") == result.sha
    assert git(repo, "status", "--porcelain") == ""
    assert ("POST", "/git/refs") in fake.calls
    assert "push feat via API (2 Commit(s), signiert" in result.summary


def test_push_existing_branch_only_sends_new_commits(remote):
    repo, bare, fake = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    pusher(repo).push("feat")
    fake.calls.clear()
    commit(repo, "two", **{"a.txt": "2\n"})
    result = pusher(repo).push("feat")
    assert result.commits == 1
    assert [c for c in fake.calls if c[1] == "/git/commits"] == [("POST", "/git/commits")]
    assert ("PATCH", "/git/refs/heads/feat") in fake.calls
    assert git(bare, "log", "--format=%s", "main..feat").split() == ["two", "one"]


def test_push_up_to_date_is_noop(remote):
    repo, _, fake = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    first = pusher(repo).push("feat")
    fake.calls.clear()
    again = pusher(repo).push("feat")
    assert (again.sha, again.commits) == (first.sha, 0)
    assert all(m == "GET" for m, _ in fake.calls)


def test_push_branch_at_base_without_new_commits_only_creates_ref(remote):
    repo, bare, fake = remote
    result = pusher(repo).push("feat")
    assert result.commits == 0
    assert git(bare, "rev-parse", "feat") == git(bare, "rev-parse", "main")
    assert ("POST", "/git/commits") not in fake.calls


def _diverge(repo: Path, bare: Path) -> None:
    """Remote-`feat` bekommt einen Commit, den das Arbeits-Repo nicht hat."""
    commit(repo, "one", **{"a.txt": "1\n"})
    pusher(repo).push("feat")
    other = repo.parent / "other"
    subprocess.run(["git", "clone", "-q", "-b", "feat", str(bare), str(other)], check=True)
    git(other, "config", "user.email", "o@o")
    git(other, "config", "user.name", "o")
    commit(other, "theirs", **{"b.txt": "x\n"})
    git(other, "push", "-q", "origin", "feat")
    commit(repo, "mine", **{"c.txt": "y\n"})


def test_push_rejects_non_fast_forward_without_force(remote):
    repo, bare, _ = remote
    _diverge(repo, bare)
    with pytest.raises(GitError, match="non-fast-forward"):
        pusher(repo).push("feat")


def test_push_force_overwrites_diverged_remote(remote):
    repo, bare, _ = remote
    _diverge(repo, bare)
    result = pusher(repo).push("feat", force=True)
    assert git(bare, "log", "--format=%s", "main..feat").split() == ["mine", "one"]
    assert result.commits == 1


def test_push_maps_merge_parents(remote):
    repo, bare, _ = remote
    commit(repo, "left", **{"l.txt": "l\n"})
    git(repo, "checkout", "-q", "-b", "side", "origin/main")
    commit(repo, "right", **{"r.txt": "r\n"})
    git(repo, "checkout", "-q", "feat")
    git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side")
    result = pusher(repo).push("feat")
    assert result.commits == 3
    parents = git(bare, "rev-list", "--parents", "-n", "1", "feat").split()[1:]
    assert len(parents) == 2
    assert git(bare, "log", "--format=%s", "-1", parents[1]) == "right"
    assert git(repo, "status", "--porcelain") == ""


def test_push_uses_branch_tip_not_head(remote):
    """Wie `git push origin <branch>`: gepusht wird der lokale Branch, nicht HEAD."""
    repo, bare, _ = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    tip = git(repo, "rev-parse", "feat")
    git(repo, "checkout", "-q", "-b", "other")
    commit(repo, "not pushed", **{"b.txt": "1\n"})
    result = pusher(repo).push("feat")
    assert result.commits == 1
    assert git(bare, "rev-parse", "feat^{tree}") == git(repo, "rev-parse", f"{tip}^{{tree}}")
    assert git(repo, "rev-parse", "feat") == result.sha  # lokaler Branch auf signiertem Commit
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "other"


def test_push_rejects_root_commit(remote):
    repo, _, _ = remote
    git(repo, "checkout", "-q", "--orphan", "lonely")
    commit(repo, "root", **{"x.txt": "x\n"})
    with pytest.raises(GitError, match="Root-Commit"):
        pusher(repo).push("lonely")


def test_push_detects_tree_mismatch(remote):
    repo, _, fake = remote
    fake.corrupt_tree = True
    commit(repo, "one", **{"a.txt": "1\n"})
    with pytest.raises(GitError, match="Tree weicht ab"):
        pusher(repo).push("feat")


def test_push_reports_unverified_commits(remote):
    repo, _, fake = remote
    fake.verified = False
    commit(repo, "one", **{"a.txt": "1\n"})
    result = pusher(repo).push("feat")
    assert not result.verified and "NICHT signiert" in result.summary


def test_ref_update_422_becomes_non_fast_forward(remote, monkeypatch):
    repo, _, _ = remote
    calls = []

    def api(method, url, token, payload=None, accept=None):
        calls.append(method)
        raise http_error(422)

    monkeypatch.setattr(gitops, "_gh_api", api)
    with pytest.raises(GitError, match="non-fast-forward"):
        pusher(repo)._set_ref("feat", "a" * 40, exists=True, force=False)
    monkeypatch.setattr(gitops, "_gh_api", lambda *a, **k: (_ for _ in ()).throw(http_error(500)))
    with pytest.raises(urllib.error.HTTPError):
        pusher(repo)._set_ref("feat", "a" * 40, exists=False, force=False)
    with pytest.raises(urllib.error.HTTPError):
        pusher(repo).remote_head("feat")


# ---------------------------------------------------------------- push_branch / CLI
def test_push_branch_git_mode_uses_git_push(remote):
    repo, bare, fake = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    assert push_branch(repo, "feat") == "push feat"
    assert git(bare, "log", "--format=%an", "-1", "feat") == "t"  # lokaler Autor, kein API-Call
    assert fake.calls == []


def test_push_branch_api_mode_via_project_setting(remote, monkeypatch):
    repo, bare, _ = remote
    monkeypatch.setattr(gitops, "_gh_token", lambda role="dev": "tok")
    monkeypatch.setattr(
        apicommit.subprocess,
        "run",
        _remote_url_override(subprocess.run, "https://github.com/o/r.git"),
    )
    commit(repo, "one", **{"a.txt": "1\n"})
    with gitops.project_github({gitops.SPEC_COMMIT_VIA_KEY: "api"}):
        summary = push_branch(repo, "feat")
    assert summary.startswith("push feat via API (1 Commit(s), signiert")
    assert git(bare, "log", "--format=%an", "-1", "feat") == "bot[bot]"


def _remote_url_override(real_run, url):
    """`git remote get-url origin` → GitHub-URL (origin ist im Test ein lokales Bare-Repo)."""

    def run(cmd, *args, **kwargs):
        if cmd[-3:] == ["remote", "get-url", "origin"]:
            out = url if kwargs.get("text") else url.encode()
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=out[:0])
        return real_run(cmd, *args, **kwargs)

    return run


@pytest.mark.parametrize("branch", ["main", "HEAD"])
def test_push_branch_refuses_protected_and_detached(tmp_path, branch):
    with pytest.raises(GitError, match="BLOCKED"):
        push_branch(tmp_path, branch)


def test_push_branch_git_mode_force_uses_lease(remote):
    repo, bare, _ = remote
    _diverge_plain(repo, bare)
    with pytest.raises(GitError, match="git push"):
        push_branch(repo, "feat")
    git(repo, "fetch", "-q", "origin")
    assert push_branch(repo, "feat", force=True) == "push feat"


def _diverge_plain(repo: Path, bare: Path) -> None:
    commit(repo, "one", **{"a.txt": "1\n"})
    git(repo, "push", "-q", "origin", "feat")
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    commit(repo, "other", **{"b.txt": "1\n"})


def test_cli_pushes_current_branch(remote, monkeypatch, capsys):
    repo, bare, _ = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    monkeypatch.chdir(repo)
    assert apicommit.main([]) == 0
    assert "push feat" in capsys.readouterr().out
    assert git(bare, "rev-parse", "feat") == git(repo, "rev-parse", "HEAD")


def test_cli_maps_errors_to_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # kein Git-Repo
    assert apicommit.main([]) != 0
    assert "fabrik push" in capsys.readouterr().err


# ---------------------------------------------------------------- Projekt-Einstellung
def test_commit_via_defaults_to_git_and_reads_env(monkeypatch):
    monkeypatch.delenv(gitops.COMMIT_VIA_VAR, raising=False)
    assert gitops.commit_via() == "git"
    monkeypatch.setenv(gitops.COMMIT_VIA_VAR, "api")
    assert gitops.commit_via() == "api"  # Subprozess eines api-Projekts (Agent-CLI)


def test_project_github_exports_commit_via_to_subprocesses(monkeypatch):
    monkeypatch.delenv(gitops.COMMIT_VIA_VAR, raising=False)
    with gitops.project_github({gitops.SPEC_COMMIT_VIA_KEY: "api"}):
        assert gitops.commit_via() == "api"
        assert gitops.subprocess_env() == {gitops.COMMIT_VIA_VAR: "api"}
    assert gitops.commit_via() == "git" and gitops.subprocess_env() == {}


@pytest.mark.parametrize("value", ["ssh", 1, ""])
def test_spec_rejects_unknown_commit_via(value):
    from fabrik.specs import _validate_spec

    with pytest.raises(ValueError, match="commit_via"):
        _validate_spec({"name": "x", "root": "/tmp", "loops": {}, "commit_via": value}, check_root=False)


# ---------------------------------------------------------------- Aufrufer
def test_git_step_pushes_via_push_branch(remote, monkeypatch):
    from fabrik.steps import git as git_step

    repo, _, _ = remote
    seen = {}
    monkeypatch.setattr(git_step, "push_branch", lambda base, b, protected: seen.setdefault("b", b) and "push X")
    commit(repo, "one", **{"a.txt": "1\n"})
    res = git_step._step_git({"workdir": str(repo)}, {"push": True})
    assert res == {"ok": True, "summary": "push X"} and seen == {"b": "feat"}


def test_relay_workspace_push_uses_api_in_api_mode(remote, monkeypatch):
    repo, bare, _ = remote
    git(repo, "checkout", "-q", "-b", branch_name(7))
    commit(repo, "relay", **{"h.md": "x\n"})
    ws = Workspace(repo, 7)
    fetched = []
    monkeypatch.setattr(ws, "_authed", lambda r, t, cmd, *a: fetched.append((cmd, a)) or git(repo, cmd, "origin", *a))
    with gitops.project_github({gitops.SPEC_COMMIT_VIA_KEY: "api"}):
        ws.push("o/r", "tok")
    assert git(bare, "log", "--format=%an", "-1", branch_name(7)) == "bot[bot]"
    assert all(cmd == "fetch" for cmd, _ in fetched)
    assert re.fullmatch(r"\+refs/heads/fabrik/relay/7:refs/remotes/origin/fabrik/relay/7", fetched[0][1][0])


def test_relay_workspace_push_uses_git_by_default(remote, monkeypatch):
    repo, _, _ = remote
    git(repo, "checkout", "-q", "-b", branch_name(7))
    ws = Workspace(repo, 7)
    calls = []
    monkeypatch.setattr(ws, "_authed", lambda r, t, cmd, *a: calls.append((cmd, a)))
    ws.push("o/r", "tok")
    assert calls == [("push", (f"HEAD:refs/heads/{branch_name(7)}",))]


def test_push_module_runs_cli_without_runpy_warning(remote):
    repo, bare, _ = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    p = subprocess.run([sys.executable, "-m", "fabrik.push"], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "RuntimeWarning" not in p.stderr
    assert git(bare, "rev-parse", "feat") == git(repo, "rev-parse", "HEAD")


def test_push_retry_after_failed_local_sync_only_catches_up(remote, monkeypatch):
    repo, bare, fake = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    real = ApiPusher._sync_local
    monkeypatch.setattr(ApiPusher, "_sync_local", lambda *a, **k: (_ for _ in ()).throw(GitError("fetch", "x")))
    with pytest.raises(GitError):
        pusher(repo).push("feat")
    monkeypatch.setattr(ApiPusher, "_sync_local", real)
    fake.calls.clear()
    result = pusher(repo).push("feat")  # Remote hat den signierten Commit, lokal noch der alte
    assert result.sha == git(bare, "rev-parse", "feat") == git(repo, "rev-parse", "feat")
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_sync_local_resets_branch_checked_out_in_other_worktree(remote, tmp_path):
    repo, _, _ = remote
    commit(repo, "one", **{"a.txt": "1\n"})
    other = tmp_path / "other"
    git(repo, "checkout", "-q", "main")
    git(repo, "worktree", "add", "-q", str(other), "feat")
    result = pusher(repo).push("feat")  # repo selbst steht auf main, feat liegt in `other`
    assert git(other, "rev-parse", "HEAD") == result.sha
    assert git(other, "status", "--porcelain") == ""


def test_push_rejects_non_utf8_path_with_clear_error():
    raw = ":000000 100644 " + "0" * 40 + " " + "a" * 40 + " A\0bad\ufffd.txt\0"
    with pytest.raises(GitError, match="nicht UTF-8"):
        parse_diff_tree(raw)


def test_commit_422_names_stale_tracking_ref(remote, monkeypatch):
    repo, _, fake = remote
    commit(repo, "one", **{"a.txt": "1\n"})

    def api(method, url, token, payload=None, accept=None):
        if url.endswith("/git/commits"):
            raise http_error(422)
        return fake(method, url, token, payload)

    monkeypatch.setattr(gitops, "_gh_api", api)
    with pytest.raises(GitError, match="Parent"):
        pusher(repo).push("feat")
