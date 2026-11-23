"""Egress default-deny — agent internet access only via the allowlist proxy (ADR 0051).

A prompt injection (OWASP ASI01/ASI05) can make the agent process exfiltrate data
(HTTP/DNS tunneling), call arbitrary APIs or read cloud metadata
(`169.254.169.254`). The control is not in the prompt but in the platform:
the agent container is attached to `fabrik-sandbox` (Docker `internal` — no direct
internet access), and all egress goes through a Tinyproxy container with a
default-deny domain filter. Every denied request is logged and is an
injection signal → alert (see `collect_denied`).

Deployment: `fabrik egress start` creates the networks and starts the proxy.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .i18n import t

# ------------------------------------------------------------- Konstanten
PROXY_CONTAINER = "fabrik-egress-proxy"
EGRESS_NET = "fabrik-egress"  # Bridge with internet (the proxy container lives here)
SANDBOX_NET = "fabrik-sandbox"  # internal: kein direkter Internetzugang (Agent-Container)
PROXY_PORT_IN_CONTAINER = 8888
PROXY_MEMORY = "256m"
PROXY_PIDS_LIMIT = 128

# Pinned subnets (fixed at `docker network create --subnet` — the
# rendered `Allow` lines reference exactly these values).
EGRESS_SUBNET = "172.27.0.0/16"  # Bridge: Host-Clienten via published Port kommen aus diesem Netz
SANDBOX_SUBNET = "172.28.0.0/16"  # internal: Agent-Container

# Tinyproxy-Konfiguration (Pfade im Proxy-Container; linuxserver-Bild-Konvention /log).
CONF_PATH = "/etc/tinyproxy/tinyproxy.conf"
FILTER_PATH = "/etc/tinyproxy/filter.txt"
LOG_PATH = "/log/tinyproxy.log"
LOG_NAME = "tinyproxy.log"

# Basis-Allowlist (sicherer Minimal-Default; Deployment darf erweitern).
BUILTIN_ALLOWLIST: tuple[str, ...] = (
    "github.com",  # API, clone, releases (incl. api.github.com as subdomain)
    "models.dev",  # opencode: Model-Metadaten
    "api.anthropic.com",  # claude-code (subdomains included)
    "api.openai.com",  # codex
)

PORT_VAR = "FABRIK_EGRESS_PORT"
IMAGE_VAR = "FABRIK_EGRESS_IMAGE"
ALLOWLIST_VAR = "FABRIK_EGRESS_ALLOWLIST"
ALLOWLIST_FILE_VAR = "FABRIK_EGRESS_ALLOWLIST_FILE"
EGRESS_SUBNET_VAR = "FABRIK_EGRESS_SUBNET"
SANDBOX_SUBNET_VAR = "FABRIK_SANDBOX_SUBNET"
DEFAULT_PORT = 8899
# Pinned by digest (tinyproxy 1.11.0; config directives are version-dependent, and the
# proxy is security-critical). New digest = deliberate update including a test with a real proxy.
DEFAULT_IMAGE = "vimagick/tinyproxy@sha256:72b441b95ee1e641af948f68f09492f9f795ead72b73954414e339168c98ad8c"
DOCKER_TIMEOUT = 60  # s, pro docker-Aufruf
READY_TIMEOUT = 15.0  # s, how long `start` waits for the proxy port
READY_POLL_INTERVAL = 0.25  # s

# Real Tinyproxy log format (1.11): the source IP is in the "Connect" line, the domain in the
# "refused" line; both are linked via the file descriptor of the "Request" line.
_CONNECT_RE = re.compile(r"^CONNECT\s+(.+?) \[\d+\]: Connect \(file descriptor (\d+)\): (\S+)")
_REQUEST_RE = re.compile(r"^CONNECT\s+.+? \[\d+\]: Request \(file descriptor (\d+)\):")
_REFUSED_RE = re.compile(r'^NOTICE\s+(.+?) \[\d+\]: Proxying refused on filtered domain "([^"]*)"')
_MAX_TARGET_LEN = 253  # DNS maximum; the target comes from the (untrusted) agent
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Hostname: at least two LDH labels, TLD starts with a letter (excludes IP literals and
# TLD-only entries like `com` — those would allow entire TLDs).
_HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass
class DeniedEvent:
    """A denied request in the proxy log — an injection signal."""

    ts: str
    source: str  # Source IP, "" if not attributable
    target: str


@dataclass
class EgressConfig:
    """Proxy configuration (env overrides in `from_env`)."""

    port: int = DEFAULT_PORT
    image: str = DEFAULT_IMAGE
    allowlist: tuple[str, ...] = BUILTIN_ALLOWLIST
    egress_subnet: str = EGRESS_SUBNET
    sandbox_subnet: str = SANDBOX_SUBNET
    conf_dir: Path = field(default_factory=lambda: config.STATE_DIR / "egress")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EgressConfig:
        env = os.environ if env is None else env
        raw_port = env.get(PORT_VAR, "") or str(DEFAULT_PORT)
        try:
            port = int(raw_port)
        except ValueError:
            raise RuntimeError(t("egress.must_be_a_number_currently", v1=PORT_VAR, raw_port=raw_port)) from None
        if not 1 <= port <= 65535:
            raise RuntimeError(t("egress.outside_1_65535_currently", v1=PORT_VAR, port=port))
        cfg = cls(
            port=port,
            image=env.get(IMAGE_VAR, "") or DEFAULT_IMAGE,
            allowlist=tuple(load_allowlist(env)),
            egress_subnet=_validated_subnet(EGRESS_SUBNET_VAR, env.get(EGRESS_SUBNET_VAR, "") or EGRESS_SUBNET),
            sandbox_subnet=_validated_subnet(SANDBOX_SUBNET_VAR, env.get(SANDBOX_SUBNET_VAR, "") or SANDBOX_SUBNET),
        )
        if ipaddress.ip_network(cfg.egress_subnet).overlaps(ipaddress.ip_network(cfg.sandbox_subnet)):
            raise RuntimeError(t("egress.and_must_not_overlap", v1=EGRESS_SUBNET_VAR, v2=SANDBOX_SUBNET_VAR))
        return cfg

    @property
    def client_subnets(self) -> tuple[str, ...]:
        """Allowed *clients*: sandbox network (agent containers) + egress bridge
        (host clients via published port — the connection comes from the
        bridge gateway, not from 127.0.0.1)."""
        return (self.sandbox_subnet, self.egress_subnet)

    @property
    def agent_proxy_url(self) -> str:
        """Proxy URL from the agent containers' point of view (name in the sandbox network, container port)."""
        return agent_proxy_url()

    @property
    def log_path(self) -> Path:
        return self.conf_dir / "log" / "tinyproxy.log"


def agent_proxy_url() -> str:
    """`HTTP(S)_PROXY` for agent containers: the proxy container in the sandbox network."""
    return f"http://{PROXY_CONTAINER}:{PROXY_PORT_IN_CONTAINER}"


def _validated_subnet(var: str, value: str) -> str:
    try:
        return str(ipaddress.ip_network(value.strip(), strict=True))
    except ValueError as e:
        raise RuntimeError(t("egress.is_not_a_valid_cidr", var=var, value=value, e=e)) from None


def normalize_domain(raw: str) -> str:
    """Allowlist entry → validated, lowercase hostname. A `*.`/`.` prefix is
    tolerated (subdomains are included anyway). Raises RuntimeError on invalid entries
    — fail-closed at start instead of silently too wide or ineffective filters."""
    host = raw.strip().lower()
    host = host.removeprefix("*.").removeprefix(".")
    if not _HOST_RE.match(host):
        raise RuntimeError(t("egress.invalid_allowlist_entry_expected_hostname", raw=raw))
    return host


# ------------------------------------------------------------- Allowlist
def parse_allowlist(text: str) -> list[str]:
    """Allowlist text → domains (comments `#`/blank lines removed, duplicates dropped, order kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def load_allowlist(env: Mapping[str, str] | None = None) -> list[str]:
    """Base allowlist + file (`FABRIK_EGRESS_ALLOWLIST_FILE`, optional; template:
    `deploy/egress-allowlist.txt`) + env (`FABRIK_EGRESS_ALLOWLIST`, comma-separated).
    All entries are validated (`normalize_domain`)."""
    env = os.environ if env is None else env
    hosts: list[str] = []
    seen: set[str] = set()

    def add(h: str) -> None:
        if not h.strip():
            return
        h = normalize_domain(h)
        if h not in seen:
            seen.add(h)
            hosts.append(h)

    for h in BUILTIN_ALLOWLIST:
        add(h)
    file = env.get(ALLOWLIST_FILE_VAR, "")
    if file:
        p = Path(file)
        if not p.is_file():
            raise RuntimeError(t("egress.is_not_a_file", v1=ALLOWLIST_FILE_VAR, file=file))
        for h in parse_allowlist(p.read_text()):
            add(h)
    for h in env.get(ALLOWLIST_VAR, "").split(","):
        add(h)
    return hosts


# ------------------------------------------------------------- proxy config
# Filter patterns are POSIX ERE. Tinyproxy 1.11.0 knows `FilterExtended Yes` for this
# (`FilterType ere` was added later and fails there with "Syntax error").
FILTER_TYPE_LINE = "FilterExtended Yes"


def _filter_pattern(domain: str) -> str:
    """Tinyproxy filter pattern (ERE, exact + subdomains, without suffix overlap:
    `github.com` must NOT match `github.com.attacker.net`). Tinyproxy does
    substring matching → anchor both ends."""
    esc = re.sub(r"([.*+?^${}()|\\])", r"\\\1", domain)
    return f"^([a-z0-9-]+\\.)*{esc}$"


def render_filter_file(cfg: EgressConfig) -> str:
    """Tinyproxy filter FILE (one ERE per line). `Filter` in the config points
    to this file — an inline `Filter "domain"` would be a (wrong) file path."""
    return "\n".join(_filter_pattern(h) for h in cfg.allowlist) + "\n"


def render_proxy_config(cfg: EgressConfig) -> str:
    """Tinyproxy config. Default deny = `FilterDefaultDeny Yes` + filter file
    (without both, Tinyproxy would allow EVERYTHING). `FilterExtended Yes` is mandatory:
    Tinyproxy's default is BRE, where `(`, `)` and `+` are literals — the patterns
    from `_filter_pattern` would match no host, everything would be blocked. `Allow`
    restricts the *clients* to the pinned bridge subnets."""
    lines = [
        f"Port {PROXY_PORT_IN_CONTAINER}",
        "Listen 0.0.0.0",
        "Timeout 600",
        f'LogFile "{LOG_PATH}"',
        "MaxClients 50",
        "ConnectPort 443",
        "ConnectPort 80",
        "",
        t("egress.clients_only_the_pinned_bridge"),
    ]
    for subnet in cfg.client_subnets:
        lines.append(f"Allow {subnet}")
    lines += [
        "",
        t("egress.default_deny_only_domains_from"),
        f'Filter "{FILTER_PATH}"',
        FILTER_TYPE_LINE,
        "FilterDefaultDeny Yes",
    ]
    return "\n".join(lines) + "\n"


def write_proxy_config(cfg: EgressConfig) -> tuple[Path, Path, Path]:
    """Write (tinyproxy.conf, filter.txt, log dir) — all 0600/0700 owned by the host user.

    The proxy container runs as the same uid:gid (`start`), without capabilities: it reads
    config/filter and writes the log as owner. (As root without `CAP_DAC_OVERRIDE` it
    could not enter the 0700 directory — the log would stay silently empty; and an
    unreadable filter + default deny silently blocks everything. Both observed with a real proxy.)
    The log file is created beforehand so fabrik can read it as well."""
    cfg.conf_dir.mkdir(parents=True, exist_ok=True)
    conf = cfg.conf_dir / "tinyproxy.conf"
    conf.write_text(render_proxy_config(cfg))
    conf.chmod(0o600)
    flt = cfg.conf_dir / "filter.txt"
    flt.write_text(render_filter_file(cfg))
    flt.chmod(0o600)
    log_dir = cfg.conf_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)
    (log_dir / LOG_NAME).touch(mode=0o600)
    return conf, flt, log_dir


# ------------------------------------------------------------- Docker
def _docker(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError(t("egress.docker_not_found_the_egress")) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"docker {args[0]} Timeout nach {DOCKER_TIMEOUT}s") from None


def _docker_rootless() -> bool:
    """Does the Docker daemon run rootless? (`docker info` → SecurityOptions `name=rootless`).

    Error (no docker, timeout) → False: then rootful behavior applies."""
    try:
        p = subprocess.run(
            ["docker", "info", "--format", "{{json .SecurityOptions}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "name=rootless" in p.stdout


def proxy_user(rootless: bool | None = None) -> str:
    """`-u` for the proxy container: it must be able to read or write its mounts (config/filter `0600`, log folder `0700`,
    owner = host user).

    rootful: host uid:gid (inside the container = same user). rootless: `0:0` — in the user namespace of the
    rootless daemon container root IS the host user; any other uid would land on a subuid without
    access to the mounts. Without capabilities (`--cap-drop ALL`) this root has no special rights."""
    if rootless is None:
        rootless = _docker_rootless()
    return "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"


def _ensure_network(name: str, subnet: str, *, internal: bool) -> None:
    """Create a network; if it already exists, it MUST match subnet and `internal` —
    otherwise the isolation (or the `Allow` lines) would rest on a foreign network."""
    flags = ["--internal"] if internal else []
    r = _docker("network", "create", *flags, "--subnet", subnet, name)
    if r.returncode == 0:
        return
    if "already exists" not in r.stderr:
        raise RuntimeError(f"docker network create {name} fehlgeschlagen: {r.stderr.strip()}")
    r = _docker("network", "inspect", "-f", "{{.Internal}} {{range .IPAM.Config}}{{.Subnet}} {{end}}", name)
    if r.returncode != 0:
        raise RuntimeError(f"docker network inspect {name} fehlgeschlagen: {r.stderr.strip()}")
    is_internal, *subnets = r.stdout.split()
    if (is_internal == "true") != internal or subnet not in subnets:
        raise RuntimeError(
            t(
                "egress.network_exists_with_a_different",
                name=name,
                is_internal=is_internal,
                subnets=subnets,
                v4=str(internal).lower(),
                subnet=subnet,
            )
        )


def ensure_networks(cfg: EgressConfig) -> None:
    """Create or check `fabrik-egress` (bridge) + `fabrik-sandbox` (internal).
    Subnets are PINNED — the `Allow` lines in the proxy config reference
    exactly these values (without a pin Docker allocates from the default pool, and
    rendered `Allow` lines would never hit the real container IPs)."""
    _ensure_network(EGRESS_NET, cfg.egress_subnet, internal=False)
    _ensure_network(SANDBOX_NET, cfg.sandbox_subnet, internal=True)


def start(cfg: EgressConfig) -> str:
    """Proxy-Container starten (bestehenden ersetzen) → Container-Name."""
    ensure_networks(cfg)
    conf, flt, log_dir = write_proxy_config(cfg)
    _docker("rm", "-f", PROXY_CONTAINER)  # idempotent
    r = _docker(
        "run",
        "-d",
        "--name",
        PROXY_CONTAINER,
        "--restart",
        "unless-stopped",
        # The proxy processes untrusted traffic: same hardening as the agent containers;
        # runs as host user (reads/writes its mounts as owner), without capabilities.
        "-u",
        proxy_user(),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:size=16m",  # nosec B108 # tmpfs in the proxy container, not the host /tmp
        "--memory",
        PROXY_MEMORY,
        "--pids-limit",
        str(PROXY_PIDS_LIMIT),
        "--network",
        EGRESS_NET,
        "-p",
        f"127.0.0.1:{cfg.port}:{PROXY_PORT_IN_CONTAINER}",
        "-v",
        f"{conf}:{CONF_PATH}:ro",
        "-v",
        f"{flt}:{FILTER_PATH}:ro",
        "-v",
        f"{log_dir}:/log",
        cfg.image,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker run {PROXY_CONTAINER} fehlgeschlagen: {r.stderr.strip()}")
    # Second network afterwards (`docker run` takes only one --network): the proxy also listens in the sandbox network.
    r = _docker("network", "connect", SANDBOX_NET, PROXY_CONTAINER)
    if r.returncode != 0:
        _docker("rm", "-f", PROXY_CONTAINER)
        raise RuntimeError(f"docker network connect {SANDBOX_NET} fehlgeschlagen: {r.stderr.strip()}")
    _wait_ready(cfg)
    return PROXY_CONTAINER


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _wait_ready(cfg: EgressConfig) -> None:
    """Health check after `docker run -d`: container runs AND the proxy port answers.

    Otherwise fail loudly (with container log) and remove the container — a dead proxy must not
    silently count as "egress active"."""
    deadline = time.monotonic() + READY_TIMEOUT
    while True:
        if not is_running():
            break
        if _port_open(cfg.port):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(READY_POLL_INTERVAL)
    logs = _docker("logs", "--tail", "20", PROXY_CONTAINER)
    detail = (logs.stdout + logs.stderr).strip()
    _docker("rm", "-f", PROXY_CONTAINER)
    raise RuntimeError(
        t("egress.not_reachable_on_127_0", v1=PROXY_CONTAINER, v2=cfg.port, v3=detail or t("egress.no_log"))
    )


def stop() -> None:
    _docker("rm", "-f", PROXY_CONTAINER)  # idempotent


def is_running() -> bool:
    r = _docker("inspect", "-f", "{{.State.Running}}", PROXY_CONTAINER)
    return r.returncode == 0 and r.stdout.strip() == "true"


def status() -> str:
    """Menschenlesbarer Status (CLI `fabrik egress status`)."""
    if is_running():
        cfg = EgressConfig.from_env()
        return t(
            "egress.running_on_127_0_0",
            v1=PROXY_CONTAINER,
            v2=cfg.port,
            v3=EGRESS_NET,
            v4=SANDBOX_NET,
            v5=len(cfg.allowlist),
        )
    return t("egress.is_not_running_fabrik_egress", v1=PROXY_CONTAINER)


# ------------------------------------------------------------- Denied-Log
def _clean_target(raw: str) -> str:
    """The target comes from the agent (untrusted): control characters removed, length limited — before it
    lands in alerts/triage prompts."""
    return _CTRL_RE.sub("", raw)[:_MAX_TARGET_LEN]


def parse_denied(lines: list[str]) -> list[DeniedEvent]:
    """Tinyproxy log lines → denied events (other lines are ignored).

    The source IP is assigned best effort via the file descriptor (last
    request line before the "refused" line); with heavily interleaved lines it may be missing."""
    out: list[DeniedEvent] = []
    fd_source: dict[str, str] = {}
    last_fd = ""
    for line in lines:
        if m := _CONNECT_RE.match(line):
            fd_source[m.group(2)] = m.group(3)
        elif m := _REQUEST_RE.match(line):
            last_fd = m.group(1)
        elif m := _REFUSED_RE.match(line):
            out.append(DeniedEvent(ts=m.group(1), source=fd_source.get(last_fd, ""), target=_clean_target(m.group(2))))
    return out


def _read_offset(state_file: Path) -> int:
    try:
        return max(0, int(state_file.read_text().strip() or 0))
    except (OSError, ValueError):
        return 0


def collect_denied(log_path: Path, state_file: Path, max_events: int = 50) -> list[DeniedEvent]:
    """New denied events since the last reconciliation (byte offset in `state_file`), deduplicated
    per (source IP, target) within the reconciliation. Without a log file: empty.

    Only complete lines are consumed (a half-written line stays for the
    next reconciliation). An unreadable log is an error (OSError), not a silent
    "nothing happened"."""
    offset = _read_offset(state_file)
    events: list[DeniedEvent] = []
    if log_path.is_file():
        if log_path.stat().st_size < offset:  # Log rotiert/geschnitten
            offset = 0
        with log_path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1]
        offset += len(complete)
        events = parse_denied(complete.decode(errors="replace").splitlines())
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(str(offset))
    tmp.replace(state_file)
    seen: set[tuple[str, str]] = set()
    deduped: list[DeniedEvent] = []
    for e in events:
        key = (e.source, e.target)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    return deduped[-max_events:]
