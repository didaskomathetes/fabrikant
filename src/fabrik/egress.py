"""Egress default-deny — Agent-Internetzugang nur über den Allowlist-Proxy (ADR 0051).

Eine Prompt-Injection (OWASP ASI01/ASI05) kann den Agent-Prozess zu Datenabfluss
auffordern (HTTP-/DNS-Tunneling), arbitrary APIs aufrufen oder Cloud-Metadaten lesen
(`169.254.169.254`). Die Kontrolle sitzt nicht im Prompt, sondern in der Plattform:
Der Agent-Container hängt an `fabrik-sandbox` (Docker `internal` — kein direkter
Internetzugang), und der gesamte Egress läuft durch einen Tinyproxy-Container mit
Default-Deny-Domänenfilter. Jede Denied-Request wird geloggt und ist ein
Injection-Signal → Alert (siehe `collect_denied`).

Deployment: `fabrik egress start` legt die Netzwerke an und startet den Proxy.
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
EGRESS_NET = "fabrik-egress"  # Bridge mit Internet (Proxy-Container lebt hier)
SANDBOX_NET = "fabrik-sandbox"  # internal: kein direkter Internetzugang (Agent-Container)
PROXY_PORT_IN_CONTAINER = 8888
PROXY_MEMORY = "256m"
PROXY_PIDS_LIMIT = 128

# Gepinnte Subnets (bei `docker network create --subnet` fixiert — die
# gerenderten `Allow`-Zeilen referenzieren genau diese Werte).
EGRESS_SUBNET = "172.27.0.0/16"  # Bridge: Host-Clienten via published Port kommen aus diesem Netz
SANDBOX_SUBNET = "172.28.0.0/16"  # internal: Agent-Container

# Tinyproxy-Konfiguration (Pfade im Proxy-Container; linuxserver-Bild-Konvention /log).
CONF_PATH = "/etc/tinyproxy/tinyproxy.conf"
FILTER_PATH = "/etc/tinyproxy/filter.txt"
LOG_PATH = "/log/tinyproxy.log"
LOG_NAME = "tinyproxy.log"

# Basis-Allowlist (sicherer Minimal-Default; Deployment darf erweitern).
BUILTIN_ALLOWLIST: tuple[str, ...] = (
    "github.com",  # API, Clone, releases (inkl. api.github.com als Subdomäne)
    "models.dev",  # opencode: Model-Metadaten
    "api.anthropic.com",  # claude-code (Subdomänen inklusive)
    "api.openai.com",  # codex
)

PORT_VAR = "FABRIK_EGRESS_PORT"
IMAGE_VAR = "FABRIK_EGRESS_IMAGE"
ALLOWLIST_VAR = "FABRIK_EGRESS_ALLOWLIST"
ALLOWLIST_FILE_VAR = "FABRIK_EGRESS_ALLOWLIST_FILE"
EGRESS_SUBNET_VAR = "FABRIK_EGRESS_SUBNET"
SANDBOX_SUBNET_VAR = "FABRIK_SANDBOX_SUBNET"
DEFAULT_PORT = 8899
# Per Digest gepinnt (tinyproxy 1.11.0; Config-Direktiven sind versionsabhängig, und der
# Proxy ist sicherheitskritisch). Neuer Digest = bewusstes Update samt Test mit echtem Proxy.
DEFAULT_IMAGE = "vimagick/tinyproxy@sha256:72b441b95ee1e641af948f68f09492f9f795ead72b73954414e339168c98ad8c"
DOCKER_TIMEOUT = 60  # s, pro docker-Aufruf
READY_TIMEOUT = 15.0  # s, so lange wartet `start` auf den Proxy-Port
READY_POLL_INTERVAL = 0.25  # s

# Echtes Tinyproxy-Logformat (1.11): Quell-IP steht in der „Connect"-Zeile, die Domäne in der
# „refused"-Zeile; beide hängen über den File-Descriptor der „Request"-Zeile zusammen.
_CONNECT_RE = re.compile(r"^CONNECT\s+(.+?) \[\d+\]: Connect \(file descriptor (\d+)\): (\S+)")
_REQUEST_RE = re.compile(r"^CONNECT\s+.+? \[\d+\]: Request \(file descriptor (\d+)\):")
_REFUSED_RE = re.compile(r'^NOTICE\s+(.+?) \[\d+\]: Proxying refused on filtered domain "([^"]*)"')
_MAX_TARGET_LEN = 253  # DNS-Maximum; das Ziel stammt vom (nicht vertrauenswürdigen) Agent
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Hostname: mind. zwei LDH-Labels, TLD beginnt mit Buchstaben (schließt IP-Literale und
# TLD-only-Einträge wie `com` aus — die würden ganze TLDs freigeben).
_HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass
class DeniedEvent:
    """Eine Denied-Request im Proxy-Log — ein Injection-Signal."""

    ts: str
    source: str  # Quell-IP, "" wenn nicht zuordenbar
    target: str


@dataclass
class EgressConfig:
    """Proxy-Konfiguration (Env-Überschreibungen in `from_env`)."""

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
        """Erlaubte *Clients*: Sandbox-Netz (Agent-Container) + Egress-Bridge
        (Host-Clienten via published Port — die Verbindung kommt aus dem
        Bridge-Gateway, nicht aus 127.0.0.1)."""
        return (self.sandbox_subnet, self.egress_subnet)

    @property
    def agent_proxy_url(self) -> str:
        """Proxy-URL aus Sicht der Agent-Container (Name im Sandbox-Netz, Container-Port)."""
        return agent_proxy_url()

    @property
    def log_path(self) -> Path:
        return self.conf_dir / "log" / "tinyproxy.log"


def agent_proxy_url() -> str:
    """`HTTP(S)_PROXY` für Agent-Container: der Proxy-Container im Sandbox-Netz."""
    return f"http://{PROXY_CONTAINER}:{PROXY_PORT_IN_CONTAINER}"


def _validated_subnet(var: str, value: str) -> str:
    try:
        return str(ipaddress.ip_network(value.strip(), strict=True))
    except ValueError as e:
        raise RuntimeError(t("egress.is_not_a_valid_cidr", var=var, value=value, e=e)) from None


def normalize_domain(raw: str) -> str:
    """Allowlist-Eintrag → validierter, kleingeschriebener Hostname. `*.`/`.`-Präfix wird
    toleriert (Subdomänen sind ohnehin inklusive). Raises RuntimeError bei ungültigen Einträgen
    — fail-closed beim Start statt still zu breite oder wirkungslose Filter."""
    host = raw.strip().lower()
    host = host.removeprefix("*.").removeprefix(".")
    if not _HOST_RE.match(host):
        raise RuntimeError(t("egress.invalid_allowlist_entry_expected_hostname", raw=raw))
    return host


# ------------------------------------------------------------- Allowlist
def parse_allowlist(text: str) -> list[str]:
    """Allowlist-Text → Domänen (Kommentare `#`/Leerzeilen weg, Duplikate raus, Reihenfolge)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def load_allowlist(env: Mapping[str, str] | None = None) -> list[str]:
    """Basis-Allowlist + Datei (`FABRIK_EGRESS_ALLOWLIST_FILE`, optional; Vorlage:
    `deploy/egress-allowlist.txt`) + Env (`FABRIK_EGRESS_ALLOWLIST`, kommasetrennt).
    Alle Einträge werden validiert (`normalize_domain`)."""
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


# ------------------------------------------------------------- Proxy-Config
# Filter-Muster sind POSIX-ERE. Tinyproxy 1.11.0 kennt dafür `FilterExtended Yes`
# (`FilterType ere` ist erst später dazugekommen und scheitert dort mit „Syntax error").
FILTER_TYPE_LINE = "FilterExtended Yes"


def _filter_pattern(domain: str) -> str:
    """Tinyproxy-Filter-Muster (ERE, exakt + Subdomänen, ohne Suffix-Überschneidung:
    `github.com` darf `github.com.attacker.net` NICHT matchen). Tinyproxy macht
    substring-Matching → beide Enden ankern."""
    esc = re.sub(r"([.*+?^${}()|\\])", r"\\\1", domain)
    return f"^([a-z0-9-]+\\.)*{esc}$"


def render_filter_file(cfg: EgressConfig) -> str:
    """Tinyproxy-Filter-DATEI (eine ERE pro Zeile). `Filter` in der Config zeigt
    auf diese Datei — Inline-`Filter "domain"` wäre ein (falscher) Dateipfad."""
    return "\n".join(_filter_pattern(h) for h in cfg.allowlist) + "\n"


def render_proxy_config(cfg: EgressConfig) -> str:
    """Tinyproxy-Config. Default-Deny = `FilterDefaultDeny Yes` + Filter-Datei
    (ohne beides würde Tinyproxy ALLES erlauben). `FilterExtended Yes` ist Pflicht:
    Tinyproxys Default ist BRE, dort sind `(`, `)` und `+` Literale — die Muster
    aus `_filter_pattern` träfen keinen Host, alles wäre gesperrt. `Allow`
    beschränkt die *Clients* auf die gepinnten Bridge-Subnets."""
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
    """(tinyproxy.conf, filter.txt, log-Dir) schreiben — alles 0600/0700 beim Host-User.

    Der Proxy-Container läuft als derselbe uid:gid (`start`), ohne Capabilities: er liest
    Config/Filter und schreibt das Log als Eigentümer. (Als root ohne `CAP_DAC_OVERRIDE`
    könnte er das 0700-Verzeichnis nicht betreten — das Log bliebe stumm leer; und ein
    nicht lesbarer Filter + Default-Deny sperrt still alles. Beides mit echtem Proxy beobachtet.)
    Die Logdatei wird vorab angelegt, damit fabrik sie auch dann lesen kann."""
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
    """Läuft der Docker-Daemon rootless? (`docker info` → SecurityOptions `name=rootless`).

    Fehler (kein docker, Timeout) → False: dann gilt das rootful-Verhalten."""
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
    """`-u` für den Proxy-Container: er muss seine Mounts (Config/Filter `0600`, Log-Ordner `0700`,
    Eigentümer = Host-User) lesen bzw. schreiben können.

    rootful: Host-uid:gid (im Container = derselbe User). rootless: `0:0` — im User-Namespace des
    rootless Daemons IST Container-root der Host-User; eine andere uid landete auf einer subuid ohne
    Zugriff auf die Mounts. Ohne Capabilities (`--cap-drop ALL`) hat dieser root keine Sonderrechte."""
    if rootless is None:
        rootless = _docker_rootless()
    return "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"


def _ensure_network(name: str, subnet: str, *, internal: bool) -> None:
    """Netzwerk anlegen; existiert es schon, MUSS es zu Subnetz und `internal` passen —
    sonst stünde die Isolation (oder die `Allow`-Zeilen) auf einem fremden Netz."""
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
    """`fabrik-egress` (Bridge) + `fabrik-sandbox` (internal) anlegen bzw. prüfen.
    Subnets sind GEPINNT — die `Allow`-Zeilen in der Proxy-Config referenzieren
    genau diese Werte (ohne Pin allokiert Docker aus dem Default-Pool, und
    gerenderte `Allow`-Zeilen träfen nie die echten Container-IPs)."""
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
        # Der Proxy verarbeitet untrusted Verkehr: gleiche Härtung wie die Agent-Container;
        # läuft als Host-User (liest/schreibt seine Mounts als Eigentümer), ohne Capabilities.
        "-u",
        proxy_user(),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:size=16m",  # nosec B108 # tmpfs im Proxy-Container, nicht das Host-/tmp
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
    # Zweites Netz nachträglich (`docker run` nimmt nur ein --network): Proxy hört auch im Sandbox-Netz.
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
    """Health-Check nach `docker run -d`: Container läuft UND Proxy-Port antwortet.

    Sonst fail-loud (mit Container-Log) und Container entfernen — ein toter Proxy darf nicht
    still als „Egress aktiv" gelten."""
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
    """Das Ziel stammt vom Agent (untrusted): Steuerzeichen raus, Länge begrenzt — bevor es
    in Alerts/Triage-Prompts landet."""
    return _CTRL_RE.sub("", raw)[:_MAX_TARGET_LEN]


def parse_denied(lines: list[str]) -> list[DeniedEvent]:
    """Tinyproxy-Log-Zeilen → Denied-Events (andere Zeilen werden ignoriert).

    Die Quell-IP wird best effort über den File-Descriptor zugeordnet (letzte
    Request-Zeile vor der „refused"-Zeile); bei stark verschränkten Zeilen kann sie fehlen."""
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
    """Neue Denied-Events seit letztem Abgleich (Byte-Offset in `state_file`), dedupliziert
    pro (Quell-IP, Ziel) innerhalb des Abgleichs. Ohne Log-Datei: leer.

    Nur vollständige Zeilen werden verbraucht (eine halb geschriebene Zeile bleibt für den
    nächsten Abgleich liegen). Ein nicht lesbares Log ist ein Fehler (OSError), kein stilles
    „nichts passiert"."""
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
