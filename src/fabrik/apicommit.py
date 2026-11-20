"""Push über die GitHub-API: lokale Commits als von GitHub signierte Commits nachbauen.

Repos mit `required_signatures` lehnen lokal erzeugte, unsignierte Commits ab. Statt
`git push` baut :class:`ApiPusher` jeden noch nicht auf GitHub liegenden Commit über die
Git-Data-API nach (Blobs → Tree → Commit → Ref). Ohne eigene Author-/Committer-Angaben
signiert GitHub Commits, die eine GitHub-App so anlegt, selbst ("Verified", Autor = App-Bot).
Kein Schlüssel auf dem Host nötig.

Ablauf von :meth:`ApiPusher.push`:
  1. Remote-Head des Branches lesen (API) und lokal holen; ohne `force` muss er Vorfahr
     des lokalen Branches sein (sonst wie `git push`: abgelehnt, non-fast-forward).
  2. Commits `<branch> --not --remotes=origin` in Topo-Reihenfolge nachbauen; Parents, die selbst
     nachgebaut wurden, werden ersetzt. Der Tree eines API-Commits ist inhaltsgleich zum lokalen
     und hat deshalb dieselbe SHA — das wird pro Commit geprüft.
  3. Ref setzen (neu: POST, sonst PATCH, `force` nur auf Wunsch).
  4. Lokal angleichen: Remote-Branch holen, `reset --soft` auf den neuen Head (Index und
     Arbeitsbaum bleiben, weil der Tree identisch ist).

GitHub-spezifisch (Git-Data-API, Signatur durch GitHub); gehört mit den Provider-Ports
(ADR 0062) in den GitHub-Adapter. Auf anderen Hosts verweigert :func:`push_branch`
den API-Weg mit `HostingNotSupported`.

Grenzen: Root-Commits (ohne Parent) werden nicht unterstützt. Autor/Datum der lokalen
Commits gehen verloren (Autor = App); die Message bleibt.

:func:`push_branch` wählt den Weg nach `gitops.commit_via()` (Spec `commit_via: git|api`).
CLI für Agents (pusht den aktuellen Branch, Weg wie im Projekt eingestellt)::

    python -m fabrik.push [--force]
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
from .hosting import HostingNotSupported, get_provider
from .secrets import _redact

GIT_TIMEOUT_S = 300
MODE_FILE = "100644"
MODE_SUBMODULE = "160000"
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")
# Wortlaut wie bei `git push`, damit Logs/Fehlerklassen gleich aussehen.
NON_FAST_FORWARD = "rejected (non-fast-forward)"


@dataclass(frozen=True)
class Change:
    """Eine Zeile aus `git diff-tree --raw`: neuer Stand eines Pfads (gelöscht → `deleted`)."""

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

    Format pro Eintrag: `:<alt-mode> <neu-mode> <alt-sha> <neu-sha> <status>\\0<pfad>\\0`."""
    parts = raw.split("\0")
    changes: list[Change] = []
    for meta, path in zip(parts[0::2], parts[1::2], strict=False):
        if not meta.startswith(":"):
            continue
        _old_mode, new_mode, _old_sha, new_sha, status = meta[1:].split(" ")
        if "\ufffd" in path:
            msg = f"Pfad {path!r} ist nicht UTF-8 — per API nicht pushbar (commit_via: git nutzen)"
            raise GitError(msg, msg)
        changes.append(Change(path=path, mode=new_mode, sha=new_sha, deleted=status.startswith("D")))
    return changes


def repo_from_remote(url: str) -> str:
    """`https://github.com/o/r.git` oder `git@github.com:o/r` → `o/r`."""
    m = _GITHUB_REMOTE.search(url.strip())
    if not m:
        raise ValueError(f"kein GitHub-Remote: {_redact(url)!r}")
    return m.group("repo")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT_S)


def _git(root: Path, *args: str) -> str:
    """`git -C root <args>` → stdout; Fehler als GitError mit redigierter Ausgabe.

    Ausgabe wird tolerant dekodiert (kein UnicodeDecodeError mitten im Push); nicht-UTF-8-Pfade
    erkennt :func:`parse_diff_tree` am Ersatzzeichen."""
    p = _run_git(root, *args)
    if p.returncode != 0:
        output = _redact((p.stderr or p.stdout).decode(errors="replace").strip())
        raise GitError(f"git {args[0]}: {output}", output)
    return p.stdout.decode(errors="replace")


@dataclass
class PushResult:
    """Ergebnis eines API-Pushs: neuer Head, Anzahl nachgebauter Commits, alle signiert?"""

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
    """Pusht den lokalen `branch` eines Worktrees per Git-Data-API nach `repo` (Token: Builder)."""

    def __init__(self, root: Path, repo: str, token: str, fetch: Callable[[str], None] | None = None):
        self.root = Path(root)
        self.repo = repo
        self.token = token
        # Holt `refs/heads/<branch>` nach `refs/remotes/origin/<branch>` (Default: origin, Token-Fallback).
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
            raise GitError(f"git cat-file {sha}: fehlt", p.stderr.decode(errors="replace"))
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
        """Lokalen `branch` als signierte Commits pushen (wie `git push origin <branch>`).

        `force` überschreibt einen abweichenden Remote-Stand."""
        head = self.git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
        remote = self.remote_head(branch)
        if remote:
            self.fetch(branch)
            if remote == head:
                return PushResult(branch=branch, sha=head)
            if not self._is_ancestor(remote, head):
                if self._same_tree(remote, head):
                    # Früherer Push ist durchgelaufen, nur das lokale Angleichen nicht: nachholen.
                    self._sync_local(branch, remote)
                    return PushResult(branch=branch, sha=remote)
                if not force:
                    msg = f"{NON_FAST_FORWARD}: {branch} hat auf GitHub Commits, die lokal fehlen"
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
            msg = f"Root-Commit {commit[:7]} kann nicht per API gepusht werden"
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
                msg = (
                    f"Commit {commit[:7]}: GitHub kennt einen Parent nicht (veralteter origin-Tracking-Ref? "
                    "`git fetch --prune origin`)"
                )
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
                msg = f"Blob {ch.path}: GitHub-SHA {blob.get('sha')} ≠ lokal {ch.sha}"
                raise GitError(msg, msg)
            uploaded.add(ch.sha)
        base_tree = self.git("rev-parse", f"{parent}^{{tree}}").strip()
        tree = self._api("POST", "/git/trees", {"base_tree": base_tree, "tree": [c.tree_entry() for c in changes]})
        if tree.get("sha") != expected:
            msg = f"Tree weicht ab: GitHub {tree.get('sha')} ≠ lokal {expected} (Dateimodus/Symlink?)"
            raise GitError(msg, msg)

    def _set_ref(self, branch: str, sha: str, *, exists: bool, force: bool) -> None:
        try:
            if exists:
                self._api("PATCH", self._ref_path(branch), {"sha": sha, "force": force})
            else:
                self._api("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        except urllib.error.HTTPError as e:
            if e.code == HTTP_UNPROCESSABLE:
                msg = f"{NON_FAST_FORWARD}: Ref {branch} nicht gesetzt (HTTP 422)"
                raise GitError(msg, msg) from e
            raise

    def _sync_local(self, branch: str, sha: str) -> None:
        """Lokalen Branch auf die signierten Commits setzen; Index/Arbeitsbaum bleiben (gleicher Tree)."""
        self.fetch(branch)
        checkout = self._checkout_dir(branch)
        if checkout:  # auch ein anderer Worktree: dort bleiben Index/Arbeitsbaum konsistent
            _git(checkout, "reset", "--soft", sha)
        else:
            self.git("update-ref", f"refs/heads/{branch}", sha)

    def _checkout_dir(self, branch: str) -> Path | None:
        """Worktree-Verzeichnis, in dem `branch` ausgecheckt ist (None: nirgends)."""
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
    """`branch` pushen — per `git push origin` oder (Projekt `commit_via: api`) per API. Liefert Summary.

    Geschützte Branches (`protected`, Default `config.PROTECTED_DEFAULT`) und detached HEAD werden verweigert.
    `commit_via: api` ist GitHub-spezifisch (GitHub signiert die API-Commits); auf anderen
    Hosts wirft es `HostingNotSupported` (dokumentierte Lücke, ADR 0062)."""
    root = Path(root)
    if branch == "HEAD" or branch in (protected or config.PROTECTED_DEFAULT):
        msg = f"BLOCKED: push auf geschützten Branch '{branch}' verweigert"
        raise GitError(msg, msg)
    if gitops.commit_via() != gitops.COMMIT_VIA_API:
        _git(root, "push", *(["--force-with-lease"] if force else []), "origin", branch)
        return f"push {branch}"
    remote_url = _git(root, "remote", "get-url", "origin")
    provider = get_provider(repo or remote_url)
    if provider.name != "github":
        raise HostingNotSupported(
            f"commit_via: api ist GitHub-spezifisch (GitHub signiert die API-Commits); "
            f"der Repo-Host nutzt {provider.name} — commit_via: git verwenden (ADR 0062)"
        )
    if repo is None:
        repo = repo_from_remote(remote_url)
    return ApiPusher(root, repo, gitops._gh_token("dev")).push(branch, force=force).summary


def main(argv: list[str] | None = None) -> int:
    """CLI für Agents: aktuellen Branch pushen (Weg wie im Projekt eingestellt, FABRIK_COMMIT_VIA)."""
    parser = argparse.ArgumentParser(
        prog="python -m fabrik.push", description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--force", action="store_true", help="abweichenden Remote-Stand überschreiben")
    parser.add_argument("--repo", help="owner/name (Default: aus origin)")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        print(push_branch(root, branch, force=args.force, repo=args.repo))
        return 0
    except Exception as e:  # CLI-Grenze: Klasse → Exit-Code (wie relay)
        print(f"fabrik push: {_redact(str(e))}", file=sys.stderr)
        return exit_code(classify_exception(e).kind)
