"""Push via the GitHub API: rebuild local commits as GitHub-signed commits.

Repos with `required_signatures` reject locally created, unsigned commits. Instead of
`git push`, :class:`ApiPusher` rebuilds every commit not yet on GitHub via the
Git Data API (blobs → tree → commit → ref). Without explicit author/committer data,
GitHub signs commits created by a GitHub App itself ("Verified", author = app bot).
No key is needed on the host.

Flow of :meth:`ApiPusher.push`:
  1. Read the branch's remote head (API) and fetch it locally; without `force` it must be an
     ancestor of the local branch (otherwise like `git push`: rejected, non-fast-forward).
  2. Rebuild commits `<branch> --not --remotes=origin` in topological order; parents that were
     rebuilt themselves are replaced. The tree of an API commit has identical content to the local
     one and therefore the same SHA — this is checked per commit.
  3. Set the ref (new: POST, otherwise PATCH, `force` only on request).
  4. Align locally: fetch the remote branch, `reset --soft` to the new head (index and
     working tree stay, because the tree is identical).

GitHub-specific (Git Data API, signing by GitHub); belongs with the provider ports
(ADR 0062) in the GitHub adapter. On other hosts :func:`push_branch` refuses
the API path with `HostingNotSupported`.

Limits: root commits (without parent) are not supported. Author/date of the local
commits are lost (author = app); the message stays.

:func:`push_branch` picks the path according to `gitops.commit_via()` (spec `commit_via: git|api`).
CLI for agents (pushes the current branch, path as configured in the project)::

    python -m fabrikant.push [--force]
"""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, gitops
from .failures import GitError, classify_exception, exit_code
from .hosting import GITHUB_HOST, HostingNotSupported, current_spec, repo_host
from .hosting.select import HOSTING_KEY
from .i18n import t
from .secrets import _redact

GIT_TIMEOUT_S = 300
MODE_FILE = "100644"
MODE_SUBMODULE = "160000"
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")
# Wording as with `git push`, so logs/failure classes look the same.
NON_FAST_FORWARD = "rejected (non-fast-forward)"


@dataclass(frozen=True)
class Change:
    """One line of `git diff-tree --raw`: new state of a path (deleted → `deleted`)."""

    path: str
    mode: str
    sha: str
    deleted: bool

    @property
    def is_submodule(self) -> bool:
        return self.mode == MODE_SUBMODULE

    def tree_entry(self) -> dict:
        if self.deleted:
            return {"path": self.path, "mode": MODE_FILE, "type": "blob", "sha": None}
        kind = "commit" if self.is_submodule else "blob"
        return {"path": self.path, "mode": self.mode, "type": kind, "sha": self.sha}


def parse_diff_tree(raw: str) -> list[Change]:
    """`git diff-tree -r --raw -z --no-renames` → Changes.

    Format per entry: `:<old-mode> <new-mode> <old-sha> <new-sha> <status>\\0<pfad>\\0`."""
    parts = raw.split("\0")
    changes: list[Change] = []
    for meta, path in zip(parts[0::2], parts[1::2], strict=False):
        if not meta.startswith(":"):
            continue
        _old_mode, new_mode, _old_sha, new_sha, status = meta[1:].split(" ")
        if "\ufffd" in path:
            msg = t("apicommit.path_is_not_utf_8", path=path)
            raise GitError(msg, msg)
        changes.append(Change(path=path, mode=new_mode, sha=new_sha, deleted=status.startswith("D")))
    return changes


def repo_from_remote(url: str) -> str:
    """`https://github.com/o/r.git` or `git@github.com:o/r` → `o/r`."""
    m = _GITHUB_REMOTE.search(url.strip())
    if not m:
        raise ValueError(t("apicommit.no_github_remote", v1=_redact(url)))
    return m.group("repo")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT_S)


def _git(root: Path, *args: str) -> str:
    """`git -C root <args>` → stdout; errors as GitError with redacted output.

    Output is decoded tolerantly (no UnicodeDecodeError in the middle of a push); non-UTF-8 paths
    are recognized by :func:`parse_diff_tree` via the replacement character."""
    p = _run_git(root, *args)
    if p.returncode != 0:
        output = _redact((p.stderr or p.stdout).decode(errors="replace").strip())
        raise GitError(f"git {args[0]}: {output}", output)
    return p.stdout.decode(errors="replace")


@dataclass
class PushResult:
    """Result of an API push: new head, number of rebuilt commits, all signed?"""

    branch: str
    sha: str
    commits: int = 0
    verified: bool = True
    replaced: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        sig = "signiert" if self.verified else "NICHT signiert"
        return f"push {self.branch} via API ({self.commits} Commit(s), {sig}, {self.sha[:7]})"


class ApiPusher:
    """Pushes a worktree's local `branch` to `repo` via the Git Data API (token: builder)."""

    def __init__(self, root: Path, repo: str, token: str, fetch: Callable[[str], None] | None = None):
        self.root = Path(root)
        self.repo = repo
        self.token = token
        # Fetches `refs/heads/<branch>` into `refs/remotes/origin/<branch>` (default: origin, token fallback).
        self.fetch = fetch or (lambda branch: gitops._git_fetch(self.root, branch))

    # ── git ──────────────────────────────────────────────────────────────────
    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return _run_git(self.root, *args)

    def git(self, *args: str) -> str:
        return _git(self.root, *args)

    def _is_ancestor(self, older: str, newer: str) -> bool:
        return self._run("merge-base", "--is-ancestor", older, newer).returncode == 0

    def _same_tree(self, a: str, b: str) -> bool:
        return self.git("rev-parse", f"{a}^{{tree}}").strip() == self.git("rev-parse", f"{b}^{{tree}}").strip()

    def _blob(self, sha: str) -> bytes:
        p = self._run("cat-file", "blob", sha)
        if p.returncode != 0:
            raise GitError(t("apicommit.git_cat_file_missing", sha=sha), p.stderr.decode(errors="replace"))
        return p.stdout

    # ── API ──────────────────────────────────────────────────────────────────
    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        return gitops._gh_api(method, f"{gitops.GITHUB_API}/repos/{self.repo}{path}", self.token, payload)

    def _ref_path(self, branch: str) -> str:
        return "/git/refs/heads/" + urllib.parse.quote(branch, safe="/")

    def remote_head(self, branch: str) -> str | None:
        try:
            ref = self._api("GET", "/git/ref/heads/" + urllib.parse.quote(branch, safe="/"))
        except urllib.error.HTTPError as e:
            if e.code == HTTP_NOT_FOUND:
                return None
            raise
        return str(ref["object"]["sha"])

    # ── Push ─────────────────────────────────────────────────────────────────
    def push(self, branch: str, *, force: bool = False) -> PushResult:
        """Push the local `branch` as signed commits (like `git push origin <branch>`).

        `force` overwrites a diverging remote state."""
        head = self.git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
        remote = self.remote_head(branch)
        if remote:
            self.fetch(branch)
            if remote == head:
                return PushResult(branch=branch, sha=head)
            if not self._is_ancestor(remote, head):
                if self._same_tree(remote, head):
                    # An earlier push went through, only the local alignment did not: catch up.
                    self._sync_local(branch, remote)
                    return PushResult(branch=branch, sha=remote)
                if not force:
                    msg = t("apicommit.has_commits_on_github_that", v1=NON_FAST_FORWARD, branch=branch)
                    raise GitError(msg, msg)
        exclude = [remote] if remote else []
        pending = self.git("rev-list", "--reverse", "--topo-order", head, "--not", "--remotes=origin", *exclude)
        result = PushResult(branch=branch, sha=head)
        for commit in pending.split():
            result.replaced[commit] = self._recreate(commit, result)
        result.sha = result.replaced.get(head, head)
        result.commits = len(result.replaced)
        self._set_ref(branch, result.sha, exists=remote is not None, force=force)
        self._sync_local(branch, result.sha)
        return result

    def _recreate(self, commit: str, result: PushResult) -> str:
        parents = self.git("rev-list", "--parents", "-n", "1", commit).split()[1:]
        if not parents:
            msg = t("apicommit.root_commit_cannot_be_pushed", v1=commit[:7])
            raise GitError(msg, msg)
        tree = self.git("rev-parse", f"{commit}^{{tree}}").strip()
        changes = parse_diff_tree(self.git("diff-tree", "-r", "--raw", "-z", "--no-renames", parents[0], commit))
        if changes:
            self._create_tree(parents[0], changes, expected=tree)
        message = self.git("log", "-1", "--format=%B", commit).rstrip("\n") + "\n"
        try:
            created = self._api(
                "POST",
                "/git/commits",
                {"message": message, "tree": tree, "parents": [result.replaced.get(p, p) for p in parents]},
            )
        except urllib.error.HTTPError as e:
            if e.code == HTTP_UNPROCESSABLE:
                msg = t("apicommit.commit_github_does_not_know", v1=commit[:7])
                raise GitError(msg, msg) from e
            raise
        result.verified = result.verified and bool((created.get("verification") or {}).get("verified"))
        return str(created["sha"])

    def _create_tree(self, parent: str, changes: list[Change], expected: str) -> None:
        uploaded: set[str] = set()
        for ch in changes:
            if ch.deleted or ch.is_submodule or ch.sha in uploaded:
                continue
            content = base64.b64encode(self._blob(ch.sha)).decode()
            blob = self._api("POST", "/git/blobs", {"content": content, "encoding": "base64"})
            if blob.get("sha") != ch.sha:
                msg = f"Blob {ch.path}: GitHub SHA {blob.get('sha')} ≠ local {ch.sha}"
                raise GitError(msg, msg)
            uploaded.add(ch.sha)
        base_tree = self.git("rev-parse", f"{parent}^{{tree}}").strip()
        tree = self._api("POST", "/git/trees", {"base_tree": base_tree, "tree": [c.tree_entry() for c in changes]})
        if tree.get("sha") != expected:
            msg = f"Tree mismatch: GitHub {tree.get('sha')} ≠ local {expected} (file mode/symlink?)"
            raise GitError(msg, msg)

    def _set_ref(self, branch: str, sha: str, *, exists: bool, force: bool) -> None:
        try:
            if exists:
                self._api("PATCH", self._ref_path(branch), {"sha": sha, "force": force})
            else:
                self._api("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        except urllib.error.HTTPError as e:
            if e.code == HTTP_UNPROCESSABLE:
                msg = t("apicommit.ref_not_set_http_422", v1=NON_FAST_FORWARD, branch=branch)
                raise GitError(msg, msg) from e
            raise

    def _sync_local(self, branch: str, sha: str) -> None:
        """Set the local branch to the signed commits; index/working tree stay (same tree)."""
        self.fetch(branch)
        checkout = self._checkout_dir(branch)
        if checkout:  # also another worktree: there the index/working tree stay consistent
            _git(checkout, "reset", "--soft", sha)
        else:
            self.git("update-ref", f"refs/heads/{branch}", sha)

    def _checkout_dir(self, branch: str) -> Path | None:
        """Worktree directory in which `branch` is checked out (None: nowhere)."""
        current: Path | None = None
        for line in self.git("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                current = Path(line.removeprefix("worktree "))
            elif line == f"branch refs/heads/{branch}":
                return current
        return None


def push_branch(
    root: Path,
    branch: str,
    *,
    force: bool = False,
    repo: str | None = None,
    protected: list[str] | None = None,
) -> str:
    """Push `branch` — via `git push origin` or (project `commit_via: api`) via the API. Returns a summary.

    Protected branches (`protected`, default `config.PROTECTED_DEFAULT`) and detached HEAD are refused.
    `commit_via: api` is GitHub-specific (GitHub signs the API commits); on other
    hosts it raises `HostingNotSupported` (documented gap, ADR 0062)."""

    root = Path(root)
    if branch == "HEAD" or branch in (protected or config.PROTECTED_DEFAULT):
        msg = t("apicommit.blocked_push_to_protected_branch", branch=branch)
        raise GitError(msg, msg)
    if gitops.commit_via() != gitops.COMMIT_VIA_API:
        _git(root, "push", *(["--force-with-lease"] if force else []), "origin", branch)
        return f"push {branch}"
    remote_url = repo or _git(root, "remote", "get-url", "origin")
    # Decide on the host before a provider (and its credentials) is built.
    host = repo_host(remote_url)
    if host not in (None, GITHUB_HOST):
        raise HostingNotSupported(
            "commit_via: api is GitHub-specific (GitHub signs the API commits); "
            f"the repo host ({host}) is not GitHub — use commit_via: git (ADR 0062)"
        )
    # A bare owner/name slug does not reveal the host — then `hosting:` of the active spec decides.

    spec_hosting = current_spec().get(HOSTING_KEY)
    if isinstance(spec_hosting, str) and spec_hosting.strip().lower() != "github":
        raise HostingNotSupported(
            "commit_via: api is GitHub-specific (GitHub signs the API commits); "
            f"hosting: {spec_hosting} is not GitHub — use commit_via: git (ADR 0062)"
        )
    if repo is None:
        repo = repo_from_remote(remote_url)
    return ApiPusher(root, repo, gitops._gh_token("dev")).push(branch, force=force).summary


def main(argv: list[str] | None = None) -> int:
    """CLI for agents: push the current branch (path as configured in the project, FABRIKANT_COMMIT_VIA)."""
    parser = argparse.ArgumentParser(
        prog="python -m fabrikant.push", description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--force", action="store_true", help=t("apicommit.overwrite_diverging_remote_state"))
    parser.add_argument("--repo", help=t("apicommit.owner_name_default_from_origin"))
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        print(push_branch(root, branch, force=args.force, repo=args.repo))
        return 0
    except Exception as e:  # CLI boundary: class → exit code (like relay)
        print(f"fabrikant push: {_redact(str(e))}", file=sys.stderr)
        return exit_code(classify_exception(e).kind)
