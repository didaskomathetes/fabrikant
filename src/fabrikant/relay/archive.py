"""Archive of the relay trail (ADR 0037, addendum 2026-11-03): archive, verify, restore.

The trail (`.fabrikant/relay/<N>/`) is the evidence an audit verdict refers to. After a
`pass` it is archived and only then removed from the relay branch by a delete commit, so that PR diff
and merge contain only the actual change. Only the storage location is replaceable
(:class:`TrailArchive`); the verdict binds to the canonical content hash (:func:`trail_digest`).

Methods: `git-ref` (hidden ref `refs/fabrikant/relay/<N>/audit-<k>`, GitHub) and `local`
(directory under `FABRIKANT_DATA/relay-archive/`, development/tests only). `s3` is planned.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .. import config
from ..i18n import t
from .model import TrailRef, format_time
from .workspace import AUDIT_REPORT, GIT_TIMEOUT_S, HANDOFF_FILE, Workspace, relay_dir

METHOD_GIT_REF = "git-ref"
METHOD_LOCAL = "local"
METHOD_S3 = "s3"
METHODS = (METHOD_GIT_REF, METHOD_LOCAL)
PLANNED_METHODS = (METHOD_S3,)
# Upper bound when searching for the next free archive number (protection against infinite loops).
MAX_ARCHIVE_PROBES = 50
LOCAL_ARCHIVE_DIR = "relay-archive"
LOCAL_MANIFEST = "manifest.json"
HTTP_UNPROCESSABLE = 422
# Hash line that audit-publish appends to AUDIT.md; it is ignored for the hash itself.
_TRAILER_PREFIX = "<!-- relay-trail sha256="
_TRAILER_RE = re.compile(r"^" + re.escape(_TRAILER_PREFIX) + r"[0-9a-f]{64} -->\n\Z", re.M)


class ArchiveError(RuntimeError):
    """Archive cannot be created, not readable, or content deviates from the bound hash."""


class ArchiveExists(ArchiveError):
    """An archive already exists under this archive number (archives are immutable)."""


def audit_trailer(digest: str) -> str:
    """Last line of AUDIT.md: to which trail hash the verdict is bound.

    The report must end with `\n` before hashing (:func:`normalize_report`), otherwise the hash
    no longer matches after appending."""
    return f"{_TRAILER_PREFIX}{digest} -->\n"


def normalize_report(text: str) -> str:
    """Normalize the report so that appending + removing the hash line is lossless."""
    return text if text.endswith("\n") else text + "\n"


def _canonical(rel: str, data: bytes) -> bytes:
    if rel != AUDIT_REPORT:
        return data
    return _TRAILER_RE.sub("", data.decode("utf-8", "replace")).encode()


def trail_digest(files: dict[str, bytes]) -> str:
    """Canonical hash: sorted paths (relative to the trail) + SHA-256 per file.

    AUDIT.md counts without the appended hash line — otherwise the report could not contain the hash it
    binds the verdict to."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{hashlib.sha256(_canonical(rel, files[rel])).hexdigest()}\n".encode())
    return h.hexdigest()


def read_tree(root: Path) -> dict[str, bytes]:
    """All regular files under `root` (paths relative, `/`-separated); missing `root` → empty."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class TrailArchive(Protocol):
    """Mandatory interface of every archive method. Archives are immutable: `archive` never overwrites."""

    method: str

    def archive(self, ws: Workspace, commit: str, index: int, now: datetime) -> TrailRef:
        """Store the trail of `commit` under number `index`; :class:`ArchiveExists` if occupied."""
        ...

    def restore(self, ref: TrailRef) -> dict[str, bytes]:
        """Files of the archive (paths relative to the trail)."""
        ...

    def exists(self, ref: TrailRef) -> bool: ...

    def read_handoff(self, ref: TrailRef) -> str | None:
        """Only the archive's `HANDOFF.md` (cheap, for the dispatcher); missing → None."""
        ...


def archive_next(archiver: TrailArchive, ws: Workspace, commit: str, start: int, now: datetime) -> TrailRef:
    """Archive under the next free number from `start` (occupied numbers are skipped)."""
    for index in range(start, start + MAX_ARCHIVE_PROBES):
        try:
            return archiver.archive(ws, commit, index, now)
        except ArchiveExists:
            continue
    raise ArchiveError(t("relay.archive.no_free_archive_number_in", start=start, v2=start + MAX_ARCHIVE_PROBES - 1))


def verify(archiver: TrailArchive, ref: TrailRef) -> dict[str, bytes]:
    """Archive present and content == bound hash; returns the files."""
    if not archiver.exists(ref):
        raise ArchiveError(t("relay.archive.archive_not_found", v1=ref.name, v2=ref.uri))
    files = archiver.restore(ref)
    if (digest := trail_digest(files)) != ref.sha256:
        raise ArchiveError(f"Archive {ref.name}: hash {digest[:12]}… ≠ bound {ref.sha256[:12]}…")
    return files


class GitHubRefs(Protocol):
    """What `git-ref` needs from GitHub (part of `commands.GitHubPort`)."""

    def create_ref(self, ref: str, sha: str) -> None: ...
    def ref_sha(self, ref: str) -> str | None: ...
    def file(self, path: str, ref: str) -> str | None: ...


class GitRefArchive:
    """Hidden ref `refs/fabrikant/relay/<N>/audit-<k>` to the audited commit.

    Does not appear in tag/branch lists, is not fetched without an explicit refspec and keeps the
    commit reachable even after the branch is deleted. `fetch(ws, refspec)` fetches the ref when
    restoring (authenticated); if the commit is already present locally, that is skipped."""

    method = METHOD_GIT_REF

    def __init__(self, github: GitHubRefs, repo: str, repo_root: Path, fetch: Callable[[str], None] | None = None):
        self.github = github
        self.repo = repo
        self.repo_root = repo_root
        self.fetch = fetch

    @staticmethod
    def ref_name(issue: int, index: int) -> str:
        return f"refs/fabrikant/relay/{issue}/audit-{index}"

    def archive(self, ws: Workspace, commit: str, index: int, now: datetime) -> TrailRef:
        ref = self.ref_name(ws.issue, index)
        try:
            self.github.create_ref(ref, commit)
        except urllib.error.HTTPError as e:
            if e.code == HTTP_UNPROCESSABLE:  # "Reference already exists"
                raise ArchiveExists(ref) from e
            raise
        digest = trail_digest(_files_at(self.repo_root, commit, relay_dir(ws.issue)))
        uri = f"https://github.com/{self.repo}/tree/{commit}/{relay_dir(ws.issue)}"
        return TrailRef(METHOD_GIT_REF, ws.issue, index, uri, digest, commit, format_time(now))

    def exists(self, ref: TrailRef) -> bool:
        return self.github.ref_sha(self._ref(ref)) == ref.commit

    def restore(self, ref: TrailRef) -> dict[str, bytes]:
        if not _has_commit(self.repo_root, ref.commit):
            if self.fetch is None:
                raise ArchiveError(t("relay.archive.commit_missing_locally_and_no", v1=ref.commit[:12]))
            name = self._ref(ref)
            self.fetch(f"+{name}:{name}")
        return _files_at(self.repo_root, ref.commit, relay_dir(ref.issue))

    def read_handoff(self, ref: TrailRef) -> str | None:
        return self.github.file(f"{relay_dir(ref.issue)}/{HANDOFF_FILE}", ref.commit)

    def _ref(self, ref: TrailRef) -> str:
        return self.ref_name(ref.issue, ref.index)


class LocalArchive:
    """Directory `FABRIKANT_DATA/relay-archive/<project>/<N>/audit-<k>/` (this machine only)."""

    method = METHOD_LOCAL

    def __init__(self, project: str, base: Path | None = None):
        self.root = (base or config.DATA_DIR / LOCAL_ARCHIVE_DIR) / project

    def _dir(self, issue: int, index: int) -> Path:
        return self.root / str(issue) / f"audit-{index}"

    def archive(self, ws: Workspace, commit: str, index: int, now: datetime) -> TrailRef:
        target = self._dir(ws.issue, index)
        if target.exists():
            raise ArchiveExists(str(target))
        files = _files_at(ws.root, commit, relay_dir(ws.issue))
        digest = trail_digest(files)
        write_tree(target / "trail", files)
        ref = TrailRef(METHOD_LOCAL, ws.issue, index, str(target), digest, commit, format_time(now))
        (target / LOCAL_MANIFEST).write_text(json.dumps(ref.to_dict(), indent=1) + "\n", encoding="utf-8")
        return ref

    def exists(self, ref: TrailRef) -> bool:
        return (Path(ref.uri) / LOCAL_MANIFEST).is_file()

    def restore(self, ref: TrailRef) -> dict[str, bytes]:
        return read_tree(Path(ref.uri) / "trail")

    def read_handoff(self, ref: TrailRef) -> str | None:
        f = Path(ref.uri) / "trail" / HANDOFF_FILE
        return f.read_text(encoding="utf-8") if f.is_file() else None


def _git_bytes(root: Path, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=GIT_TIMEOUT_S)
    if p.returncode != 0:
        raise ArchiveError(f"git {args[0]}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def _has_commit(root: Path, sha: str) -> bool:
    p = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"], timeout=GIT_TIMEOUT_S)
    return p.returncode == 0


def _files_at(root: Path, commit: str, rel_dir: str) -> dict[str, bytes]:
    """Files under `rel_dir` in the commit (paths relative to `rel_dir`), read directly from git."""
    listing = _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", rel_dir).decode()
    files: dict[str, bytes] = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        _mode, kind, sha = meta.split(" ")
        if kind == "blob":
            files[path.removeprefix(rel_dir + "/")] = _git_bytes(root, "cat-file", "blob", sha)
    return files


def bind_report(ws: Workspace, paths: list[str]) -> str:
    """Stage `paths`, compute the trail hash in the index and append it as the last line to AUDIT.md.

    The index is exactly what the audit commit contains; AUDIT.md counts without the hash line."""
    report = ws.relay / AUDIT_REPORT
    report.write_text(normalize_report(report.read_text(encoding="utf-8")), encoding="utf-8")
    if paths:
        ws.git("add", "-A", "--", *paths)
    digest = trail_digest(_files_in_index(ws.root, relay_dir(ws.issue)))
    with report.open("a", encoding="utf-8") as f:
        f.write(audit_trailer(digest))
    return digest


def _files_in_index(root: Path, rel_dir: str) -> dict[str, bytes]:
    listing = _git_bytes(root, "ls-files", "-z", "-s", "--", rel_dir).decode()
    files: dict[str, bytes] = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        _mode, sha, _stage = meta.split(" ")
        files[path.removeprefix(rel_dir + "/")] = _git_bytes(root, "cat-file", "blob", sha)
    return files


def remove_trail(ws: Workspace) -> list[str]:
    """Remove the trail from the working tree; staging + committing the deletion is up to the caller.

    Deliberately no `git rm`: the caller commits the trail path, whose `git add -A` stages the deletion
    deterministically. A `git rm` here would take the path out of index *and* working tree,
    so a subsequent `git add -A` on the path would fail (pathspec matches nothing)."""
    rel = relay_dir(ws.issue)
    removed = [p for p in ws.git("ls-files", "-z", "--", rel).split("\0") if p]
    shutil.rmtree(ws.relay, ignore_errors=True)
    return removed
