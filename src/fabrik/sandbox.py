"""Per-Run-Agent-Sandbox — One-Shot-CLI-Agenten in einem Docker-Container (ADR 0054, in Überarbeitung).

One-Shot-CLI-Agenten (claude-code, codex, copilot, opencode) laufen standardmäßig als
bloßer Subprozess auf dem Host. Mit `FABRIK_SANDBOX=1` hüllt
`OneShotCliBackend.send_prompt` den Agent-Befehl in `docker run` mit
Hardening ein:

  - eigener Container pro Run (Name `fabrik-agent-<sid>`, `--rm`, `--init`),
  - `--network fabrik-sandbox` (ADR 0051: `internal` — kein direkter
    Internetzugang, Egress nur über den Allowlist-Proxy; Loopback
    (`NO_PROXY`) ausgenommen),
  - `--cap-drop ALL` + `--security-opt no-new-privileges`,
  - Resource-Caps (`--memory`, `--cpus`, `--pids-limit`),
  - read-only root fs + tmpfs für `/tmp`, `/home/agent` (HOME) und den Arbeitsklon `/work`,
  - Env nur als explizite `-e`-Flags (bereits per ADR 0050 gescrubbt);
    Host-Pfad-Variablen (HOME, PATH, XDG_*, TMPDIR, …) werden GESTRIPPT und
    durch Container-Defaults ersetzt (Overrides stehen LETZTE → gewinnen).

Kein Mount des Worktrees. Der Container sieht den Host nicht, Austausch nur über git:

  1. Host (`prepare_exchange`): Stand des Worktrees (HEAD + uncommittete Änderungen,
     über einen temporären Index — Index und HEAD des Worktrees bleiben unberührt)
     als `git bundle` → read-only Mount `/in/base.bundle`.
  2. Container (`entrypoint_script`): eigener Klon in `/work/repo`, Agent arbeitet dort;
     danach `git add -A` + Commit + Bundle nach `/out/result.bundle` (eigenes,
     leeres Austauschverzeichnis — das einzige schreibbare Mount).
  3. Host (`apply_result`): `git fetch` aus dem Bundle mit Objekt-Prüfung, Ergebnis muss
     vom Basis-Commit abstammen, kein Pfad mit `.git`-Komponente, dann den vom Host
     berechneten Diff per `git apply` in den Worktree — Ergebnis wie ein Agent auf dem
     Host, der nicht committet. Eigene Commits des Agenten werden dabei zusammengefasst.

Lokal ignorierte Dateien bleiben draußen — mit einer Ausnahme: das Austauschverzeichnis
`config.HARNESS_DIR` (Brief, Session-Plan, Audit-Report) reist in beide Richtungen
(`CARRY_DIRS`). Darin gilt nicht die `.gitignore` des Repos, sondern nur
`config.HARNESS_LOCAL_FILE` (z. B. Arbeitskopien, die auf ihrer Seite bleiben); eingebettete
Repos reisen nicht hinein, und zurück lehnt die Gitlink-Prüfung sie ab.

Damit kann der Agent weder `.git` des Host-Repos (Hooks, Config) noch Dateien
außerhalb des Worktrees schreiben. `cancel()` stoppt zusätzlich den Container
(`docker rm -f`) — ein `killpg` auf die docker-CLI-Prozessgruppe beendet das Image
nicht — und verwirft das Ergebnis.
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
from .config import HARNESS_DIR, HARNESS_LOCAL_FILE, HARNESS_PRIVATE_DIR
from .i18n import t

log = logging.getLogger(__name__)

ENABLE_VAR = "FABRIK_SANDBOX"
IMAGE_VAR = "FABRIK_SANDBOX_IMAGE"
MEMORY_VAR = "FABRIK_SANDBOX_MEMORY"
CPUS_VAR = "FABRIK_SANDBOX_CPUS"
USER_VAR = "FABRIK_SANDBOX_USER"
PIDS_LIMIT_VAR = "FABRIK_SANDBOX_PIDS_LIMIT"
NETWORK_VAR = "FABRIK_SANDBOX_NETWORK"
WORK_SIZE_VAR = "FABRIK_SANDBOX_WORK_SIZE"
PROXY_URL_VAR = "FABRIK_EGRESS_PROXY_URL"
# Loopback nie über den Proxy: Tests mit lokalem Server (127.0.0.1) bekämen sonst 403 vom Filter.
NO_PROXY_HOSTS = "localhost,127.0.0.1,::1"

DEFAULT_IMAGE = "fabrik/agent-sandbox:latest"
DEFAULT_MEMORY = "4g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS_LIMIT = 512
DEFAULT_NETWORK = "fabrik-sandbox"  # ADR 0051: internal, kein direkter Egress
# tmpfs für den Arbeitsklon — zählt gegen --memory (cgroup), daher < DEFAULT_MEMORY.
DEFAULT_WORK_SIZE = "2g"
_CONTAINER_PREFIX = "fabrik-agent-"
DOCKER_TIMEOUT = 60  # s, für `docker rm -f` und `docker ps`
AGENT_LABEL = "fabrik.agent=1"  # Marker an jedem Agent-Container (Startup-Sweep)
# Unprivilegierter Default-User, wenn fabrik selbst als root läuft (root im Container = root-uid
# auf dem Host, solange kein userns-remap aktiv ist). `out/` ist dafür 0777.
UNPRIVILEGED_USER = "10001:10001"
# Obergrenzen für das, was der Container zurückgibt (Host-Platte/-RAM): Bundle vor dem Fetch,
# Patch vor dem Anwenden. Der Container ist untrusted.
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_PATCH_BYTES = 128 * 1024 * 1024
SMALL_TMPFS_SIZE = "256m"  # /tmp und /home/agent (HOME)
# Dateimodi, die ein Ergebnis nicht anlegen darf: Symlink (zeigt aus dem Worktree heraus) und
# Gitlink/Submodul.
_FORBIDDEN_MODES = frozenset({"120000", "160000"})

# Pfade im Container (Austausch-Mounts, Arbeitsklon).
IN_BUNDLE = "/in/base.bundle"
OUT_DIR = "/out"
RESULT_BUNDLE_NAME = "result.bundle"
WORK_DIR = "/work"
# Lokal ignorierte Verzeichnisse, die trotzdem in den Container und zurück reisen (Moduldocstring).
CARRY_DIRS: tuple[str, ...] = (HARNESS_DIR,)
# Exit-Code des Entrypoints, wenn der git-Austausch im Container scheitert (EX_SOFTWARE).
EX_EXCHANGE = 70

_REF_PREFIX = "refs/fabrik/sandbox/"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "fabrik-sandbox",
    "GIT_AUTHOR_EMAIL": "sandbox@fabrik.invalid",
    "GIT_COMMITTER_NAME": "fabrik-sandbox",
    "GIT_COMMITTER_EMAIL": "sandbox@fabrik.invalid",
}
# Keine Auto-Wartung (gc/maintenance) während des Austauschs im Host-Repo.
_GIT_QUIET_CONFIG = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


# Staging in drei Schritten, identisch auf dem Host (`_worktree_commit`) und im Container
# (`entrypoint_script`): alles nach den Regeln des Repos, Carry-Verzeichnisse wieder aus dem
# Index, dann deren Dateien gefiltert nur nach HARNESS_LOCAL_FILE erzwungen hinzufügen.
# (Exclude-Pathspecs gehen nicht: Auf ein ignoriertes Verzeichnis endet `git add` mit Exit 1.)
_LIST_CARRY = ("ls-files", "-o", "-z", f"--exclude-per-directory={HARNESS_LOCAL_FILE}", "--")
_UNSTAGE_CARRY = ("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--")
_FORCE_ADD = ("-c", "advice.addEmptyPathspec=false", "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul")


def entrypoint_script(
    work_dir: str = WORK_DIR, in_bundle: str = IN_BUNDLE, out_dir: str = OUT_DIR, carry: tuple[str, ...] = CARRY_DIRS
) -> str:
    """Entrypoint im Container (POSIX sh, braucht git im Image): $1 = Basis-Ref im Bundle,
    danach der Agent-Befehl. git-Ausgaben gehen nach stderr — stdout gehört dem Agenten
    (das Backend parst ihn). Exit: der des Agenten; scheitert nur der Austausch, EX_EXCHANGE.
    Gestaged wird wie auf dem Host (`_worktree_commit`): alles außer `carry` nach den Regeln des
    Repos, `carry` nur nach `HARNESS_LOCAL_FILE`. Pfade sind Parameter, damit Tests das Skript
    ohne Docker ausführen können."""
    dirs = " ".join(shlex.quote(d) for d in carry)
    unstage, listing, force_add = (shlex.join(c) for c in (_UNSTAGE_CARRY, _LIST_CARRY, _FORCE_ADD))
    return f"""\
set -u
ref="$1"; shift
mkdir -p {work_dir}/repo && cd {work_dir}/repo || exit {EX_EXCHANGE}
{{ git init -q && git fetch -q --no-tags {in_bundle} "+$ref:refs/heads/fabrik-base" \\
  && git checkout -q -b work fabrik-base; }} >&2 || exit {EX_EXCHANGE}
rc=0
"$@" || rc=$?
{{ git add -A && git {unstage} {dirs} \\
  && git {listing} {dirs} | GIT_LITERAL_PATHSPECS=1 git {force_add} \\
  && git -c user.name=fabrik-sandbox -c user.email=sandbox@fabrik.invalid \\
    commit -q --no-verify --allow-empty -m "fabrik-sandbox: Ergebnis" \\
  && git bundle create -q {out_dir}/{RESULT_BUNDLE_NAME} fabrik-base..HEAD; }} >&2 \\
  || {{ [ "$rc" -ne 0 ] || rc={EX_EXCHANGE}; }}
exit "$rc"
"""


# Host-Pfad-Variablen, die im Container LÜGEN würden (HOME=/root liegt auf der
# read-only Root-FS; der Host-PATH findet die Image-Binaries nicht). Werden
# gestrippt und durch SANDBOX_ENV_OVERRIDES ersetzt.
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

# Container-Defaults — werden NACH der Env-Schleife gesetzt (Docker: letztes -e gewinnt).
SANDBOX_ENV_OVERRIDES: dict[str, str] = {
    "HOME": "/home/agent",
    "XDG_CONFIG_HOME": "/home/agent/.config",
    "XDG_DATA_HOME": "/home/agent/.local/share",
    "XDG_CACHE_HOME": "/home/agent/.cache",
    "XDG_STATE_HOME": "/home/agent/.local/state",
    "TMPDIR": "/tmp",  # nosec B108 # Pfad im Agent-Container (eigene tmpfs), nicht das Host-/tmp
    # uv-Cache auf die große /work-tmpfs, neben (nicht in) den Klon: HOME ist zu klein für
    # die Wheels eines `uv sync`, und im Klon landete er im Ergebnis-Diff.
    "UV_CACHE_DIR": f"{WORK_DIR}/.uv-cache",
}

_TRUTHY = ("1", "true", "yes", "on")


class SandboxError(RuntimeError):
    """Austausch mit dem Container gescheitert oder Ergebnis abgewiesen."""


def sandbox_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`FABRIK_SANDBOX` ist gesetzt (1/true/yes/on) → Agent-Container-Modus."""
    env = os.environ if env is None else env
    return env.get(ENABLE_VAR, "").strip().lower() in _TRUTHY


@dataclass
class SandboxConfig:
    """Container-Hardening (Env-Überschreibungen in `from_env`)."""

    image: str = DEFAULT_IMAGE
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    user: str = ""  # leer → host uid:gid; als root: UNPRIVILEGED_USER (s. `effective_user`)
    pids_limit: int = DEFAULT_PIDS_LIMIT
    network: str = DEFAULT_NETWORK
    work_size: str = DEFAULT_WORK_SIZE
    # ADR 0051: Egress-Proxy im Sandbox-Netz. Default = der von `fabrik egress start` gestartete
    # Proxy-Container; ohne laufenden Proxy scheitern Agent-HTTP-Calls (fail-closed).
    proxy_url: str = ""

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
        )

    @property
    def effective_user(self) -> str:
        """Ausführungs-User im Container: explizit gesetzt, sonst host uid:gid — außer fabrik
        läuft als root, dann ein unprivilegierter User."""
        if self.user:
            return self.user
        uid = os.getuid()
        return UNPRIVILEGED_USER if uid == 0 else f"{uid}:{os.getgid()}"


@dataclass(frozen=True)
class Exchange:
    """Austausch eines Agent-Runs: temporäres Verzeichnis (Bundles), Basis-Commit, Host-Refs."""

    tmpdir: Path
    base: str  # Commit des Worktree-Stands vor dem Agent-Step
    ref: str  # Host-Ref auf `base` (Bundle-Ref, im Container der Basis-Ref)
    session_id: str = ""  # für `stop_container` vor dem Einlesen des Ergebnisses

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
    """Docker-sicherer Name: `fabrik-agent-<sid>` (nur `[a-zA-Z0-9_-]`, ≤ 63 Z)."""
    return (_CONTAINER_PREFIX + _SLUG_RE.sub("-", session_id))[:63]


def _git(
    workdir: str, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """git im Host-Worktree; Fehler → SandboxError mit stderr."""
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
    """`workdir` muss die Wurzel eines Git-Worktrees sein. In einem Unterverzeichnis würde das
    Bundle das ganze Repo enthalten und der Wurzel-relative Patch am falschen Ort angewendet."""
    top = _git(workdir, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if os.path.realpath(top) != os.path.realpath(workdir):
        raise SandboxError(t("sandbox.sandbox_needs_the_worktree_root", top=top, workdir=workdir))


def _carry_files(workdir: str, carry: tuple[str, ...], env: Mapping[str, str] | None = None) -> list[str]:
    """Untracked Dateien in `carry`, gefiltert nur nach `HARNESS_LOCAL_FILE` (nicht nach den
    Ignore-Regeln des Repos); eingebettete Repos (`pfad/`) bleiben draußen."""
    out = _git(workdir, *_LIST_CARRY, *carry, env=env)
    return [p for p in out.stdout.decode(errors="surrogateescape").split("\0") if p and not p.endswith("/")]


def _worktree_commit(workdir: str, tmpdir: Path, carry: tuple[str, ...] = CARRY_DIRS) -> str:
    """Commit des aktuellen Worktree-Stands (HEAD + Änderungen + untracked, ohne ignored —
    außer den Dateien in `carry`, siehe Moduldocstring).

    Sauberer Worktree → HEAD selbst. Sonst ein temporärer Commit über einen eigenen Index
    (`GIT_INDEX_FILE`): Index, HEAD und Dateien des Worktrees bleiben unverändert."""
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
    msg = "fabrik-sandbox: Worktree-Stand vor dem Agent-Step"
    return _git(workdir, "commit-tree", tree, "-p", head, "-m", msg, env=index_env).stdout.decode().strip()


def prepare_exchange(session_id: str, workdir: str) -> Exchange:
    """Worktree-Stand als Bundle für den Container bereitstellen (vor `docker run`)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="fabrik-sandbox-"))  # 0700: kein anderer Host-User
    ref = _REF_PREFIX + _SLUG_RE.sub("-", session_id)
    try:
        _require_worktree_root(workdir)
        base = _worktree_commit(workdir, tmpdir)
        _git(workdir, "update-ref", ref, base)
        _git(workdir, "bundle", "create", "-q", str(tmpdir / "base.bundle"), ref)
        out = tmpdir / "out"
        out.mkdir()
        out.chmod(0o777)  # FABRIK_SANDBOX_USER kann ≠ Host-User sein; Eltern-Verzeichnis ist 0700
    except Exception:
        discard_exchange(Exchange(tmpdir, "", ref), workdir)
        raise
    return Exchange(tmpdir=tmpdir, base=base, ref=ref, session_id=session_id)


def _reject_unsafe_paths(paths: list[str]) -> None:
    """Kein Pfad mit `.git`-Komponente (Groß/Klein egal): git-Metadaten schreibt nur der Host.
    Kein Pfad unter dem privaten Harness-Verzeichnis: der Agent darf dort keinen Zustand fälschen."""
    for path in paths:
        if any(part.lower() == ".git" for part in path.split("/")):
            raise SandboxError(t("sandbox.result_rejected_path_touches_git", path=path))
        if path.split("/", 1)[0] == HARNESS_PRIVATE_DIR:
            raise SandboxError(f"Ergebnis abgewiesen: Pfad {path!r} liegt im privaten Harness-Verzeichnis")


def _parse_raw_diff(raw: str) -> list[tuple[str, str]]:
    """`git diff --raw -z` → [(neuer Modus, Pfad)]. Format je Eintrag: `:<alt> <neu> <sha> <sha> <status>\0<pfad>\0`."""
    tokens = raw.split("\0")
    entries: list[tuple[str, str]] = []
    for meta, path in zip(tokens[0::2], tokens[1::2], strict=False):
        fields = meta.lstrip(":").split()
        if len(fields) >= 2:
            entries.append((fields[1], path))
    return entries


def _reject_unsafe_entries(entries: list[tuple[str, str]]) -> None:
    """Pfade (`.git`) und Modi prüfen: Symlinks zeigen aus dem Worktree heraus (spätere Host-Schritte
    würden Host-Dateien lesen), Gitlinks sind Submodule — beides legt ein Agent-Ergebnis nicht an."""
    _reject_unsafe_paths([path for _, path in entries])
    for mode, path in entries:
        if mode in _FORBIDDEN_MODES:
            kind = "Symlink" if mode == "120000" else "Submodul"
            raise SandboxError(f"Ergebnis abgewiesen: {kind} {path!r} (Modus {mode})")


def apply_result(ex: Exchange, workdir: str) -> bool:
    """Ergebnis-Bundle des Containers in den Worktree übernehmen. False = kein Ergebnis.

    Raises:
        SandboxError: Bundle ungültig/kein regulärer File, Ergebnis stammt nicht vom
            Basis-Commit ab, unsicherer Pfad oder der Diff lässt sich nicht anwenden."""
    bundle = ex.result_bundle
    if not os.path.lexists(bundle):
        return False
    st = os.lstat(bundle)
    if not stat.S_ISREG(st.st_mode):  # kein Symlink auf Host-Dateien
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
    """Refs und temporäres Verzeichnis entfernen (best effort, idempotent)."""
    for ref in (ex.ref, ex.result_ref):
        subprocess.run(
            ["git", *_GIT_QUIET_CONFIG, "update-ref", "-d", ref], cwd=workdir, capture_output=True, check=False
        )
    shutil.rmtree(ex.tmpdir, ignore_errors=True)


def finish_exchange(ex: Exchange, workdir: str) -> str | None:
    """Nach Prozessende: Ergebnis übernehmen und aufräumen. Fehlermeldung oder None.

    Der Container wird vorher beendet: nach Timeout/Kill der docker-CLI kann er weiterlaufen
    und `/out` noch beschreiben, während wir lesen."""
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
    """`docker run`-argv für einen Agent-Run (Hardening-Flags, Austausch, s. Modul-Docstring).

    Secrets stehen NICHT im argv (sichtbar in `ps`/`docker inspect`): `-e KEY` ohne Wert lässt
    docker den Wert aus der Env des CLI-Prozesses lesen — der Aufrufer startet ihn mit `env`
    (+ `docker_cli_env()`). Nur die festen Container-Defaults stehen als `KEY=VALUE` im argv.

    Env-Regel: Host-Pfad-Variablen (`SANDBOX_ENV_STRIP`) werden rausgenommen —
    sie zeigen auf Host-Pfade, die auf der read-only Root-FS nicht existieren
    (HOME=/root) oder die Image-Binaries verdecken (Host-PATH). Danach stehen
    die Container-Defaults (`SANDBOX_ENV_OVERRIDES`) LETZTE — Docker wendet bei
    doppeltem `-e` das letzte an, das gewinnt also."""
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-i",  # stdin anhängen: der Prompt kommt via stdin (_oneshot.send_prompt)
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
        cfg.memory,  # = memory: kein Swap
        "--pull",
        "never",  # das Image ist ein Deployment-Artefakt; kein stilles Nachladen zur Laufzeit
        "--cpus",
        cfg.cpus,
        "--pids-limit",
        str(cfg.pids_limit),
        "--read-only",
        "--tmpfs",
        f"/tmp:size={SMALL_TMPFS_SIZE}",  # nosec B108 # tmpfs im Agent-Container, nicht das Host-/tmp
        # HOME der Agent-CLIs (Konfiguration, Caches). Eigene tmpfs-Optionen → mode explizit,
        # sonst root:755 und für den Agent-User nicht beschreibbar.
        "--tmpfs",
        f"{SANDBOX_ENV_OVERRIDES['HOME']}:rw,mode=1777,size={SMALL_TMPFS_SIZE}",
        # Arbeitsklon: exec (Build-/Test-Tools des Agenten), Größe begrenzt.
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
    for key in sorted(env):
        if key in SANDBOX_ENV_STRIP:
            continue
        cmd += ["-e", key]
    for key, value in SANDBOX_ENV_OVERRIDES.items():
        cmd += ["-e", f"{key}={value}"]
    if cfg.proxy_url:
        # Groß- und Kleinschreibung: curl/wget lesen nur `http_proxy`, andere nur die großen.
        for name, value in (
            ("HTTP_PROXY", cfg.proxy_url),
            ("HTTPS_PROXY", cfg.proxy_url),
            ("NO_PROXY", NO_PROXY_HOSTS),
        ):
            cmd += ["-e", f"{name}={value}", "-e", f"{name.lower()}={value}"]
    cmd += ["--entrypoint", "sh", cfg.image, "-c", entrypoint_script(), "fabrik-sandbox", exchange.ref]
    cmd.extend(agent_cmd)
    return cmd


_DOCKER_CLI_ENV_PREFIXES = ("DOCKER_",)
_DOCKER_CLI_ENV_NAMES = ("XDG_RUNTIME_DIR",)  # rootless-Docker-Socket


def docker_cli_env(base: Mapping[str, str], host: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env für den `docker`-CLI-Prozess: die gescrubte Agent-Env (`base`, aus der docker die
    `-e KEY`-Werte liest) plus die Variablen, die die CLI selbst zum Finden des Daemons braucht
    (`DOCKER_HOST`/`DOCKER_CONFIG`/`DOCKER_CONTEXT`, `XDG_RUNTIME_DIR`). Diese gelangen NICHT
    in den Container (dafür gibt es nur explizite `-e`-Flags)."""
    host = os.environ if host is None else host
    extra = {k: v for k, v in host.items() if k.startswith(_DOCKER_CLI_ENV_PREFIXES) or k in _DOCKER_CLI_ENV_NAMES}
    return {**base, **extra}


def stop_container(session_id: str) -> None:
    """Best-effort `docker rm -f` — idempotent (no-op, wenn der Container weg ist)."""
    # Docker-Daemon hängt: cancel() soll nicht mit hängen.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):  # OSError: kein docker installiert
        subprocess.run(
            ["docker", "rm", "-f", container_name(session_id)],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )


def sweep_stale_containers() -> int:
    """Entfernt verwaiste Agent-Container (Crash/Host-Reboot); Anzahl entfernter zurück.

    NUR beim Server-Start vor dem ersten Run aufrufen — sonst würden aktive Runs getroffen.
    Best-effort: kein docker/Timeout/Fehler → Warnung, nie ein Abbruch des Starts."""
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
