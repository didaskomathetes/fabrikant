"""Archiv des Relay-Trails (ADR 0037, Nachtrag 2026-11-03): archivieren, prüfen, wiederherstellen.

Der Trail (`.fabrik/relay/<N>/`) ist die Evidenz, auf die sich ein Audit-Verdikt bezieht. Nach einem
`pass` wird er archiviert und erst dann per Lösch-Commit vom Relay-Branch entfernt, damit PR-Diff
und Merge nur die eigentliche Änderung enthalten. Austauschbar ist allein der Ablageort
(:class:`TrailArchive`); das Verdikt bindet an den kanonischen Inhalts-Hash (:func:`trail_digest`).

Methoden: `git-ref` (versteckter Ref `refs/fabrik/relay/<N>/audit-<k>`, GitHub) und `local`
(Verzeichnis unter `FABRIK_DATA/relay-archive/`, nur Entwicklung/Tests). `s3` ist vorgesehen.
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
from .model import TrailRef, format_time
from .workspace import AUDIT_REPORT, GIT_TIMEOUT_S, HANDOFF_FILE, Workspace, relay_dir

METHOD_GIT_REF = "git-ref"
METHOD_LOCAL = "local"
METHOD_S3 = "s3"
METHODS = (METHOD_GIT_REF, METHOD_LOCAL)
PLANNED_METHODS = (METHOD_S3,)
# Obergrenze beim Suchen der nächsten freien Archiv-Nummer (Schutz vor Endlosschleifen).
MAX_ARCHIVE_PROBES = 50
LOCAL_ARCHIVE_DIR = "relay-archive"
LOCAL_MANIFEST = "manifest.json"
HTTP_UNPROCESSABLE = 422
# Hash-Zeile, die audit-publish an AUDIT.md anhängt; für den Hash selbst wird sie ignoriert.
_TRAILER_PREFIX = "<!-- relay-trail sha256="
_TRAILER_RE = re.compile(r"^" + re.escape(_TRAILER_PREFIX) + r"[0-9a-f]{64} -->\n\Z", re.M)


class ArchiveError(RuntimeError):
    """Archiv nicht anlegbar, nicht lesbar oder Inhalt weicht vom gebundenen Hash ab."""


class ArchiveExists(ArchiveError):
    """Unter dieser Archiv-Nummer liegt schon ein Archiv (Archive sind unveränderlich)."""


def audit_trailer(digest: str) -> str:
    """Letzte Zeile von AUDIT.md: an welchen Trail-Hash das Verdikt gebunden ist.

    Der Report muss vor dem Hashen mit `\n` enden (:func:`normalize_report`), sonst stimmt der Hash
    nach dem Anhängen nicht mehr."""
    return f"{_TRAILER_PREFIX}{digest} -->\n"


def normalize_report(text: str) -> str:
    """Report so normalisieren, dass Anhängen + Entfernen der Hash-Zeile verlustfrei ist."""
    return text if text.endswith("\n") else text + "\n"


def _canonical(rel: str, data: bytes) -> bytes:
    if rel != AUDIT_REPORT:
        return data
    return _TRAILER_RE.sub("", data.decode("utf-8", "replace")).encode()


def trail_digest(files: dict[str, bytes]) -> str:
    """Kanonischer Hash: sortierte Pfade (relativ zum Trail) + SHA-256 je Datei.

    AUDIT.md zählt ohne die angehängte Hash-Zeile — sonst könnte der Report den Hash, an den er das
    Verdikt bindet, nicht enthalten."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{hashlib.sha256(_canonical(rel, files[rel])).hexdigest()}\n".encode())
    return h.hexdigest()


def read_tree(root: Path) -> dict[str, bytes]:
    """Alle regulären Dateien unter `root` (Pfade relativ, `/`-getrennt); fehlt `root` → leer."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class TrailArchive(Protocol):
    """Pflicht-Interface jeder Archiv-Methode. Archive sind unveränderlich: `archive` überschreibt nie."""

    method: str

    def archive(self, ws: Workspace, commit: str, index: int, now: datetime) -> TrailRef:
        """Trail von `commit` unter Nummer `index` ablegen; :class:`ArchiveExists`, wenn belegt."""
        ...

    def restore(self, ref: TrailRef) -> dict[str, bytes]:
        """Dateien des Archivs (Pfade relativ zum Trail)."""
        ...

    def exists(self, ref: TrailRef) -> bool: ...

    def read_handoff(self, ref: TrailRef) -> str | None:
        """Nur `HANDOFF.md` des Archivs (günstig, für den Dispatcher); fehlt → None."""
        ...


def archive_next(archiver: TrailArchive, ws: Workspace, commit: str, start: int, now: datetime) -> TrailRef:
    """Unter der nächsten freien Nummer ab `start` archivieren (belegte Nummern werden übersprungen)."""
    for index in range(start, start + MAX_ARCHIVE_PROBES):
        try:
            return archiver.archive(ws, commit, index, now)
        except ArchiveExists:
            continue
    raise ArchiveError(f"keine freie Archiv-Nummer in audit-{start}…audit-{start + MAX_ARCHIVE_PROBES - 1}")


def verify(archiver: TrailArchive, ref: TrailRef) -> dict[str, bytes]:
    """Archiv vorhanden und Inhalt == gebundener Hash; liefert die Dateien."""
    if not archiver.exists(ref):
        raise ArchiveError(f"Archiv {ref.name} ({ref.uri}) nicht gefunden")
    files = archiver.restore(ref)
    if (digest := trail_digest(files)) != ref.sha256:
        raise ArchiveError(f"Archiv {ref.name}: Hash {digest[:12]}… ≠ gebunden {ref.sha256[:12]}…")
    return files


class GitHubRefs(Protocol):
    """Was `git-ref` von GitHub braucht (Teil von `commands.GitHubPort`)."""

    def create_ref(self, ref: str, sha: str) -> None: ...
    def ref_sha(self, ref: str) -> str | None: ...
    def file(self, path: str, ref: str) -> str | None: ...


class GitRefArchive:
    """Versteckter Ref `refs/fabrik/relay/<N>/audit-<k>` auf den auditierten Commit.

    Erscheint nicht in Tag-/Branch-Listen, wird ohne expliziten Refspec nicht geholt und hält den
    Commit auch nach dem Löschen des Branches erreichbar. `fetch(ws, refspec)` holt den Ref beim
    Wiederherstellen (authentifiziert); liegt der Commit lokal schon vor, entfällt das."""

    method = METHOD_GIT_REF

    def __init__(self, github: GitHubRefs, repo: str, repo_root: Path, fetch: Callable[[str], None] | None = None):
        self.github = github
        self.repo = repo
        self.repo_root = repo_root
        self.fetch = fetch

    @staticmethod
    def ref_name(issue: int, index: int) -> str:
        return f"refs/fabrik/relay/{issue}/audit-{index}"

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
                raise ArchiveError(f"Commit {ref.commit[:12]} fehlt lokal und kein Fetch möglich")
            name = self._ref(ref)
            self.fetch(f"+{name}:{name}")
        return _files_at(self.repo_root, ref.commit, relay_dir(ref.issue))

    def read_handoff(self, ref: TrailRef) -> str | None:
        return self.github.file(f"{relay_dir(ref.issue)}/{HANDOFF_FILE}", ref.commit)

    def _ref(self, ref: TrailRef) -> str:
        return self.ref_name(ref.issue, ref.index)


class LocalArchive:
    """Verzeichnis `FABRIK_DATA/relay-archive/<projekt>/<N>/audit-<k>/` (nur dieser Rechner)."""

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
    """Dateien unter `rel_dir` im Commit (Pfade relativ zu `rel_dir`), direkt aus Git gelesen."""
    listing = _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", rel_dir).decode()
    files: dict[str, bytes] = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        _mode, kind, sha = meta.split(" ")
        if kind == "blob":
            files[path.removeprefix(rel_dir + "/")] = _git_bytes(root, "cat-file", "blob", sha)
    return files


def bind_report(ws: Workspace, paths: list[str]) -> str:
    """`paths` stagen, Hash des Trails im Index berechnen und als letzte Zeile an AUDIT.md hängen.

    Der Index ist genau das, was der Audit-Commit enthält; AUDIT.md zählt ohne die Hash-Zeile."""
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
    """Trail aus dem Arbeitsbaum entfernen; die Löschung stagen + committen tut der Aufrufer.

    Bewusst kein `git rm`: Der Aufrufer committet den Trail-Pfad, dessen `git add -A` die Löschung
    deterministisch stagen. Ein `git rm` hier würde den Pfad aus Index *und* Arbeitsbaum nehmen,
    sodass ein nachfolgendes `git add -A` auf den Pfad fehlschlagen würde (pathspec matcht nichts)."""
    rel = relay_dir(ws.issue)
    removed = [p for p in ws.git("ls-files", "-z", "--", rel).split("\0") if p]
    shutil.rmtree(ws.relay, ignore_errors=True)
    return removed
