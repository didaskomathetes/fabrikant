"""Git-Worktrees + GitHub-API + generischer HTTP-POST (fetch, worktree, Token, REST)."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from . import config, httpsafe
from .failures import GitError, SetupError
from .filelock import locked
from .secrets import _redact

# Dual-Identity (ADR 0010): "dev" = Builder-App (erstellt/mergt PRs),
# "reviewer" = Reviewer-App (approvet PRs). GitHub verbietet Self-Approval —
# daher müssen die Rollen immer verschiedene Identitäten sein.
DEFAULT_ENV_FILE = "/root/.hermes/.env"
GH_TOKEN_ENV = {"dev": "GITHUB_TOKEN", "reviewer": "GITHUB_REVIEWER_TOKEN"}
GITHUB_API = "https://api.github.com"
GH_CLI_TOKEN_VAR = "GH_TOKEN"  # `gh` bevorzugt GH_TOKEN vor GITHUB_TOKEN  # nosec B105
ENV_FILE_VAR = "FABRIK_ENV_FILE"
# Projekt-Token-Datei für Subprozesse: markiert FABRIK_ENV_FILE als Projekt-Override, damit
# fabrik-Code im Subprozess (`fabrik.push`, Relay-CLI) geerbte globale Token-Env-Werte ignoriert.
PROJECT_ENV_FILE_VAR = "FABRIK_PROJECT_ENV_FILE"
COMMIT_VIA_VAR = "FABRIK_COMMIT_VIA"
# GitHub-spezifische Spec-Felder (Projekt-Ebene): Token-Datei nur für dieses Projekt (andere
# Installation/Org, PAT als Reviewer) und Commit-Weg (`api` = von GitHub signierte Commits, siehe
# `apicommit`). Implementierungsdetail des GitHub-Anschlusses: gehört mit den Provider-Ports
# (PR #437) in die Config des GitHub-Adapters.
SPEC_ENV_FILE_KEY = "github_env_file"
SPEC_COMMIT_VIA_KEY = "commit_via"
COMMIT_VIA_GIT = "git"
COMMIT_VIA_API = "api"
COMMIT_VIA_MODES = (COMMIT_VIA_GIT, COMMIT_VIA_API)


@dataclass(frozen=True)
class ProjectGitHub:
    """GitHub-spezifische Einstellungen eines Projekts, die für alle Steps eines Runs gelten."""

    env_file: Path | None = None
    commit_via: str | None = None

    @classmethod
    def from_spec(cls, spec: dict | None) -> ProjectGitHub:
        spec = spec or {}
        raw = spec.get(SPEC_ENV_FILE_KEY)
        env_file = Path(os.path.expandvars(str(raw))).expanduser() if raw else None
        return cls(env_file=env_file, commit_via=spec.get(SPEC_COMMIT_VIA_KEY) or None)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProjectGitHub:
        """Gegenstück zu :meth:`env`: Einstellungen, die ein Run an seinen Subprozess vererbt hat."""
        environ = os.environ if environ is None else environ
        raw = environ.get(PROJECT_ENV_FILE_VAR, "").strip()
        return cls(env_file=Path(raw) if raw else None, commit_via=environ.get(COMMIT_VIA_VAR, "").strip() or None)

    def env(self) -> dict[str, str]:
        """Env-Zusatz für Subprozesse (Shell-Steps, Agent, Relay-CLI)."""
        env = {ENV_FILE_VAR: str(self.env_file), PROJECT_ENV_FILE_VAR: str(self.env_file)} if self.env_file else {}
        if self.commit_via:
            env[COMMIT_VIA_VAR] = self.commit_via
        return env


_project: ContextVar[ProjectGitHub | None] = ContextVar("fabrik_project_github", default=None)


def _current() -> ProjectGitHub:
    """Aktiver Kontext (`project_github`), sonst das vom Run geerbte Env (Subprozess)."""
    return _project.get() or ProjectGitHub.from_env()


@contextmanager
def project_github(spec: dict | None) -> Iterator[None]:
    """GitHub-Einstellungen der Spec (`github_env_file`, `commit_via`) im Block aktivieren.

    Gilt pro Thread/Kontext: Runs, Trigger-Polls und daraus gestartete Subprozesse
    (`subprocess_env`) eines Projekts sehen nur dessen Identitäten und Commit-Weg."""
    token = _project.set(ProjectGitHub.from_spec(spec))
    try:
        yield
    finally:
        _project.reset(token)


def token_env_file() -> Path:
    """Aktuelle Token-Datei: Projekt-Override → FABRIK_ENV_FILE → Default."""
    return _current().env_file or Path(os.environ.get(ENV_FILE_VAR, DEFAULT_ENV_FILE))


def commit_via() -> str:
    """Commit-Weg: Projekt (`commit_via`) → Env FABRIK_COMMIT_VIA (Subprozesse) → `git`."""
    return _current().commit_via or os.environ.get(COMMIT_VIA_VAR, "").strip() or COMMIT_VIA_GIT


def subprocess_env() -> dict[str, str]:
    """Env-Zusatz für Subprozesse, damit sie dieselbe Token-Datei und denselben Commit-Weg nutzen."""
    return _current().env()


def token_overrides() -> dict[str, str]:
    """Token-Variablen, die ein Subprozess mit Projekt-Token-Datei bekommt (leerer Wert: entfernen/leeren).

    Ohne `github_env_file` leer. Sonst: GH_TOKEN/GITHUB_TOKEN = Builder-Token des Projekts, der Rest
    (GITHUB_REVIEWER_TOKEN) leer — geerbte globale Werte gehören zu einer anderen Identität."""
    if not _current().env_file:
        return {}
    overrides = dict.fromkeys((GH_CLI_TOKEN_VAR, *GH_TOKEN_ENV.values()), "")
    dev = _gh_token_safe("dev")
    if dev:
        overrides[GH_CLI_TOKEN_VAR] = overrides[GH_TOKEN_ENV["dev"]] = dev
    else:
        print(f"[fabrik] Warnung: {GH_TOKEN_ENV['dev']} fehlt in {token_env_file()}", file=sys.stderr)
    return overrides


def process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Vollständige Env für einen Subprozess eines Runs: `base` (Default `os.environ`) + Projekt.

    Mit Projekt-Token-Datei werden geerbte Token-Variablen ersetzt: GH_TOKEN/GITHUB_TOKEN durch
    das Builder-Token des Projekts (für `gh` und Skripte), GITHUB_REVIEWER_TOKEN entfällt
    (fabrik liest es dann aus der Datei) — globale Werte gehören zu einer anderen Identität."""
    env = dict(os.environ if base is None else base)
    for key, value in token_overrides().items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    env.update(_current().env())
    return env


def _gh_token_safe(role: str = "dev") -> str:
    """Token für eine GitHub-Identity (pro Call frisch: Env → Token-Datei).

    Die Datei wird JEDEN Call neu gelesen, damit der externe gh-token-refresh
    (1h-Installation-Token) ohne Restart greift. Mit Projekt-Override (`project_github`)
    zählt nur dessen Datei: globale GITHUB_TOKEN-Env-Werte gehören zu einer anderen Identität."""
    env_key = GH_TOKEN_ENV.get(role)
    if env_key is None:
        raise ValueError(f"unbekannte GitHub-Rolle: {role!r} (erlaubt: {list(GH_TOKEN_ENV)})")
    tok = "" if _current().env_file else os.environ.get(env_key, "").strip()
    if tok:
        return tok
    env_file = token_env_file()
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            m = re.match(rf'^{env_key}=["\']?([^"\']+)', line)
            if m:
                return m.group(1)
    return ""


def tracking_refspec(branch: str) -> str:
    """Refspec, die `refs/remotes/origin/<branch>` aktualisiert (ohne landet ein Fetch nur in FETCH_HEAD)."""
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


# Lockfile im gemeinsamen Git-Verzeichnis (`<git-common-dir>/fabrik-repo.lock`): gilt für das Repo
# und alle seine Worktrees, auch über Prozessgrenzen (Server + CLI).
REPO_LOCK_NAME = "fabrik-repo"


def _git_common_dir(path: Path) -> Path | None:
    """Gemeinsames `.git`-Verzeichnis von `path` (Repo oder einer seiner Worktrees); None außerhalb von Git."""
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return Path(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


@contextlib.contextmanager
def repo_lock(path: Path) -> Iterator[None]:
    """Serialisiert `fetch` und `worktree add/remove` eines Repos (Issue #460).

    `worktree add` legt `.git/worktrees/<id>/HEAD` an, bevor der Checkout steht; ein paralleles
    `fetch` läuft über die HEADs aller Worktrees und bricht dann mit `bad object …/HEAD` ab.
    Nicht reentrant (Thread-Lock + flock). Außerhalb von Git: kein Lock."""
    common = _git_common_dir(path)
    if common is None:
        yield
        return
    with locked(common / REPO_LOCK_NAME):
        yield


def _git_fetch(repo_root: Path, ref: str | None = None) -> None:
    """fetch origin; bei Auth-Fehler (URL ohne Token) Retry per Token-URL.
    Expliziter Refspec — sonst landet das Fetch nur in FETCH_HEAD und
    refs/remotes/origin/<ref> bleibt stale."""
    refspec = [tracking_refspec(ref)] if ref else []
    p = subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "origin", *refspec], capture_output=True, text=True, timeout=300
    )
    if p.returncode == 0:
        return
    token = _gh_token_safe()
    if token:
        remote = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"], capture_output=True, text=True
        ).stdout.strip()
        if remote.startswith("https://"):
            auth_url = remote.replace("https://", f"https://x-access-token:{token}@", 1)
            p2 = subprocess.run(
                ["git", "-C", str(repo_root), "fetch", auth_url, *refspec], capture_output=True, text=True, timeout=300
            )
            if p2.returncode == 0:
                return
            raise GitError(f"git fetch (token) fehlgeschlagen: {_redact(p2.stderr.strip())}", p2.stderr)
    raise GitError(f"git fetch fehlgeschlagen: {_redact(p.stderr.strip())}", p.stderr)


def worktree_roots() -> list[Path]:
    """Worktree-Wurzeln: aktuelle zuerst, dann die Legacy-Wurzel im Repo (ADR 0047, Nachtrag)."""
    roots = [config.WORKTREES_DIR]
    if config.LEGACY_WORKTREES_DIR.resolve() != config.WORKTREES_DIR.resolve():
        roots.append(config.LEGACY_WORKTREES_DIR)
    return roots


def run_worktree_path(project: str, run_id: str) -> Path:
    """Worktree-Pfad eines Runs: `<WORKTREES_DIR>/<projekt>/<run_id>` (ADR 0028).

    Ein schon vorhandener Worktree unter der Legacy-Wurzel gewinnt: ein vor dem Umzug
    pausierter Run setzt dort fort, statt mit leerem Checkout neu zu beginnen.
    Gezählt wird nur ein echter Worktree (`.git`-Datei) — ein leerer/verwaister
    Legacy-Ordner (z. B. nach gecrashtem `worktree add`) darf die neue Wurzel nicht
    verdecken, sonst würde `ensure_worktree` in einem leeren Verzeichnis weiterlaufen."""
    for root in worktree_roots()[1:]:
        legacy = root / project / run_id
        if (legacy / ".git").exists():
            return legacy
    return config.WORKTREES_DIR / project / run_id


def ensure_worktree(spec: dict, run_id: str) -> Path:
    """Liefert den Worktree des Runs (Basis inkl. `subpath`).

    Jeder Run hat einen eigenen Worktree, detached auf origin/<branch> (ADR 0028).
    Existiert er bereits (Resume nach Approval), bleibt der Bestand unangetastet —
    kein fetch/reset, vor-Approval-Work darf nicht zerstört werden."""
    repo = spec.get("repo")
    if not repo:
        return Path(spec["root"])
    branch = repo["branch"]
    wt = run_worktree_path(spec["name"], run_id)
    repo_root = Path(spec["root"])
    if not wt.is_dir():
        wt.parent.mkdir(parents=True, exist_ok=True)
        # detached: kollidiert nie mit dem Haupt-Checkout (z. B. main),
        # Tests brauchen keinen Branch, nur den Stand von origin/<branch>
        with repo_lock(repo_root):  # fetch + add atomar gegen parallele Runs desselben Repos
            _git_fetch(repo_root, branch)
            p = subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "add", "--detach", str(wt), f"origin/{branch}"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        if p.returncode != 0:
            output = p.stderr.strip() or p.stdout.strip()
            raise GitError(f"worktree add fehlgeschlagen: {_redact(output)}", output)
    sub = repo.get("subpath")
    base = wt / sub if sub else wt
    if not base.is_dir():
        raise SetupError(f"Subpfad {sub} existiert nicht im Worktree {wt}")
    return base


def lease_path(wt: Path) -> Path:
    """Lease-Datei eines Run-Worktrees: Geschwisterdatei `<run_id>.lease` (nicht im Git-Tree)."""
    return wt.parent / f"{wt.name}{config.WORKTREE_LEASE_SUFFIX}"


def lease_lock(wt: Path) -> contextlib.AbstractContextManager[None]:
    """Projektweiter Lock (`<projekt>.lock`) um Lease-Schreiben/-Entfernen und Cleanup-Prüfung.

    Macht „Lease prüfen → Claim (Lease entfernen)“ atomar gegenüber Anlegen/Erneuern einer
    Lease. Das Entfernen des Worktrees selbst läuft außerhalb (unter `repo_lock`), damit
    dieser Lock nie über die `repo_lock`-Erwerbung gehalten wird (ADR 0028).
    Hostlokal (flock); mehrere Hosts/Instanzen: #280. Nicht reentrant."""
    return locked(wt.parent)


@dataclass(frozen=True)
class LeaseInfo:
    """Inhalt einer Lease-Datei (ADR 0028): Besitzer-PID, letzter Heartbeat, Besitz-Token."""

    pid: int
    heartbeat: float
    token: str = ""

    @classmethod
    def parse(cls, raw: str) -> LeaseInfo | None:
        """Validierte Lease aus JSON; None bei korruptem Inhalt (kein Objekt, PID ≤ 0,
        Heartbeat nicht endlich/≤ 0, falsche Typen) — eine korrupte Lease schützt nie."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        pid, heartbeat, token = data.get("pid"), data.get("heartbeat"), data.get("token", "")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if not _is_number(heartbeat) or not math.isfinite(heartbeat) or heartbeat <= 0:
            return None
        if not isinstance(token, str):
            return None
        return cls(pid, float(heartbeat), token)

    def to_json(self) -> str:
        return json.dumps({"pid": self.pid, "heartbeat": self.heartbeat, "token": self.token})

    def is_fresh(self, now: float) -> bool:
        """Heartbeat liegt höchstens `WORKTREE_LEASE_TIMEOUT_S` von `now` entfernt.

        Symmetrisch: ein leicht in der Zukunft liegender Heartbeat (Uhr zurückgestellt, NTP)
        zählt als frisch; ein weit in der Zukunft liegender ist korrupt und schützt nicht."""
        return math.isfinite(now) and abs(now - self.heartbeat) <= config.WORKTREE_LEASE_TIMEOUT_S


def _is_number(value: object) -> TypeGuard[int | float]:
    """int/float, aber kein bool (JSON `true` ist in Python ein int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_lease_text(wt: Path) -> str | None:
    try:
        return lease_path(wt).read_text(encoding="utf-8")
    except OSError:
        return None


def read_lease(wt: Path) -> LeaseInfo | None:
    """Lease des Worktrees; None, wenn sie fehlt oder korrupt ist."""
    raw = _read_lease_text(wt)
    return None if raw is None else LeaseInfo.parse(raw)


def write_lease(wt: Path, token: str | None = None) -> str:
    """Schreibt/erneuert die Lease (PID + Heartbeat) des Worktrees atomar (ADR 0028)."""
    path = lease_path(wt)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = LeaseInfo(os.getpid(), time.time(), token or uuid.uuid4().hex)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(lease.to_json(), encoding="utf-8")
    tmp.replace(path)
    return lease.token


def release_lease(wt: Path, token: str | None = None) -> None:
    """Entfernt die eigene Lease: mit `token` nur bei gleichem Besitz-Token, sonst nur bei eigener PID."""
    lease = read_lease(wt)
    if lease is None:
        return
    owned = lease.token == token if token is not None else lease.pid == os.getpid()
    if owned:
        lease_path(wt).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Prozess existiert, gehört nur einem anderen User
    return True


def lease_held_by_other(wt: Path, now: float | None = None) -> bool:
    """True, wenn ein *anderer* lebender Prozess den Worktree per frischer Lease hält.

    Fehlende/korrupte Lease, eigene PID, tote PID oder abgelaufener Heartbeat = nicht gehalten."""
    lease = read_lease(wt)
    if lease is None or lease.pid == os.getpid():
        return False
    if not lease.is_fresh(time.time() if now is None else now):
        return False
    return _pid_alive(lease.pid)


class WorktreeLease:
    """Hält die Lease eines Run-Worktrees per Heartbeat-Thread, bis `release()` (ADR 0028).

    `start()` vor dem Anlegen des Worktrees aufrufen: so sieht ein fremder Cleanup nie
    einen Worktree des Runs ohne Lease. Alle Schreibzugriffe laufen unter `lease_lock`."""

    def __init__(self, wt: Path):
        self._wt = wt
        self._token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"fabrik-lease-{wt.name}")

    def start(self) -> None:
        with lease_lock(self._wt):
            write_lease(self._wt, self._token)
        self._thread.start()

    def _renew(self) -> bool:
        """Erneuert die Lease, solange sie uns gehört; False, wenn sie entfernt oder übernommen wurde.

        Eine korrupte (vorhandene, unlesbare) Lease gehört niemandem und wird überschrieben."""
        with lease_lock(self._wt):
            lease = read_lease(self._wt)
            lost = lease.token != self._token if lease is not None else not lease_path(self._wt).exists()
            if lost:
                return False
            write_lease(self._wt, self._token)
        return True

    def _beat(self) -> None:
        while not self._stop.wait(config.WORKTREE_LEASE_HEARTBEAT_S):
            # Schreibfehler ignorieren: nächster Tick versucht es erneut, Ablauf nach Timeout ist der Fallback
            with contextlib.suppress(OSError, TypeError):
                if not self._renew():
                    return  # verloren (abgelaufen und weggeräumt): nicht neu anlegen

    def release(self) -> None:
        """Stoppt den Heartbeat und entfernt die Lease-Datei (idempotent)."""
        self._stop.set()
        if self._thread.is_alive():
            # Warten, falls ein Dateisystemzugriff gerade noch läuft; sonst könnte
            # der Heartbeat nach dem Entfernen die Lease erneut schreiben.
            self._thread.join()
        with lease_lock(self._wt):
            release_lease(self._wt, self._token)


def remove_worktree(repo_root: Path, wt: Path) -> bool:
    """Entfernt einen Worktree: `git worktree remove`, sonst rmtree + prune.

    Gibt zurück, ob das Verzeichnis danach weg ist. Subprocess-Fehler
    (Timeout, git fehlt, nonzero exit) werden nicht verschluckt. Läuft unter `repo_lock`
    (serialisiert `worktree remove` gegen parallele `fetch`/`worktree add`, Issue #460).
    Die Lease-Datei wird auch bei Fehlern entfernt — auch eine abgelaufene, fremde oder
    korrupte —, sofern sie sich währenddessen nicht geändert hat (sonst wurde sie
    erneuert/übernommen und bleibt)."""
    snapshot = _read_lease_text(wt)
    try:
        if not wt.exists():
            return True
        with repo_lock(repo_root):
            p = subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if p.returncode != 0:
                shutil.rmtree(wt, ignore_errors=True)
                prune = subprocess.run(
                    ["git", "-C", str(repo_root), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if prune.returncode != 0:
                    raise subprocess.CalledProcessError(
                        prune.returncode,
                        prune.args,
                        output=prune.stdout,
                        stderr=prune.stderr,
                    )
                raise subprocess.CalledProcessError(p.returncode, p.args, output=p.stdout, stderr=p.stderr)
        return not wt.exists()
    finally:
        if snapshot is not None and _read_lease_text(wt) == snapshot:
            lease_path(wt).unlink(missing_ok=True)


def cleanup_run_worktree(project: str, run_id: str) -> bool:
    """Räumt den Worktree eines beendeten Runs auf; True, wenn er danach weg ist.

    Unbekanntes Projekt, Spec ohne `repo:` oder fehlender Worktree = No-Op (True)."""
    from .specs import load_projects

    spec = load_projects().get(project)
    if spec and not spec.get("_error") and spec.get("repo"):
        return remove_worktree(Path(spec["root"]), run_worktree_path(project, run_id))
    return True


def cleanup_orphaned_worktrees(active_run_ids: set[str] | None = None) -> list[Path]:
    """Entfernt Worktrees verwaister Runs (Crash/Kill) — beim Start aufrufen (ADR 0028).

    Behalten werden Worktrees pausierter Runs (`needs_approval`) und `active_run_ids`;
    ein `running`-Record ohne aktiven Prozess gilt als gecrasht. Worktrees mit frischer
    Lease eines anderen lebenden Prozesses bleiben ebenfalls (Multi-Process, ADR 0028).
    Die Lease-Prüfung + der Claim (Lease-Entfernung) laufen unter `lease_lock`; das
    Entfernen des Worktrees (`remove_worktree`, unter `repo_lock`) erfolgt außerhalb,
    damit `lease_lock` nie über die `repo_lock`-Erwerbung gehalten wird.
    Legacy-Worktrees (`<projekt>/` selbst ist ein Worktree) werden ebenfalls entfernt,
    ebenso nicht gehaltene Lease-Dateien ohne Worktree. Durchsucht alle `worktree_roots()`.
    Gibt die entfernten Worktree-Pfade zurück."""
    from .runs import get_run
    from .specs import load_projects

    removed: list[Path] = []
    keep = active_run_ids or set()
    proj_dirs = [p for root in worktree_roots() if root.is_dir() for p in sorted(root.iterdir()) if p.is_dir()]
    if not proj_dirs:
        return removed
    specs = load_projects()
    for proj_dir in proj_dirs:
        spec = specs.get(proj_dir.name)
        if not spec or spec.get("_error") or not spec.get("repo"):
            continue
        repo_root = Path(spec["root"])
        if (proj_dir / ".git").exists():  # Legacy: ein gemeinsamer Worktree pro Projekt
            remove_worktree(repo_root, proj_dir)
            removed.append(proj_dir)
            continue
        for wt in sorted(p for p in proj_dir.iterdir() if p.is_dir()):
            if wt.name in keep:
                continue
            record = get_run(wt.name, proj_dir.name)
            if record and record.get("status") == "needs_approval":
                continue
            with lease_lock(wt):  # Claim: Prüfung + Lease-Entfernung atomar gegenüber Anlegen/Erneuern
                if lease_held_by_other(wt):
                    continue
                lease_path(wt).unlink(missing_ok=True)
            # Entfernen außerhalb von `lease_lock`: `remove_worktree` hält selbst `repo_lock`
            # (kann bis zu ~600 s blockieren) — `lease_lock` darf nie darüber gehalten werden (ADR 0028).
            remove_worktree(repo_root, wt)
            removed.append(wt)
        _remove_orphaned_leases(proj_dir, keep)
    return removed


def _remove_orphaned_leases(proj_dir: Path, keep: set[str]) -> None:
    """Lease-Dateien ohne Worktree entfernen (z. B. Crash zwischen Lease und `ensure_worktree`).

    Eine frische Lease eines anderen lebenden Prozesses bleibt: dessen Run legt den
    Worktree womöglich gerade an."""
    for path in sorted(proj_dir.glob(f"*{config.WORKTREE_LEASE_SUFFIX}")):
        wt = path.with_name(path.name.removesuffix(config.WORKTREE_LEASE_SUFFIX))
        if wt.name in keep or wt.exists():
            continue
        with lease_lock(wt):
            if not wt.exists() and not lease_held_by_other(wt):
                path.unlink(missing_ok=True)


def _worktree_sync_check(spec: dict, base: Path) -> dict:
    """Branch/Head-Info für Logs & Reports.

    Zwei separate Calls: `--abbrev-ref` gilt global für alle Refs, daher würde
    `rev-parse --abbrev-ref HEAD HEAD` für BEIDE das Branch-Name liefern (statt
    Branch + Commit-SHA)."""
    branch = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    head = subprocess.run(["git", "-C", str(base), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"branch": branch or "?", "head": head or "?"}


def _git_out(workdir: Path, *args: str) -> str | None:
    p = subprocess.run(["git", "-C", str(workdir), *args], capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else None


def exclude_dir_locally(workdir: Path, dirname: str) -> None:
    """`<dirname>/` des Workdirs lokal git-ignorieren (`info/exclude`; No-Op außerhalb von Git).

    Verankert auf den Workdir-Pfad im Repo (`/<prefix><dirname>/`), damit gleichnamige
    Projektverzeichnisse (z. B. Terraform-`state/`) nicht still ignoriert werden."""
    exclude, prefix = (
        _git_out(workdir, "rev-parse", "--git-path", "info/exclude"),
        _git_out(workdir, "rev-parse", "--show-prefix"),
    )
    if exclude is None or prefix is None:
        return
    path = Path(exclude) if Path(exclude).is_absolute() else workdir / exclude
    pattern = f"/{prefix}{dirname}/"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if pattern in lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if lines and lines[-1] else "") + pattern + "\n")


GH_WRAPPER_DIR = config.STATE_DIR / "gh-wrapper"
_GH_WRAPPER_TEMPLATE = """#!/bin/sh
# fabrik: gh mit frischem Identity-Token aus FABRIK_ENV_FILE (Token lebt nur 1h, ADR 0010).
f="${{FABRIK_ENV_FILE:-{default_env_file}}}"
tok=$(sed -n -E "s/^{env_key}=[\\"']?([^\\"']+).*/\\1/p" "$f" 2>/dev/null | head -n 1)
if [ -n "$tok" ]; then GH_TOKEN=$tok; GITHUB_TOKEN=$tok; export GH_TOKEN GITHUB_TOKEN; fi
PATH={real_path}
export PATH
exec {real_gh} "$@"
"""


def _gh_wrapper_dir(role: str, path: str) -> Path | None:
    """Verzeichnis mit einem `gh`-Wrapper, der das Token pro Call frisch aus FABRIK_ENV_FILE liest.

    Die Env eines laufenden Agent-Prozesses ist fix; Runs mit `timeout` > 1h überlebten sonst
    ihr 1h-Token. Fehlt die Env-Datei oder der Key, gilt das Token aus der Prozess-Env.
    None, wenn kein echtes `gh` im PATH liegt."""
    wrapper_dir = GH_WRAPPER_DIR / role
    clean_path = os.pathsep.join(p for p in path.split(os.pathsep) if p and Path(p) != wrapper_dir)
    real_gh = shutil.which("gh", path=clean_path)
    if not real_gh:
        return None
    script = _GH_WRAPPER_TEMPLATE.format(
        default_env_file=DEFAULT_ENV_FILE,
        env_key=GH_TOKEN_ENV[role],
        real_path=shlex.quote(clean_path),
        real_gh=shlex.quote(real_gh),
    )
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    target = wrapper_dir / "gh"
    if not target.is_file() or target.read_text() != script:
        tmp = wrapper_dir / f".gh.{os.getpid()}"
        tmp.write_text(script)
        tmp.chmod(0o755)
        tmp.replace(target)  # atomar: parallele Runs sehen nie ein halbes Skript
    return wrapper_dir


def github_env(role: str) -> dict[str, str]:
    """Prozess-Env, unter dem `gh`/`git` (Credential-Helper `gh`) als Identity `role` laufen.

    Für Agent-Subprozesse (`github_identity:`): ohne diese Variablen nutzt `gh` den
    lokalen Login des OS-Users. RuntimeError, wenn das Token der Rolle fehlt.
    Zusätzlich kommt ein `gh`-Wrapper vorn in den PATH, der das Token pro Call frisch aus
    FABRIK_ENV_FILE liest — so überlebt der Agent-Prozess auch Laufzeiten > 1h."""
    tok = _gh_token(role)
    env = {"GH_TOKEN": tok, "GITHUB_TOKEN": tok, **subprocess_env()}
    path = os.environ.get("PATH", os.defpath)
    wrapper_dir = _gh_wrapper_dir(role, path)
    if wrapper_dir:
        env["PATH"] = f"{wrapper_dir}{os.pathsep}{path}"
    return env


def _gh_token(role: str = "dev") -> str:
    tok = _gh_token_safe(role)
    if not tok:
        env_key = GH_TOKEN_ENV[role]
        raise SetupError(f"{env_key} nicht verfügbar (Env oder {token_env_file()})")
    return tok


def _gh_request(req: urllib.request.Request) -> bytes:
    """urlopen mit Retry bei transienten Netzwerkfehlern (DNS-Flakes,
    Connection Reset). HTTPError (4xx/5xx) ist eine Antwort, kein Flake —
    wird NICHT geretried."""
    for attempt in range(config.GH_RETRIES):
        try:
            with httpsafe.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt >= config.GH_RETRIES - 1:
                raise
            time.sleep(config.GH_RETRY_DELAY_S)
    raise AssertionError("unreachable")  # pragma: no cover


def _gh_api(method: str, url: str, token: str, payload: dict | None = None, accept: str | None = None) -> dict:
    """GitHub-REST-Call. `accept` überschreibt den JSON-Accept-Header
    (z. B. "application/vnd.github.diff" für PR-Diffs)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": accept or "application/vnd.github+json",
            "User-Agent": "fabrik",
            "Content-Type": "application/json",
        },
    )
    body = _gh_request(req).decode()
    return json.loads(body) if body else {}


GH_DEPENDENCIES_PER_PAGE = 100  # GitHub-Maximum; mehr Blocker pro Issue sind unrealistisch
GH_DEPENDENCY_KINDS = ("blocked_by", "blocking")


def _gh_dependencies(repo: str, number: int | str, token: str, kind: str = "blocked_by") -> list[dict]:
    """Native Issue-Dependencies (ADR 0031): `blocked_by` = Issues, die `number`
    blockieren; `blocking` = Issues, die `number` blockiert. Issue-Objekte inkl.
    `id`, `number`, `state`, `state_reason`."""
    if kind not in GH_DEPENDENCY_KINDS:
        raise ValueError(f"unbekannte Dependency-Art: {kind!r} (erlaubt: {GH_DEPENDENCY_KINDS})")
    url = f"https://api.github.com/repos/{repo}/issues/{number}/dependencies/{kind}?per_page={GH_DEPENDENCIES_PER_PAGE}"
    return cast(list[dict], _gh_api("GET", url, token) or [])


def _gh_diff(repo: str, pr_number: int, token: str) -> str:
    """PR-Diff als Text (Accept: application/vnd.github.diff)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        method="GET",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.diff",
            "User-Agent": "fabrik",
        },
    )
    return _gh_request(req).decode()


def _http_post(url: str, payload: dict, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "fabrik"},
        method="POST",
    )
    try:
        with httpsafe.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return 0, str(e)
