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

from . import config, httpsafe, netretry
from .failures import GitError, SetupError
from .filelock import locked
from .i18n import t
from .secrets import _redact

# Dual identity (ADR 0010): "dev" = builder app (creates/merges PRs),
# "reviewer" = reviewer app (approves PRs). GitHub forbids self-approval —
# so the roles must always be different identities.
DEFAULT_ENV_FILE = config.DEFAULT_ENV_FILE
GH_TOKEN_ENV = {"dev": "GITHUB_TOKEN", "reviewer": "GITHUB_REVIEWER_TOKEN"}
GITHUB_API = "https://api.github.com"
GH_CLI_TOKEN_VAR = "GH_TOKEN"  # `gh` bevorzugt GH_TOKEN vor GITHUB_TOKEN  # nosec B105
ENV_FILE_VAR = "FABRIKANT_ENV_FILE"
# Project token file for subprocesses: marks FABRIKANT_ENV_FILE as a project override so that
# fabrikant code in the subprocess (`fabrikant.push`, relay CLI) ignores inherited global token env values.
PROJECT_ENV_FILE_VAR = "FABRIKANT_PROJECT_ENV_FILE"
COMMIT_VIA_VAR = "FABRIKANT_COMMIT_VIA"
# GitHub-specific spec fields (project level): token file only for this project (other
# installation/org, PAT as reviewer) and commit path (`api` = commits signed by GitHub, see
# `apicommit`). Implementation detail of the GitHub integration: belongs with the provider ports
# (PR #437) in the GitHub adapter's config.
SPEC_ENV_FILE_KEY = "github_env_file"
SPEC_COMMIT_VIA_KEY = "commit_via"
COMMIT_VIA_GIT = "git"
COMMIT_VIA_API = "api"
COMMIT_VIA_MODES = (COMMIT_VIA_GIT, COMMIT_VIA_API)


@dataclass(frozen=True)
class ProjectGitHub:
    """GitHub-specific settings of a project that apply to all steps of a run."""

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
        """Counterpart to :meth:`env`: settings a run has handed down to its subprocess."""
        environ = os.environ if environ is None else environ
        raw = environ.get(PROJECT_ENV_FILE_VAR, "").strip()
        return cls(env_file=Path(raw) if raw else None, commit_via=environ.get(COMMIT_VIA_VAR, "").strip() or None)

    def env(self) -> dict[str, str]:
        """Env addition for subprocesses (shell steps, agent, relay CLI)."""
        env = {ENV_FILE_VAR: str(self.env_file), PROJECT_ENV_FILE_VAR: str(self.env_file)} if self.env_file else {}
        if self.commit_via:
            env[COMMIT_VIA_VAR] = self.commit_via
        return env


_project: ContextVar[ProjectGitHub | None] = ContextVar("fabrikant_project_github", default=None)


def _current() -> ProjectGitHub:
    """Active context (`project_github`), otherwise the env inherited from the run (subprocess)."""
    return _project.get() or ProjectGitHub.from_env()


@contextmanager
def project_github(spec: dict | None) -> Iterator[None]:
    """Activate the spec's GitHub settings (`github_env_file`, `commit_via`) within the block.

    Applies per thread/context: runs, trigger polls and subprocesses started from them
    (`subprocess_env`) of a project see only its identities and commit path."""
    token = _project.set(ProjectGitHub.from_spec(spec))
    try:
        yield
    finally:
        _project.reset(token)


def token_env_file() -> Path:
    """Aktuelle Token-Datei: Projekt-Override → FABRIKANT_ENV_FILE → Default."""
    return _current().env_file or Path(os.environ.get(ENV_FILE_VAR, DEFAULT_ENV_FILE))


def commit_via() -> str:
    """Commit-Weg: Projekt (`commit_via`) → Env FABRIKANT_COMMIT_VIA (Subprozesse) → `git`."""
    return _current().commit_via or os.environ.get(COMMIT_VIA_VAR, "").strip() or COMMIT_VIA_GIT


def subprocess_env() -> dict[str, str]:
    """Env addition for subprocesses so they use the same token file and the same commit path."""
    return _current().env()


def cleared_tokens() -> dict[str, str]:
    """Alle GitHub-Token-Variablen leer — Subprozess ohne GitHub-Identität (`gh` ignoriert leere Werte)."""
    return dict.fromkeys((GH_CLI_TOKEN_VAR, *GH_TOKEN_ENV.values()), "")


def token_overrides() -> dict[str, str]:
    """Token variables a subprocess with a project token file gets (empty value: remove/clear).

    Empty without `github_env_file`. Otherwise: GH_TOKEN/GITHUB_TOKEN = the project's builder token, the rest
    (GITHUB_REVIEWER_TOKEN) empty — inherited global values belong to a different identity."""
    if not _current().env_file:
        return {}
    overrides = cleared_tokens()
    dev = _gh_token_safe("dev")
    if dev:
        overrides[GH_CLI_TOKEN_VAR] = overrides[GH_TOKEN_ENV["dev"]] = dev
    else:
        print(t("gitops.fabrikant_warning_missing_in", v1=GH_TOKEN_ENV["dev"], v2=token_env_file()), file=sys.stderr)
    return overrides


def process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Complete env for a run's subprocess: `base` (default `os.environ`) + project.

    With a project token file, inherited token variables are replaced: GH_TOKEN/GITHUB_TOKEN by
    the project's builder token (for `gh` and scripts), GITHUB_REVIEWER_TOKEN is dropped
    (fabrikant then reads it from the file) — global values belong to a different identity."""
    env = dict(os.environ if base is None else base)
    for key, value in token_overrides().items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    env.update(_current().env())
    return env


def _gh_token_safe(role: str = "dev") -> str:
    """Token for a GitHub identity (fresh per call: env → token file).

    The file is re-read on EVERY call so the external gh-token-refresh
    (1h installation token) takes effect without a restart. With a project override (`project_github`)
    only its file counts: global GITHUB_TOKEN env values belong to a different identity."""
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
    """Refspec that updates `refs/remotes/origin/<branch>` (without it a fetch lands only in FETCH_HEAD)."""
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


# Lock file in the shared git directory (`<git-common-dir>/fabrikant-repo.lock`): applies to the repo
# and all its worktrees, also across process boundaries (server + CLI).
REPO_LOCK_NAME = "fabrikant-repo"


def _git_common_dir(path: Path) -> Path | None:
    """Shared `.git` directory of `path` (repo or one of its worktrees); None outside git."""
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return Path(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


@contextlib.contextmanager
def repo_lock(path: Path) -> Iterator[None]:
    """Serializes `fetch` and `worktree add/remove` of a repo (issue #460).

    `worktree add` creates `.git/worktrees/<id>/HEAD` before the checkout exists; a parallel
    `fetch` runs over the HEADs of all worktrees and then aborts with `bad object …/HEAD`.
    Not reentrant (thread lock + flock). Outside git: no lock."""
    common = _git_common_dir(path)
    if common is None:
        yield
        return
    with locked(common / REPO_LOCK_NAME):
        yield


def _git_fetch(repo_root: Path, ref: str | None = None) -> None:
    """fetch origin; on auth error (URL without token) retry via token URL.
    Explicit refspec — otherwise the fetch lands only in FETCH_HEAD and
    refs/remotes/origin/<ref> stays stale."""
    refspec = [tracking_refspec(ref)] if ref else []
    p = netretry.run_git(["git", "-C", str(repo_root), "fetch", "origin", *refspec], timeout=300)
    if p.returncode == 0:
        return
    token = _gh_token_safe()
    if token:
        remote = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"], capture_output=True, text=True
        ).stdout.strip()
        if remote.startswith("https://"):
            auth_url = remote.replace("https://", f"https://x-access-token:{token}@", 1)
            p2 = netretry.run_git(["git", "-C", str(repo_root), "fetch", auth_url, *refspec], timeout=300)
            if p2.returncode == 0:
                return
            raise GitError(f"git fetch (token) fehlgeschlagen: {_redact(p2.stderr.strip())}", p2.stderr)
    raise GitError(f"git fetch fehlgeschlagen: {_redact(p.stderr.strip())}", p.stderr)


def worktree_roots() -> list[Path]:
    """Worktree roots: current first, then the legacy root in the repo (ADR 0047, addendum)."""
    roots = [config.WORKTREES_DIR]
    if config.LEGACY_WORKTREES_DIR.resolve() != config.WORKTREES_DIR.resolve():
        roots.append(config.LEGACY_WORKTREES_DIR)
    return roots


def run_worktree_path(project: str, run_id: str) -> Path:
    """Worktree path of a run: `<WORKTREES_DIR>/<project>/<run_id>` (ADR 0028).

    An already existing worktree under the legacy root wins: a run paused before the move
    resumes there instead of starting over with an empty checkout.
    Only a real worktree (`.git` file) counts — an empty/orphaned
    legacy folder (e.g. after a crashed `worktree add`) must not
    shadow the new root, otherwise `ensure_worktree` would continue in an empty directory."""
    for root in worktree_roots()[1:]:
        legacy = root / project / run_id
        if (legacy / ".git").exists():
            return legacy
    return config.WORKTREES_DIR / project / run_id


def ensure_worktree(spec: dict, run_id: str) -> Path:
    """Returns the run's worktree (base incl. `subpath`).

    Every run has its own worktree, detached at origin/<branch> (ADR 0028).
    If it already exists (resume after approval), the existing state stays untouched —
    no fetch/reset, pre-approval work must not be destroyed."""
    repo = spec.get("repo")
    if not repo:
        return Path(spec["root"])
    branch = repo["branch"]
    wt = run_worktree_path(spec["name"], run_id)
    repo_root = Path(spec["root"])
    if not wt.is_dir():
        wt.parent.mkdir(parents=True, exist_ok=True)
        # detached: never collides with the main checkout (e.g. main),
        # tests need no branch, only the state of origin/<branch>
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
        raise SetupError(t("gitops.subpath_does_not_exist_in", sub=sub, wt=wt))
    return base


def lease_path(wt: Path) -> Path:
    """Lease file of a run worktree: sibling file `<run_id>.lease` (not in the git tree)."""
    return wt.parent / f"{wt.name}{config.WORKTREE_LEASE_SUFFIX}"


def lease_lock(wt: Path) -> contextlib.AbstractContextManager[None]:
    """Project-wide lock (`<project>.lock`) around lease writing/removal and cleanup check.

    Makes "check lease → claim (remove lease)" atomic with respect to creating/renewing a
    lease. The removal of the worktree itself runs outside (under `repo_lock`) so
    this lock is never held across the `repo_lock` acquisition (ADR 0028).
    Host-local (flock); multiple hosts/instances: #280. Not reentrant."""
    return locked(wt.parent)


@dataclass(frozen=True)
class LeaseInfo:
    """Inhalt einer Lease-Datei (ADR 0028): Besitzer-PID, letzter Heartbeat, Besitz-Token."""

    pid: int
    heartbeat: float
    token: str = ""

    @classmethod
    def parse(cls, raw: str) -> LeaseInfo | None:
        """Validated lease from JSON; None on corrupt content (not an object, PID ≤ 0,
        heartbeat not finite/≤ 0, wrong types) — a corrupt lease never protects."""
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
        """Heartbeat is at most `WORKTREE_LEASE_TIMEOUT_S` away from `now`.

        Symmetric: a heartbeat slightly in the future (clock set back, NTP)
        counts as fresh; one far in the future is corrupt and does not protect."""
        return math.isfinite(now) and abs(now - self.heartbeat) <= config.WORKTREE_LEASE_TIMEOUT_S


def _is_number(value: object) -> TypeGuard[int | float]:
    """int/float, but not bool (JSON `true` is an int in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_lease_text(wt: Path) -> str | None:
    try:
        return lease_path(wt).read_text(encoding="utf-8")
    except OSError:
        return None


def read_lease(wt: Path) -> LeaseInfo | None:
    """Lease of the worktree; None if missing or corrupt."""
    raw = _read_lease_text(wt)
    return None if raw is None else LeaseInfo.parse(raw)


def write_lease(wt: Path, token: str | None = None) -> str:
    """Writes/renews the worktree's lease (PID + heartbeat) atomically (ADR 0028)."""
    path = lease_path(wt)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = LeaseInfo(os.getpid(), time.time(), token or uuid.uuid4().hex)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(lease.to_json(), encoding="utf-8")
    tmp.replace(path)
    return lease.token


def release_lease(wt: Path, token: str | None = None) -> None:
    """Removes the own lease: with `token` only on the same ownership token, otherwise only on own PID."""
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
        return True  # Process exists, but belongs to another user only
    return True


def lease_held_by_other(wt: Path, now: float | None = None) -> bool:
    """True if *another* live process holds the worktree via a fresh lease.

    Missing/corrupt lease, own PID, dead PID or expired heartbeat = not held."""
    lease = read_lease(wt)
    if lease is None or lease.pid == os.getpid():
        return False
    if not lease.is_fresh(time.time() if now is None else now):
        return False
    return _pid_alive(lease.pid)


class WorktreeLease:
    """Holds the lease of a run worktree via heartbeat thread until `release()` (ADR 0028).

    Call `start()` before creating the worktree: that way a foreign cleanup never sees
    a run's worktree without a lease. All write accesses run under `lease_lock`."""

    def __init__(self, wt: Path):
        self._wt = wt
        self._token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"fabrikant-lease-{wt.name}")

    def start(self) -> None:
        with lease_lock(self._wt):
            write_lease(self._wt, self._token)
        self._thread.start()

    def _renew(self) -> bool:
        """Renews the lease as long as it belongs to us; False if it was removed or taken over.

        A corrupt (existing, unreadable) lease belongs to nobody and is overwritten."""
        with lease_lock(self._wt):
            lease = read_lease(self._wt)
            lost = lease.token != self._token if lease is not None else not lease_path(self._wt).exists()
            if lost:
                return False
            write_lease(self._wt, self._token)
        return True

    def _beat(self) -> None:
        while not self._stop.wait(config.WORKTREE_LEASE_HEARTBEAT_S):
            # Ignore write errors: the next tick tries again, expiry after timeout is the fallback
            with contextlib.suppress(OSError, TypeError):
                if not self._renew():
                    return  # lost (expired and cleaned up): do not recreate

    def release(self) -> None:
        """Stops the heartbeat and removes the lease file (idempotent)."""
        self._stop.set()
        if self._thread.is_alive():
            # Wait in case a file-system access is still running; otherwise
            # the heartbeat could write the lease again after removal.
            self._thread.join()
        with lease_lock(self._wt):
            release_lease(self._wt, self._token)


def remove_worktree(repo_root: Path, wt: Path) -> bool:
    """Removes a worktree: `git worktree remove`, otherwise rmtree + prune.

    Returns whether the directory is gone afterwards. Subprocess errors
    (timeout, git missing, nonzero exit) are not swallowed. Runs under `repo_lock`
    (serializes `worktree remove` against parallel `fetch`/`worktree add`, issue #460).
    The lease file is removed even on errors — also an expired, foreign or
    corrupt one — provided it did not change in the meantime (otherwise it was
    renewed/taken over and stays)."""
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
    """Cleans up the worktree of a finished run; True if it is gone afterwards.

    Unknown project, spec without `repo:` or missing worktree = no-op (True)."""
    from .specs import load_projects

    spec = load_projects().get(project)
    if spec and not spec.get("_error") and spec.get("repo"):
        return remove_worktree(Path(spec["root"]), run_worktree_path(project, run_id))
    return True


def cleanup_orphaned_worktrees(active_run_ids: set[str] | None = None) -> list[Path]:
    """Removes worktrees of orphaned runs (crash/kill) — call at startup (ADR 0028).

    Worktrees of paused runs (`needs_approval`) and `active_run_ids` are kept;
    a `running` record without an active process counts as crashed. Worktrees with a fresh
    lease of another live process also stay (multi-process, ADR 0028).
    The lease check + the claim (lease removal) run under `lease_lock`; the
    removal of the worktree (`remove_worktree`, under `repo_lock`) happens outside,
    so `lease_lock` is never held across the `repo_lock` acquisition.
    Legacy worktrees (`<project>/` itself is a worktree) are removed as well,
    as are unheld lease files without a worktree. Searches all `worktree_roots()`.
    Returns the removed worktree paths."""
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
            with lease_lock(wt):  # Claim: check + lease removal atomic with respect to create/renew
                if lease_held_by_other(wt):
                    continue
                lease_path(wt).unlink(missing_ok=True)
            # Remove outside `lease_lock`: `remove_worktree` itself holds `repo_lock`
            # (can block up to ~600 s) — `lease_lock` must never be held across it (ADR 0028).
            remove_worktree(repo_root, wt)
            removed.append(wt)
        _remove_orphaned_leases(proj_dir, keep)
    return removed


def _remove_orphaned_leases(proj_dir: Path, keep: set[str]) -> None:
    """Remove lease files without a worktree (e.g. crash between lease and `ensure_worktree`).

    A fresh lease of another live process stays: its run may just be
    creating the worktree."""
    for path in sorted(proj_dir.glob(f"*{config.WORKTREE_LEASE_SUFFIX}")):
        wt = path.with_name(path.name.removesuffix(config.WORKTREE_LEASE_SUFFIX))
        if wt.name in keep or wt.exists():
            continue
        with lease_lock(wt):
            if not wt.exists() and not lease_held_by_other(wt):
                path.unlink(missing_ok=True)


def _worktree_sync_check(spec: dict, base: Path) -> dict:
    """Branch/head info for logs & reports.

    Two separate calls: `--abbrev-ref` applies globally to all refs, so
    `rev-parse --abbrev-ref HEAD HEAD` would return the branch name for BOTH
    (instead of branch + commit SHA)."""
    branch = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    head = subprocess.run(["git", "-C", str(base), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"branch": branch or "?", "head": head or "?"}


def _git_out(workdir: Path, *args: str) -> str | None:
    p = subprocess.run(["git", "-C", str(workdir), *args], capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else None


def exclude_dir_locally(workdir: Path, dirname: str) -> None:
    """Locally git-ignore the workdir's `<dirname>/` (`info/exclude`; no-op outside git).

    Anchored to the workdir path in the repo (`/<prefix><dirname>/`) so same-named
    project directories (e.g. Terraform `state/`) are not silently ignored."""
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
# fabrikant: gh mit frischem Identity-Token aus FABRIKANT_ENV_FILE (Token lebt nur 1h, ADR 0010).
f="${{FABRIKANT_ENV_FILE:-{default_env_file}}}"
tok=$(sed -n -E "s/^{env_key}=[\\"']?([^\\"']+).*/\\1/p" "$f" 2>/dev/null | head -n 1)
if [ -n "$tok" ]; then GH_TOKEN=$tok; GITHUB_TOKEN=$tok; export GH_TOKEN GITHUB_TOKEN; fi
PATH={real_path}
export PATH
exec {real_gh} "$@"
"""


def _gh_wrapper_dir(role: str, path: str) -> Path | None:
    """Directory with a `gh` wrapper that reads the token fresh per call from FABRIKANT_ENV_FILE.

    The env of a running agent process is fixed; runs with `timeout` > 1h would otherwise outlive
    their 1h token. If the env file or the key is missing, the token from the process env applies.
    None if no real `gh` is in the PATH."""
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
        tmp.replace(target)  # atomic: parallel runs never see a half-written script
    return wrapper_dir


def github_env(role: str) -> dict[str, str]:
    """Process env under which `gh`/`git` (credential helper `gh`) run as identity `role`.

    For agent subprocesses (`github_identity:`): without these variables `gh` uses the
    OS user's local login. RuntimeError if the role's token is missing.
    In addition a `gh` wrapper is put at the front of the PATH that reads the token fresh per call from
    FABRIKANT_ENV_FILE — so the agent process also survives runtimes > 1h."""
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
        raise SetupError(t("gitops.not_available_env_or", env_key=env_key, v2=token_env_file()))
    return tok


def _gh_request(req: urllib.request.Request) -> bytes:
    """urlopen with retry on transient network errors (DNS flakes,
    connection reset). HTTPError (4xx/5xx) is an answer, not a flake —
    NOT retried."""
    waits = netretry.delays()
    for attempt in range(len(waits) + 1):
        try:
            with httpsafe.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == len(waits):
                raise
            time.sleep(waits[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


def _gh_api(method: str, url: str, token: str, payload: dict | None = None, accept: str | None = None) -> dict:
    """GitHub REST call. `accept` overrides the JSON Accept header
    (e.g. "application/vnd.github.diff" for PR diffs)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": accept or "application/vnd.github+json",
            "User-Agent": "fabrikant",
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
            "User-Agent": "fabrikant",
        },
    )
    return _gh_request(req).decode()


def _http_post(url: str, payload: dict, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "fabrikant"},
        method="POST",
    )
    try:
        with httpsafe.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()[:500]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return 0, str(e)
