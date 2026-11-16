"""Relay-Dateien im Worktree: Handoff, Session-Dateien, Scope, Commit/Push, Blind-Phase.

Layout auf dem Branch `fabrik/relay/<N>`:
    .fabrik/relay/<N>/HANDOFF.md        Zustand + Gedächtnis (committet)
    .fabrik/relay/<N>/scripts/          jedes ausgeführte Ad-hoc-Skript / SQL (committet)
    .fabrik/relay/<N>/sessions/s<k>.md  was Session k ausgeführt hat + Ergebnisse (committet)
    .fabrik/relay/<N>/AUDIT.md, audit/  Audit-Report + Blind-Evidenz (committet, ohne Unterordner)
    .fabrik/relay/<N>/ruff.toml         nimmt das Verzeichnis aus dem ruff-Lint des Zielrepos (committet)
    .fabrik-session/                    Brief, SESSION.md, Audit-Arbeitsstand (lokal, git-ignoriert;
                                        die Sandbox trägt es zum Agenten und zurück)
    .fabrik-harness/                    Plan, Snapshot, Check-in (lokal, git-ignoriert; nie beim Agenten)
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
from ..secrets import _redact
from .model import Handoff, HandoffError

SESSION_DIR = config.HARNESS_DIR
# Harness-interner Zustand (Scope, Verify-Kommando, Handoff-Snapshot, Blind-Liste) liegt getrennt
# vom Austauschverzeichnis: Die Sandbox trägt SESSION_DIR zum Agenten und zurück — was dort liegt,
# kann der Agent ändern.
PRIVATE_DIR = config.HARNESS_PRIVATE_DIR
# Bleibt beim Agenten, kommt nicht zurück: Arbeitskopien des Audits (Klone, venvs).
HARNESS_LOCAL = "audit/work/\n"
# Zustand, den Werkzeuge der Agent-Umgebung ungefragt ins Arbeitsverzeichnis schreiben (z. B. der
# Serena-MCP-Server: `.serena/project.yml`). Lokal ignoriert, damit er nicht als Änderung außerhalb
# des Scopes zählt und die Session blockiert. Nur ungetrackte Dateien — getrackte bleiben sichtbar.
AGENT_TOOL_DIRS = (".serena",)
_PLAN_FILE = "plan.json"
_BEFORE_FILE = "HANDOFF.before.md"
_CHECKIN_FILE = "checkin.json"
_HIDDEN_FILE = "hidden.json"
AUDIT_REPORT = "AUDIT.md"
HANDOFF_FILE = "HANDOFF.md"
# Vom Blind-Audit wird nur Evidenz veröffentlicht: Dateien direkt in `audit/blind/` (Prüfskripte und
# Rohausgaben, `<nn>-<invariante>.<ext>`). Unterordner (ausgecheckte Trees, venvs) und große Dateien
# bleiben lokal: Sie gehören nicht in den PR, und `commit_via: api` lädt jede Datei einzeln hoch.
MAX_EVIDENCE_FILE_BYTES = 1_000_000
SKIPPED_EVIDENCE_FILE = "SKIPPED.md"
# Relay-Artefakte (Worker-Skripte, Audit-Evidenz) sind Protokoll, kein Produktcode: Eine verschachtelte
# ruff-Config nimmt `.fabrik/relay/<N>/` aus dem Lint/Format-Check des Zielrepos, ohne dessen Config
# anzufassen. Die Skripte bleiben byte-genau (ihre `.out`-Dateien sind ihre Ausgabe).
LINT_CONFIG_FILE = "ruff.toml"
LINT_CONFIG = (
    "# fabrik relay: worker scripts and audit evidence are records of what ran, not product code.\n"
    "# Keep them byte-for-byte and out of the repo's lint/format checks.\n"
    'exclude = ["*"]\n'
)
GIT_TIMEOUT_S = 300
# Credential-Helper liest das Token aus der Env — nie in argv/URL (Prozessliste, Logs).
# Kein hartkodiertes Passwort: `$RELAY_PUSH_TOKEN` ist eine Env-Variable; der git-Credential-Helper
# muss wörtlich `password=…` echoen (deshalb NOSONAR).
_CRED_HELPER = '!f() { echo username=x-access-token; echo "password=$RELAY_PUSH_TOKEN"; }; f'  # NOSONAR


def branch_name(issue: int) -> str:
    return f"fabrik/relay/{issue}"


def relay_dir(issue: int) -> str:
    return f".fabrik/relay/{issue}"


def handoff_path(issue: int) -> str:
    return f"{relay_dir(issue)}/{HANDOFF_FILE}"


def matches(path: str, pattern: str) -> bool:
    """Pfad passt auf Glob; `dir/` und `dir/**` matchen alles darunter."""
    stem = pattern.removesuffix("**").rstrip("/")
    if pattern.endswith(("/", "/**")):
        return path == stem or path.startswith(stem + "/")
    return fnmatch.fnmatchcase(path, pattern)


@dataclass(frozen=True)
class SessionPlan:
    """Von `checkout` festgehalten, von `checkin`/`publish` gelesen (Agent ändert ihn nicht)."""

    issue: int
    session: int
    max_sessions: int
    started: str
    soft_deadline: str
    hard_deadline: str
    verify_command: str | None
    scope: tuple[str, ...]
    forbid: tuple[str, ...]
    head: str = ""  # Commit beim Checkout — Basis des Scope-Checks ("" = Plan vor diesem Feld)

    @classmethod
    def from_dict(cls, data: dict) -> SessionPlan:
        return cls(**{**data, "scope": tuple(data["scope"]), "forbid": tuple(data["forbid"])})


@dataclass(frozen=True)
class CheckinResult:
    status: str
    notes: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()


class Workspace:
    """Relay-Sicht auf einen Worktree für Issue `issue`."""

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
        """SHA von `MERGE_HEAD`, solange ein Merge (ungecommittet) im Worktree läuft."""
        return self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip() or None

    def _diff_names(self, rev: str, other: str | None = None) -> list[str]:
        """Geänderte Pfade: `rev` gegen Worktree, oder `rev` gegen `other` (zwei Revisions)."""
        # `--no-renames`: ein Rename zählt mit beiden Seiten (sonst verschwände die Löschung).
        args = [rev, other] if other is not None else [rev]
        return [p for p in self.git("diff", "--name-only", "--no-renames", "-z", *args).split("\0") if p]

    def settle_session(self, start: str, base: str) -> list[str]:
        """Git-Zustand nach der Agent-Session auf das bringen, was der Scope-Check bewerten kann.

        - Eigene Commits des Agents (HEAD ≠ `start`) werden per Soft-Reset in den Worktree
          zurückgeholt: Committen ist Sache der Harness, sonst entgingen sie dem Scope-Check.
        - Ein laufender Merge bleibt nur, wenn er einen Stand von `origin/<base>` einmergt; jeder
          andere wird vergessen (`merge --quit`), seine Änderungen zählen dann als Session-Änderungen.
        Liefert Notizen für die Log-Zeile."""
        notes: list[str] = []
        if start and self.head_sha() != start:
            if self.merge_head():
                self.git("merge", "--quit")  # Soft-Reset ist mitten im Merge nicht erlaubt
            self.git("reset", "-q", "--soft", start)
            notes.append("Agent-Commits in den Worktree zurückgeholt (fabrik committet)")
        merge = self.merge_head()
        if merge and not self._is_ancestor(merge, f"origin/{base}"):
            self.git("merge", "--quit")
            notes.append(f"Merge von {merge[:8]} verworfen (kein Stand von origin/{base})")
        return notes

    def _is_ancestor(self, rev: str, of: str) -> bool:
        p = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", rev, of], timeout=GIT_TIMEOUT_S)
        return p.returncode == 0

    def changed_paths(self) -> list[str]:
        """Von der Session geänderte + untracked Pfade (repo-relativ, ohne ignorierte).

        Läuft ein Merge, zählt ein Pfad nur, wenn er von BEIDEN Seiten abweicht: Was unverändert
        aus `MERGE_HEAD` kommt, hat der Merge gebracht, nicht die Session. Ausnahme: Hat die Base
        einen Pfad gegenüber dem Merge-Base geändert, der Branch nicht, und steht im Worktree die
        HEAD-Version, hat die Session die Base-Änderung zurückgedreht ("take ours") — das ist
        unsichtbar für `diff HEAD`, zählt aber als Änderung."""
        head = self._diff_names("HEAD")
        if merge := self.merge_head():
            theirs = set(self._diff_names(merge))
            tracked = [p for p in head if p in theirs]
            base = self.git("merge-base", "HEAD", merge).strip()
            main_only = set(self._diff_names(base, merge)) - set(self._diff_names(base, "HEAD"))
            # Nur Pfade, deren Worktree-Stand exakt der HEAD-Stand ist (sonst z. B. eine von der
            # Base hinzugefügte Datei, die der Merge korrekt ins Worktree gelegt hat).
            tracked += [p for p in main_only if p not in head]
        else:
            tracked = head
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return sorted({p for p in tracked + untracked if p})

    def split_changes(self, allowed: tuple[str, ...], forbid: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
        """Geänderte Pfade → (passt auf `allowed` und nicht auf `forbid`, Rest)."""
        inside: list[str] = []
        outside: list[str] = []
        for path in self.changed_paths():
            ok = any(matches(path, p) for p in allowed) and not any(matches(path, p) for p in forbid)
            (inside if ok else outside).append(path)
        return inside, outside

    def partition(self, scope: tuple[str, ...], forbid: tuple[str, ...]) -> tuple[list[str], list[str]]:
        """(in Scope, außerhalb) einer Session — Relay-Verzeichnis + `scope`, minus `forbid`."""
        return self.split_changes((relay_dir(self.issue) + "/", *scope), forbid)

    def audit_paths(self) -> tuple[str, ...]:
        """Was ein Audit committen darf: Report, Blind-Evidenz, Handoff und Lint-Config (beide von der Harness)."""
        rel = relay_dir(self.issue)
        return (handoff_path(self.issue), f"{rel}/{AUDIT_REPORT}", f"{rel}/audit/", f"{rel}/{LINT_CONFIG_FILE}")

    def commit(self, paths: list[str], message: str, author: tuple[str, str]) -> str:
        """`paths` stagen + committen (No-Op ohne Änderung); liefert den Kurz-SHA von HEAD.

        Committet wird genau `paths` — Vorgestagtes außerhalb fliegt aus dem Index. Ausnahme:
        Läuft ein Merge, wird der ganze Merge-Stand committet (ein Merge-Commit mit Teilstand
        würde die fehlenden Änderungen der Gegenseite still zurückdrehen); Fremdes hat der
        Check-in bis dahin verworfen. `author` = (name, email) für Author und Committer."""
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
        """HEAD → `fabrik/relay/<N>` als Builder-App; jeder andere Branch wird verweigert.

        Projekt mit `commit_via: api`: von GitHub signierte API-Commits statt `git push`."""
        branch = branch_name(self.issue)
        if self.current_branch() != branch:
            raise RuntimeError(f"Push verweigert: Worktree steht auf {self.current_branch()!r}, nicht {branch!r}")
        if gitops.commit_via() == gitops.COMMIT_VIA_API:

            def fetch(name: str) -> None:
                self.fetch(repo, token, gitops.tracking_refspec(name))

            ApiPusher(self.root, repo, token, fetch=fetch).push(branch)
            return
        self._authed(repo, token, "push", f"HEAD:refs/heads/{branch}")

    def fetch(self, repo: str, token: str, refspec: str) -> None:
        """`refspec` vom GitHub-Repo holen (authentifiziert wie der Push)."""
        self._authed(repo, token, "fetch", refspec)

    def _authed(self, repo: str, token: str, command: str, *args: str) -> None:
        """`git <command> https://github.com/<repo>.git <args>` mit dem Token per Credential-Helper."""
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
        """Pfade, die der Relay-Branch gegenüber `origin/<base>` hinzugefügt/geändert/gelöscht hat.

        `--no-renames`: ein Rename zählt als Löschung + Hinzufügung, beide Seiten sind sichtbar."""
        out = self.git("diff", "--name-only", "--no-renames", "--diff-filter=AMD", "-z", f"origin/{base}...HEAD")
        return sorted(p for p in out.split("\0") if p)

    def revert(self, paths: list[str]) -> None:
        """Änderungen an `paths` verwerfen (Index + Worktree).

        Ziel ist HEAD; während eines Merges `MERGE_HEAD`: Pfade außerhalb des Scopes ändert der
        Branch nie, ihr korrektes Merge-Ergebnis ist also der Stand der eingemergten Seite.
        Pfade, die es im Ziel nicht gibt, werden aus dem Index genommen und gelöscht."""
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
        if not lint.exists():  # eine vorhandene (auch vom Team angepasste) Config bleibt
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
        """Datei für den Agenten im Austauschverzeichnis (SESSION_DIR) schreiben."""
        return _write(self.session / name, text)

    def _private(self, name: str) -> Path:
        """Harness-interne Datei (PRIVATE_DIR); fehlt sie, lief der vorherige Step nicht."""
        f = self.private / name
        if not f.is_file():
            raise RuntimeError(f"{PRIVATE_DIR}/{name} fehlt — lief der vorherige Relay-Step?")
        return f

    def _write_json(self, name: str, data: object) -> None:
        _write(self.private / name, json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    def _read_json(self, name: str) -> object:
        return json.loads(self._private(name).read_text(encoding="utf-8"))

    def save_snapshot(self, handoff: Handoff) -> None:
        """Handoff-Stand vor dem Agent (Session oder Audit); Basis für das Zurücksetzen danach."""
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
        """(Handoff, None) oder (None, Fehler) — für den Check-in nach der Agent-Session."""
        try:
            handoff = self.read_handoff()
        except HandoffError as e:
            return None, str(e)
        return (handoff, None) if handoff else (None, "Datei fehlt")

    # ── Audit (Blind-Phase) ──────────────────────────────────────────────────
    def hide(self, paths: list[str], base: str) -> list[str]:
        """Branch-Änderungen für die Blind-Phase aus dem Worktree nehmen (Inhalt bleibt in Git).

        Hinzugefügte/geänderte Dateien werden entfernt; auf dem Branch gelöschte Dateien
        werden aus `origin/<base>` wiederhergestellt, damit die Löschung nicht sichtbar ist.
        Der Worktree ist in der Blind-Phase *bewusst* schmutzig (unstaged) — `reveal()`
        stellt den Branch-Zustand wieder her."""
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
            else:  # auf dem Branch gelöscht: Base-Wiederherstellung wieder entfernen
                self.git("rm", "-q", "-f", "--", rel)
        return hidden

    def _in_head(self, rel: str) -> bool:
        return self._in_tree("HEAD", rel)

    def _in_tree(self, rev: str, rel: str) -> bool:
        p = subprocess.run(["git", "-C", str(self.root), "cat-file", "-e", f"{rev}:{rel}"], timeout=GIT_TIMEOUT_S)
        return p.returncode == 0

    def publish_audit(self) -> str | None:
        """Report + Blind-Evidenz (:func:`copy_evidence`) aus `.fabrik-session/audit/` ins Relay-Verzeichnis kopieren."""
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
    """Evidenz-Dateien direkt aus ``src`` nach ``dest`` kopieren; liefert die übersprungenen Einträge.

    Übersprungen werden Unterordner, Symlinks, keine regulären Dateien und Dateien über
    :data:`MAX_EVIDENCE_FILE_BYTES`; sie stehen mit Grund in ``dest/SKIPPED.md``, damit der
    Report nicht auf Fehlendes verweist. Ein Manifest aus einem früheren Lauf wird ersetzt."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / SKIPPED_EVIDENCE_FILE).unlink(missing_ok=True)
    skipped: list[str] = []
    for entry in sorted(src.iterdir()):
        if entry.name == SKIPPED_EVIDENCE_FILE:
            skipped.append(f"`{entry.name}`: Manifest-Name (wird durch diese Liste ersetzt)")
        elif entry.is_symlink():
            skipped.append(f"`{entry.name}`: Symlink")
        elif entry.is_dir():
            skipped.append(f"`{entry.name}/`: Ordner (Arbeitskopie, nicht veröffentlicht)")
        elif not entry.is_file():
            skipped.append(f"`{entry.name}`: keine reguläre Datei")
        elif (size := entry.stat().st_size) > MAX_EVIDENCE_FILE_BYTES:
            skipped.append(f"`{entry.name}`: {size} B > {MAX_EVIDENCE_FILE_BYTES} B")
        else:
            shutil.copy2(entry, dest / entry.name)
    if skipped:
        lines = "\n".join(f"- {s}" for s in skipped)
        (dest / SKIPPED_EVIDENCE_FILE).write_text(f"# Nicht veröffentlicht\n\n{lines}\n", encoding="utf-8")
    return skipped
