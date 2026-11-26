"""Per-run agent sandbox — one-shot CLI agents in a Docker container (ADR 0054, under revision).

One-shot CLI agents (claude-code, codex, copilot, opencode) by default run as a
plain subprocess on the host. With `FABRIKANT_SANDBOX=1`,
`OneShotCliBackend.send_prompt` wraps the agent command in `docker run` with
hardening:


  - own container per run (name `fabrikant-agent-<sid>`, `--rm`, `--init`),
  - `--network fabrikant-sandbox` (ADR 0051: `internal` — no direct
    internet access, egress only via the allowlist proxy; loopback
    (`NO_PROXY`) excepted),
  - `--cap-drop ALL` + `--security-opt no-new-privileges`,
  - resource caps (`--memory`, `--cpus`, `--pids-limit`),
  - read-only root fs + tmpfs for `/tmp`, `/home/agent` (HOME) and the working clone `/work`,
  - env only as explicit `-e` flags (already scrubbed per ADR 0050);
    host path variables (HOME, PATH, XDG_*, TMPDIR, …) are STRIPPED and
    replaced by container defaults (overrides come LAST → win).

No mount of the worktree. The container does not see the host, exchange only via git:

  1. Host (`prepare_exchange`): state of the worktree (HEAD + uncommitted changes,
     via a temporary index — index and HEAD of the worktree stay untouched)
     as `git bundle` → read-only mount `/in/base.bundle`.
  2. Container (`entrypoint_script`): own clone in `/work/repo`, the agent works there;
     afterwards `git add -A` + commit + bundle to `/out/result.bundle` (own,
     empty exchange directory — the only writable mount).
  3. Host (`apply_result`): `git fetch` from the bundle with object check, result must
     descend from the base commit, no path with a `.git` component, then the diff computed
     by the host via `git apply` into the worktree — result like an agent on the
     host that does not commit. The agent's own commits are squashed in the process.

Locally ignored files stay outside — with one exception: the exchange directory
`config.HARNESS_DIR` (brief, session plan, audit report) travels in both directions
(`CARRY_DIRS`). In it the repo's `.gitignore` does not apply, only
`config.HARNESS_LOCAL_FILE` (e.g. working copies that stay on their side); embedded
repos do not travel in, and on the way back the gitlink check rejects them.

This way the agent can write neither the host repo's `.git` (hooks, config) nor files
outside the worktree. `cancel()` additionally stops the container
(`docker rm -f`) — a `killpg` on the docker CLI process group does not end the image
— and discards the result.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import egress
from .config import FABRIKANT_HOME, HARNESS_DIR, HARNESS_LOCAL_FILE, HARNESS_PRIVATE_DIR
from .i18n import t

log = logging.getLogger(__name__)

ENABLE_VAR = "FABRIKANT_SANDBOX"
IMAGE_VAR = "FABRIKANT_SANDBOX_IMAGE"
MEMORY_VAR = "FABRIKANT_SANDBOX_MEMORY"
CPUS_VAR = "FABRIKANT_SANDBOX_CPUS"
USER_VAR = "FABRIKANT_SANDBOX_USER"
PIDS_LIMIT_VAR = "FABRIKANT_SANDBOX_PIDS_LIMIT"
NETWORK_VAR = "FABRIKANT_SANDBOX_NETWORK"
WORK_SIZE_VAR = "FABRIKANT_SANDBOX_WORK_SIZE"
PROXY_URL_VAR = "FABRIKANT_EGRESS_PROXY_URL"
# Never route loopback through the proxy: tests with a local server (127.0.0.1) would otherwise get 403 from the filter.
NO_PROXY_HOSTS = "localhost,127.0.0.1,::1"

DEFAULT_IMAGE = "fabrikant/agent-sandbox:latest"
DEFAULT_MEMORY = "4g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS_LIMIT = 512
DEFAULT_NETWORK = "fabrikant-sandbox"  # ADR 0051: internal, no direct egress
# tmpfs for the working clone — counts against --memory (cgroup), hence < DEFAULT_MEMORY.
DEFAULT_WORK_SIZE = "2g"
_CONTAINER_PREFIX = "fabrikant-agent-"
DOCKER_TIMEOUT = 60  # s, for `docker rm -f` and `docker ps`
AGENT_LABEL = "fabrikant.agent=1"  # Marker an jedem Agent-Container (Startup-Sweep)
# Unprivileged default user when fabrikant itself runs as root (root in the container = root uid
# on the host as long as no userns remap is active). `out/` is 0777 for that.
UNPRIVILEGED_USER = "10001:10001"
# Upper bounds for what the container returns (host disk/RAM): bundle before the fetch,
# patch before applying. The container is untrusted.
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_PATCH_BYTES = 128 * 1024 * 1024
SMALL_TMPFS_SIZE = "256m"  # /tmp and /home/agent (HOME)
# File modes a result must not create: symlink (points out of the worktree) and
# gitlink/submodule.
_FORBIDDEN_MODES = frozenset({"120000", "160000"})

# Pfade im Container (Austausch-Mounts, Arbeitsklon).
IN_BUNDLE = "/in/base.bundle"
OUT_DIR = "/out"
RESULT_BUNDLE_NAME = "result.bundle"
WORK_DIR = "/work"
# Helper scripts (relative to FABRIKANT_HOME) that specs call by host path, e.g.
# `{fabrikant_home}/scripts/ci-matrix-pytest.py`; see `SandboxConfig.helper_scripts`.
HELPER_SCRIPTS_DIR = "scripts"
# Locally ignored directories that nevertheless travel into the container and back (module docstring).
CARRY_DIRS: tuple[str, ...] = (HARNESS_DIR,)
# Exit code of the entrypoint when the git exchange in the container fails (EX_SOFTWARE).
EX_EXCHANGE = 70

_REF_PREFIX = "refs/fabrikant/sandbox/"
# Remote-tracking refs that `prepare_exchange(extra_refs=…)` adds to the bundle (e.g. `origin/main` as
# the diff base of test steps); the entrypoint fetches them into the clone when present.
_REMOTE_REFS = "refs/remotes/"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "fabrikant-sandbox",
    "GIT_AUTHOR_EMAIL": "sandbox@fabrikant.invalid",
    "GIT_COMMITTER_NAME": "fabrikant-sandbox",
    "GIT_COMMITTER_EMAIL": "sandbox@fabrikant.invalid",
}
# No auto maintenance (gc/maintenance) during the exchange in the host repo.
_GIT_QUIET_CONFIG = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


# Staging in three steps, identical on the host (`_worktree_commit`) and in the container
# (`entrypoint_script`): everything per the repo's rules, carry directories out of the
# index again, then force-add their files filtered only by HARNESS_LOCAL_FILE.
# (Exclude pathspecs do not work: on an ignored directory `git add` ends with exit 1.)
_LIST_CARRY = ("ls-files", "-o", "-z", f"--exclude-per-directory={HARNESS_LOCAL_FILE}", "--")
_UNSTAGE_CARRY = ("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--")
_FORCE_ADD = ("-c", "advice.addEmptyPathspec=false", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul")


def entrypoint_script(
    work_dir: str = WORK_DIR, in_bundle: str = IN_BUNDLE, out_dir: str = OUT_DIR, carry: tuple[str, ...] = CARRY_DIRS
) -> str:
    """Entrypoint in the container (POSIX sh, needs git in the image): $1 = base ref in the bundle,
    followed by the agent command. git output goes to stderr — stdout belongs to the agent
    (the backend parses it). Exit: that of the agent; if only the exchange fails, EX_EXCHANGE.
    Staging works like on the host (`_worktree_commit`): everything except `carry` per the rules of the
    repo, `carry` only per `HARNESS_LOCAL_FILE`. Paths are parameters so tests can run the script
    without Docker."""
    dirs = " ".join(shlex.quote(d) for d in carry)
    unstage, listing, force_add = (shlex.join(c) for c in (_UNSTAGE_CARRY, _LIST_CARRY, _FORCE_ADD))
    return f"""\
set -u
ref="$1"; shift
mkdir -p {work_dir}/repo && cd {work_dir}/repo || exit {EX_EXCHANGE}
{{ git init -q && git fetch -q --no-tags {in_bundle} "+$ref:refs/heads/fabrikant-base" \\
  && git checkout -q -b work fabrikant-base; }} >&2 || exit {EX_EXCHANGE}
git fetch -q --no-tags {in_bundle} "+{_REMOTE_REFS}*:{_REMOTE_REFS}*" >&2 2>/dev/null || true
rc=0
"$@" || rc=$?
{{ git add -A && git {unstage} {dirs} \\
  && git {listing} {dirs} | GIT_LITERAL_PATHSPECS=1 git {force_add} \\
  && git -c user.name=fabrikant-sandbox -c user.email=sandbox@fabrikant.invalid \\
    commit -q --no-verify --allow-empty -m "fabrikant-sandbox: Ergebnis" \\
  && git bundle create -q {out_dir}/{RESULT_BUNDLE_NAME} fabrikant-base..HEAD; }} >&2 \\
  || {{ [ "$rc" -ne 0 ] || rc={EX_EXCHANGE}; }}
exit "$rc"
"""


# Host path variables that would LIE in the container (HOME=/root is on the
# read-only root FS; the host PATH does not find the image binaries). They are
# stripped and replaced by SANDBOX_ENV_OVERRIDES.
SANDBOX_ENV_STRIP: tuple[str, ...] = (
    "HOME",
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PWD",
    "HOSTNAME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)

# Container defaults — set AFTER the env loop (Docker: the last -e wins).
SANDBOX_ENV_OVERRIDES: dict[str, str] = {
    "HOME": "/home/agent",
    "XDG_CONFIG_HOME": "/home/agent/.config",
    "XDG_DATA_HOME": "/home/agent/.local/share",
    "XDG_CACHE_HOME": "/home/agent/.cache",
    "XDG_STATE_HOME": "/home/agent/.local/state",
    "TMPDIR": "/tmp",  # nosec B108 # path in the agent container (own tmpfs), not the host /tmp
    # uv cache onto the large /work tmpfs, next to (not inside) the clone: HOME is too small for
    # the wheels of a `uv sync`, and inside the clone it ended up in the result diff.
    "UV_CACHE_DIR": f"{WORK_DIR}/.uv-cache",
}

_TRUTHY = ("1", "true", "yes", "on")


class SandboxError(RuntimeError):
    """Exchange with the container failed or result rejected."""


def sandbox_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`FABRIKANT_SANDBOX` is set (1/true/yes/on) → agent container mode."""
    env = os.environ if env is None else env
    return env.get(ENABLE_VAR, "").strip().lower() in _TRUTHY


def _helper_scripts_dir() -> str:
    """`FABRIKANT_HOME/scripts` if it exists, else empty (no mount): `docker run -v` would create a
    missing source directory on the host (root-owned with a rootful daemon), e.g. under the
    `~/.local/share/fabrikant` home of an installed wheel, which has no `scripts/`."""
    path = FABRIKANT_HOME / HELPER_SCRIPTS_DIR
    return str(path) if path.is_dir() else ""


@dataclass
class SandboxConfig:
    """Container hardening (env overrides in `from_env`)."""

    image: str = DEFAULT_IMAGE
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    user: str = ""  # leer → host uid:gid; als root: UNPRIVILEGED_USER (s. `effective_user`)
    pids_limit: int = DEFAULT_PIDS_LIMIT
    network: str = DEFAULT_NETWORK
    work_size: str = DEFAULT_WORK_SIZE
    # ADR 0051: egress proxy in the sandbox network. Default = the proxy container started by
    # `fabrikant egress start`; without a running proxy agent HTTP calls fail (fail-closed).
    proxy_url: str = ""
    # Host directory mounted read-only at the SAME path, so commands that call fabrikant's helper
    # scripts by host path (`{fabrikant_home}/scripts/…`) work in the container too. Empty → no mount.
    helper_scripts: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SandboxConfig:
        env = os.environ if env is None else env
        pids_raw = env.get(PIDS_LIMIT_VAR, "") or str(DEFAULT_PIDS_LIMIT)
        try:
            pids_limit = int(pids_raw)
        except ValueError:
            raise RuntimeError(t("sandbox.must_be_an_integer_is", v1=PIDS_LIMIT_VAR, pids_raw=pids_raw)) from None
        return cls(
            image=env.get(IMAGE_VAR, "") or DEFAULT_IMAGE,
            memory=env.get(MEMORY_VAR, "") or DEFAULT_MEMORY,
            cpus=env.get(CPUS_VAR, "") or DEFAULT_CPUS,
            user=env.get(USER_VAR, ""),
            pids_limit=pids_limit,
            network=env.get(NETWORK_VAR, "") or DEFAULT_NETWORK,
            work_size=env.get(WORK_SIZE_VAR, "") or DEFAULT_WORK_SIZE,
            proxy_url=env.get(PROXY_URL_VAR, "") or egress.agent_proxy_url(),
            helper_scripts=_helper_scripts_dir(),
        )

    @property
    def effective_user(self) -> str:
        """Execution user in the container: explicitly set, otherwise host uid:gid — unless fabrikant
        runs as root, then an unprivileged user."""
        if self.user:
            return self.user
        uid = os.getuid()
        return UNPRIVILEGED_USER if uid == 0 else f"{uid}:{os.getgid()}"


@dataclass(frozen=True)
class Exchange:
    """Exchange of an agent run: temporary directory (bundles), base commit, host refs."""

    tmpdir: Path
    base: str  # Commit of the worktree state before the agent step
    ref: str  # Host ref to `base` (bundle ref, in the container the base ref)
    session_id: str = ""  # for `stop_container` before reading in the result

    @property
    def in_bundle(self) -> Path:
        return self.tmpdir / "base.bundle"

    @property
    def out_dir(self) -> Path:
        return self.tmpdir / "out"

    @property
    def result_bundle(self) -> Path:
        return self.out_dir / RESULT_BUNDLE_NAME

    @property
    def result_ref(self) -> str:
        return f"{self.ref}-result"


def container_name(session_id: str) -> str:
    """Docker-safe name: `fabrikant-agent-<sid>` (only `[a-zA-Z0-9_-]`, ≤ 63 chars)."""
    return (_CONTAINER_PREFIX + _SLUG_RE.sub("-", session_id))[:63]


def _git(
    workdir: str, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """git in the host worktree; error → SandboxError with stderr."""
    p = subprocess.run(
        ["git", *_GIT_QUIET_CONFIG, *args],
        cwd=workdir,
        env={**os.environ, **(env or {})},
        input=stdin,
        capture_output=True,
        check=False,
    )
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip()
        raise SandboxError(f"git {args[0]} fehlgeschlagen (exit {p.returncode}): {err}")
    return p


def _require_worktree_root(workdir: str) -> None:
    """`workdir` must be the root of a git worktree. In a subdirectory the
    bundle would contain the whole repo and the root-relative patch would be applied in the wrong place."""
    top = _git(workdir, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if os.path.realpath(top) != os.path.realpath(workdir):
        raise SandboxError(t("sandbox.sandbox_needs_the_worktree_root", top=top, workdir=workdir))


def _carry_files(workdir: str, carry: tuple[str, ...], env: Mapping[str, str] | None = None) -> list[str]:
    """Untracked files in `carry`, filtered only by `HARNESS_LOCAL_FILE` (not by the
    repo's ignore rules); embedded repos (`path/`) stay outside."""
    out = _git(workdir, *_LIST_CARRY, *carry, env=env)
    return [p for p in out.stdout.decode(errors="surrogateescape").split("\0") if p and not p.endswith("/")]


def _worktree_commit(workdir: str, tmpdir: Path, carry: tuple[str, ...] = CARRY_DIRS) -> str:
    """Commit of the current worktree state (HEAD + changes + untracked, without ignored —
    except the files in `carry`, see module docstring).

    Clean worktree → HEAD itself. Otherwise a temporary commit via its own index
    (`GIT_INDEX_FILE`): index, HEAD and files of the worktree stay unchanged."""
    head = _git(workdir, "rev-parse", "HEAD").stdout.decode().strip()
    carried = _carry_files(workdir, carry)
    if not carried and not _git(workdir, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        return head
    index_env = {"GIT_INDEX_FILE": str(tmpdir / "index"), **_GIT_IDENTITY}
    _git(workdir, "read-tree", "HEAD", env=index_env)
    _git(workdir, "add", "-A", env=index_env)
    _git(workdir, *_UNSTAGE_CARRY, *carry, env=index_env)
    if carried := _carry_files(workdir, carry, env=index_env):
        listing = "\0".join(carried).encode(errors="surrogateescape")
        _git(workdir, *_FORCE_ADD, env={**index_env, "GIT_LITERAL_PATHSPECS": "1"}, stdin=listing)
    tree = _git(workdir, "write-tree", env=index_env).stdout.decode().strip()
    msg = "fabrikant-sandbox: worktree state before the agent step"
    return _git(workdir, "commit-tree", tree, "-p", head, "-m", msg, env=index_env).stdout.decode().strip()


def prepare_exchange(session_id: str, workdir: str, extra_refs: tuple[str, ...] = ()) -> Exchange:
    """Provide the worktree state as a bundle for the container (before `docker run`).

    `extra_refs`: additional `refs/remotes/...` to bundle (the container gets them under the same name)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="fabrikant-sandbox-"))  # 0700: no other host user
    ref = _REF_PREFIX + _SLUG_RE.sub("-", session_id)
    try:
        _require_worktree_root(workdir)
        base = _worktree_commit(workdir, tmpdir)
        _git(workdir, "update-ref", ref, base)
        _git(workdir, "bundle", "create", "-q", str(tmpdir / "base.bundle"), ref, *extra_refs)
        out = tmpdir / "out"
        out.mkdir()
        out.chmod(0o777)  # FABRIKANT_SANDBOX_USER may be ≠ host user; parent directory is 0700
    except Exception:
        discard_exchange(Exchange(tmpdir, "", ref), workdir)
        raise
    return Exchange(tmpdir=tmpdir, base=base, ref=ref, session_id=session_id)


def _reject_unsafe_paths(paths: list[str]) -> None:
    """No path with a `.git` component (case-insensitive): git metadata is written only by the host.
    No path under the private harness directory: the agent must not forge state there."""
    for path in paths:
        if any(part.lower() == ".git" for part in path.split("/")):
            raise SandboxError(t("sandbox.result_rejected_path_touches_git", path=path))
        if path.split("/", 1)[0] == HARNESS_PRIVATE_DIR:
            raise SandboxError(f"Ergebnis abgewiesen: Pfad {path!r} liegt im privaten Harness-Verzeichnis")


def _parse_raw_diff(raw: str) -> list[tuple[str, str]]:
    """`git diff --raw -z` → [(new mode, path)]. Format per entry: `:<old> <new> <sha> <sha> <status>\0<pfad>\0`."""
    tokens = raw.split("\0")
    entries: list[tuple[str, str]] = []
    for meta, path in zip(tokens[0::2], tokens[1::2], strict=False):
        fields = meta.lstrip(":").split()
        if len(fields) >= 2:
            entries.append((fields[1], path))
    return entries


def _reject_unsafe_entries(entries: list[tuple[str, str]]) -> None:
    """Check paths (`.git`) and modes: symlinks point out of the worktree (later host steps
    would read host files), gitlinks are submodules — an agent result creates neither."""
    _reject_unsafe_paths([path for _, path in entries])
    for mode, path in entries:
        if mode in _FORBIDDEN_MODES:
            kind = "Symlink" if mode == "120000" else "Submodul"
            raise SandboxError(f"Ergebnis abgewiesen: {kind} {path!r} (Modus {mode})")


def apply_result(ex: Exchange, workdir: str) -> bool:
    """Take over the container's result bundle into the worktree. False = no result.

    Raises:
        SandboxError: bundle invalid/not a regular file, result does not descend from the
            base commit, unsafe path or the diff cannot be applied."""
    bundle = ex.result_bundle
    if not os.path.lexists(bundle):
        return False
    st = os.lstat(bundle)
    if not stat.S_ISREG(st.st_mode):  # no symlink to host files
        raise SandboxError(t("sandbox.result_rejected_result_bundle_is"))
    if st.st_size > MAX_BUNDLE_BYTES:
        raise SandboxError(t("sandbox.result_rejected_result_bundle_too", v1=st.st_size, v2=MAX_BUNDLE_BYTES))
    _git(
        workdir,
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "fetch",
        "-q",
        "--no-tags",
        "--no-write-fetch-head",
        str(bundle),
        f"+HEAD:{ex.result_ref}",
    )
    try:
        _git(workdir, "merge-base", "--is-ancestor", ex.base, ex.result_ref)
    except SandboxError:
        raise SandboxError(t("sandbox.result_rejected_does_not_descend")) from None
    diff_args = ("--no-renames", "--no-ext-diff", "--no-textconv", ex.base, ex.result_ref)
    raw = _git(workdir, "diff", "--raw", "-z", *diff_args).stdout.decode(errors="surrogateescape")
    _reject_unsafe_entries(_parse_raw_diff(raw))
    patch = _git(workdir, "diff", "--binary", *diff_args).stdout
    if len(patch) > MAX_PATCH_BYTES:
        raise SandboxError(t("sandbox.result_rejected_patch_too_large", v1=len(patch), v2=MAX_PATCH_BYTES))
    if patch:
        _git(workdir, "apply", "--binary", "--whitespace=nowarn", "-", stdin=patch)
    return True


def discard_exchange(ex: Exchange, workdir: str) -> None:
    """Remove refs and temporary directory (best effort, idempotent)."""
    for ref in (ex.ref, ex.result_ref):
        subprocess.run(
            ["git", *_GIT_QUIET_CONFIG, "update-ref", "-d", ref], cwd=workdir, capture_output=True, check=False
        )
    shutil.rmtree(ex.tmpdir, ignore_errors=True)


def finish_exchange(ex: Exchange, workdir: str) -> str | None:
    """After process end: take over the result and clean up. Error message or None.

    The container is ended beforehand: after a timeout/kill of the docker CLI it may keep running
    and still write to `/out` while we read."""
    try:
        if ex.session_id:
            stop_container(ex.session_id)
        apply_result(ex, workdir)
        return None
    except (SandboxError, OSError) as e:
        return str(e)
    finally:
        discard_exchange(ex, workdir)


def render_docker_cmd(
    cfg: SandboxConfig,
    session_id: str,
    agent_cmd: list[str],
    exchange: Exchange,
    env: dict[str, str],
) -> list[str]:
    """`docker run` argv for an agent run (hardening flags, exchange, see module docstring).

    Secrets are NOT in the argv (visible in `ps`/`docker inspect`): `-e KEY` without a value lets
    docker read the value from the env of the CLI process — the caller starts it with `env`
    (+ `docker_cli_env()`). Only the fixed container defaults appear as `KEY=VALUE` in the argv.

    Env rule: host path variables (`SANDBOX_ENV_STRIP`) are taken out —
    they point to host paths that do not exist on the read-only root FS
    (HOME=/root) or shadow the image binaries (host PATH). Afterwards
    the container defaults (`SANDBOX_ENV_OVERRIDES`) come LAST — with a
    duplicate `-e` Docker applies the last one, so that one wins."""
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-i",  # append stdin: the prompt comes via stdin (_oneshot.send_prompt)
        "--name",
        container_name(session_id),
        "--label",
        AGENT_LABEL,
        "--network",
        cfg.network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        cfg.memory,
        "--memory-swap",
        cfg.memory,  # = memory: no swap
        "--pull",
        "never",  # the image is a deployment artifact; no silent pulling at runtime
        "--cpus",
        cfg.cpus,
        "--pids-limit",
        str(cfg.pids_limit),
        "--read-only",
        "--tmpfs",
        # exec: test runners and build tools put executables there (pytest's tmp_path, uv, pip builds).
        f"/tmp:rw,exec,mode=1777,size={SMALL_TMPFS_SIZE}",  # nosec B108 # tmpfs in the container, not the host /tmp
        # HOME of the agent CLIs (config, caches). Own tmpfs options → mode explicit,
        # otherwise root:755 and not writable for the agent user.
        "--tmpfs",
        f"{SANDBOX_ENV_OVERRIDES['HOME']}:rw,mode=1777,size={SMALL_TMPFS_SIZE}",
        # Working clone: exec (the agent's build/test tools), size limited.
        "--tmpfs",
        f"{WORK_DIR}:rw,exec,mode=1777,size={cfg.work_size}",
        "-u",
        cfg.effective_user,
        "-v",
        f"{exchange.in_bundle}:{IN_BUNDLE}:ro",
        "-v",
        f"{exchange.out_dir}:{OUT_DIR}",
        "-w",
        WORK_DIR,
    ]
    if cfg.helper_scripts:
        cmd[-2:-2] = ["-v", f"{cfg.helper_scripts}:{cfg.helper_scripts}:ro"]
    for key in sorted(env):
        if key in SANDBOX_ENV_STRIP:
            continue
        cmd += ["-e", key]
    for key, value in SANDBOX_ENV_OVERRIDES.items():
        cmd += ["-e", f"{key}={value}"]
    if cfg.proxy_url:
        # Upper and lower case: curl/wget read only `http_proxy`, others only the uppercase ones.
        for name, value in (
            ("HTTP_PROXY", cfg.proxy_url),
            ("HTTPS_PROXY", cfg.proxy_url),
            ("NO_PROXY", NO_PROXY_HOSTS),
        ):
            cmd += ["-e", f"{name}={value}", "-e", f"{name.lower()}={value}"]
    cmd += ["--entrypoint", "sh", cfg.image, "-c", entrypoint_script(), "fabrikant-sandbox", exchange.ref]
    cmd.extend(agent_cmd)
    return cmd


_DOCKER_CLI_ENV_PREFIXES = ("DOCKER_",)
_DOCKER_CLI_ENV_NAMES = ("XDG_RUNTIME_DIR",)  # rootless-Docker-Socket


def docker_cli_env(base: Mapping[str, str], host: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env for the `docker` CLI process: the scrubbed agent env (`base`, from which docker reads the
    `-e KEY` values) plus the variables the CLI itself needs to find the daemon
    (`DOCKER_HOST`/`DOCKER_CONFIG`/`DOCKER_CONTEXT`, `XDG_RUNTIME_DIR`). These do NOT get into
    the container (there are only explicit `-e` flags for that)."""
    host = os.environ if host is None else host
    extra = {k: v for k, v in host.items() if k.startswith(_DOCKER_CLI_ENV_PREFIXES) or k in _DOCKER_CLI_ENV_NAMES}
    return {**base, **extra}


def stop_container(session_id: str) -> None:
    """Best-effort `docker rm -f` — idempotent (no-op if the container is gone)."""
    # Docker daemon hangs: cancel() should not hang with it.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):  # OSError: docker not installed
        subprocess.run(
            ["docker", "rm", "-f", container_name(session_id)],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )


def sweep_stale_containers() -> int:
    """Removes orphaned agent containers (crash/host reboot); returns the number removed.

    Call ONLY at server start before the first run — otherwise active runs would be hit.
    Best effort: no docker/timeout/error → warning, never an abort of the start."""
    try:
        ls = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={AGENT_LABEL}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )
        ids = ls.stdout.split() if ls.returncode == 0 else []
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning(t("sandbox.sandbox_sweep_skipped_s"), e)
        return 0
    return len(ids)


# ── Shell steps in the sandbox (`sandboxed: true`, ADR 0054: repo code does not run on the host) ──


def default_branch_refs(workdir: str) -> tuple[str, ...]:
    """The worktree's remote default branch as a ref (`origin/HEAD`, else `origin/main|master`) — the
    usual diff base of lint/test scripts (`--base origin/main`). Empty when there is none."""
    head = subprocess.run(
        ["git", *_GIT_QUIET_CONFIG, "symbolic-ref", "-q", f"{_REMOTE_REFS}origin/HEAD"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    candidates = (head, f"{_REMOTE_REFS}origin/main", f"{_REMOTE_REFS}origin/master")
    for ref in filter(None, candidates):
        verified = subprocess.run(
            ["git", *_GIT_QUIET_CONFIG, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"],
            cwd=workdir,
            capture_output=True,
            check=False,
        )
        if verified.returncode == 0:
            return (ref,)
    return ()


# `sh -c` in the container: change into the step's subdirectory, then run the command.
_SHELL_IN_SUBDIR = f'cd "$1" || exit {EX_EXCHANGE}; exec sh -c "$2"'


def run_shell(
    session_id: str, cmd: str, workdir: str, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a shell step's command in the agent container.

    Same exchange as for agents: the worktree state (plus the remote default branch) goes in as a
    bundle, changes come back through `finish_exchange`. `workdir` may be a subdirectory of the
    worktree; the command runs in the same subdirectory of the clone. `env` is the (scrubbed)
    container env. Returns the `CompletedProcess`; a timeout propagates like in `procs.run_group`.

    The entrypoint always writes a result bundle (an empty commit when nothing changed) and keeps a
    failing command's exit code even when the export fails. So the exit code alone cannot tell "the
    command failed" from "the exchange failed": a missing bundle is the signal, checked before the
    caller classifies the exit code (a red test must not hide an infrastructure failure).

    Raises:
        SandboxError: the exchange failed before or after the run (no result bundle, result rejected)."""
    from .procs import run_group  # local: procs registers atexit hooks

    top = _git(workdir, "rev-parse", "--show-toplevel").stdout.decode().strip()
    rel = os.path.relpath(os.path.realpath(workdir), os.path.realpath(top))
    cfg = SandboxConfig.from_env()  # before the exchange: a config error must not leave refs/tmpdir behind
    ex = prepare_exchange(session_id, top, default_branch_refs(top))
    argv = render_docker_cmd(cfg, session_id, ["sh", "-c", _SHELL_IN_SUBDIR, "fabrikant-shell", rel, cmd], ex, env)
    try:
        proc = run_group(argv, timeout=timeout, env=docker_cli_env(env), stdin=subprocess.DEVNULL)
    except BaseException:
        stop_container(session_id)
        discard_exchange(ex, top)
        raise
    has_result = os.path.lexists(ex.result_bundle)  # docker run has returned: the container is done
    error = finish_exchange(ex, top)
    if error:
        raise SandboxError(error)
    if not has_result:
        raise SandboxError(t("sandbox.shell_result_missing", code=proc.returncode))
    return proc
