"""Egress default-deny (ADR 0051): Allowlist, Proxy-Config, Docker-Orchestrierung, Denied-Log."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from fabrik import egress


def test_parse_allowlist_strips_comments_and_dedupes():
    text = "github.com\n# Kommentar\n\napi.github.com\ngithub.com\n  huggingface.co  \n"
    assert egress.parse_allowlist(text) == ["github.com", "api.github.com", "huggingface.co"]


def test_load_allowlist_builtin_plus_env_and_file(tmp_path, monkeypatch):
    f = tmp_path / "allow.txt"
    f.write_text("huggingface.co\n# none\n")
    env = {egress.ALLOWLIST_FILE_VAR: str(f), egress.ALLOWLIST_VAR: "example.org, github.com"}
    hosts = egress.load_allowlist(env)
    assert hosts[: len(egress.BUILTIN_ALLOWLIST)] == list(egress.BUILTIN_ALLOWLIST)
    assert hosts[len(egress.BUILTIN_ALLOWLIST) :] == ["huggingface.co", "example.org"]  # Duplikat weg


# ---------------------------------------------------------------- Proxy-Config
def test_render_proxy_config_is_default_deny():
    cfg = egress.EgressConfig(allowlist=("github.com", "example.org"))
    text = egress.render_proxy_config(cfg)
    assert f"Port {egress.PROXY_PORT_IN_CONTAINER}" in text
    # Default-Deny-Semantik: Filter-DATEI + FilterDefaultDeny (ohne beides erlaubte Tinyproxy ALLES)
    assert f'Filter "{egress.FILTER_PATH}"' in text
    assert "FilterDefaultDeny Yes" in text
    # Muster sind ERE; ohne FilterType ere nutzt Tinyproxy BRE und sperrt alles
    assert egress.FILTER_TYPE_LINE in text.splitlines()
    # Inline-Filter wäre ein (falscher) Dateipfad; DefaultPort/StartServers sind keine Tinyproxy-Direktiven
    assert 'Filter "github.com"' not in text
    assert "DefaultPort" not in text
    assert "StartServers" not in text
    # Clients: nur die gepinnten Subnets (Sandbox = Agenten, Egress = Host via published Port)
    assert "Allow 172.28.0.0/16" in text
    assert "Allow 172.27.0.0/16" in text
    assert "Allow 127.0.0.1" not in text  # nützt nichts:.published-Port-Verbindungen kommen aus der Bridge
    # Metadata-Endpoint darf nicht erreichbar sein: kein Allow, keine Filter-Zeile für ihn.
    assert "169.254.169.254" not in text


def test_render_filter_file_anchors_patterns():
    text = egress.render_filter_file(egress.EgressConfig(allowlist=("github.com", "example.org")))
    assert "^([a-z0-9-]+\\.)*github\\.com$" in text
    assert "^([a-z0-9-]+\\.)*example\\.org$" in text
    # Keine nackten Domänen (Tinyproxy-Matching ist substring → ohne Anker matcht github.com.attacker.net)
    assert text.count("github") == 1


def test_filter_pattern_semantics():
    import re

    pat = re.compile(egress._filter_pattern("github.com"))
    assert pat.search("github.com")
    assert pat.search("api.github.com")
    assert pat.search("a.b.github.com")
    assert not pat.search("github.com.attacker.net")  # Suffix-Übergriff
    assert not pat.search("evilgithub.com")  # Präfix-Übergriff


@pytest.mark.skipif(shutil.which("grep") is None, reason="grep fehlt")
def test_filter_file_matches_with_posix_ere(tmp_path):
    """Muster gegen echtes POSIX-ERE (grep -E ≈ regcomp(REG_EXTENDED) in Tinyproxy)."""
    flt = tmp_path / "filter.txt"
    flt.write_text(egress.render_filter_file(egress.EgressConfig(allowlist=("github.com",))))
    hosts = ["github.com", "api.github.com", "github.com.attacker.net", "evilgithub.com"]
    r = subprocess.run(
        ["grep", "-E", "-i", "-f", str(flt)], input="\n".join(hosts) + "\n", capture_output=True, text=True
    )
    assert r.stdout.split() == ["github.com", "api.github.com"]


def test_from_env_reads_port_image_allowlist(monkeypatch):
    monkeypatch.setenv(egress.PORT_VAR, "9911")
    monkeypatch.setenv(egress.IMAGE_VAR, "my/proxy:1")
    monkeypatch.setenv(egress.ALLOWLIST_VAR, "example.org")
    cfg = egress.EgressConfig.from_env()
    assert cfg.port == 9911
    assert cfg.image == "my/proxy:1"
    assert "example.org" in cfg.allowlist
    assert "github.com" in cfg.allowlist  # Builtins bleiben


def test_from_env_invalid_port_raises_cleanly(monkeypatch):
    monkeypatch.setenv(egress.PORT_VAR, "achtzehn")
    with pytest.raises(RuntimeError, match=egress.PORT_VAR):
        egress.EgressConfig.from_env()
    monkeypatch.setenv(egress.PORT_VAR, "70000")
    with pytest.raises(RuntimeError, match=egress.PORT_VAR):
        egress.EgressConfig.from_env()


@pytest.mark.parametrize(
    "entry", ["com", "169.254.169.254", "localhost", "a/b.com", "git hub.com", "-x.com", "a[b]c.com", "x.123", ""]
)
def test_normalize_domain_rejects_broad_or_malformed_entries(entry):
    """`com` würde ganze TLDs freigeben, IP-Literale/Klammern verbiegen den Filter."""
    with pytest.raises(RuntimeError):
        egress.normalize_domain(entry)


def test_normalize_domain_accepts_hosts_and_tolerates_wildcard_prefix():
    assert egress.normalize_domain(" API.GitHub.com ") == "api.github.com"
    assert egress.normalize_domain("*.github.com") == "github.com"
    assert egress.normalize_domain(".github.com") == "github.com"


def test_load_allowlist_rejects_invalid_env_entry_and_missing_file(tmp_path):
    with pytest.raises(RuntimeError, match="Ungültiger"):
        egress.load_allowlist({egress.ALLOWLIST_VAR: "ok.example,com"})
    with pytest.raises(RuntimeError, match=egress.ALLOWLIST_FILE_VAR):
        egress.load_allowlist({egress.ALLOWLIST_FILE_VAR: str(tmp_path / "fehlt.txt")})


def test_from_env_validates_subnets():
    with pytest.raises(RuntimeError, match=egress.EGRESS_SUBNET_VAR):
        egress.EgressConfig.from_env({egress.EGRESS_SUBNET_VAR: "10.0.0.0/8\nAllow 0.0.0.0/0"})
    with pytest.raises(RuntimeError, match="überlappen"):
        egress.EgressConfig.from_env(
            {egress.EGRESS_SUBNET_VAR: "10.0.0.0/16", egress.SANDBOX_SUBNET_VAR: "10.0.1.0/24"}
        )


def test_agent_proxy_url_points_at_proxy_container():
    assert egress.EgressConfig().agent_proxy_url == f"http://{egress.PROXY_CONTAINER}:{egress.PROXY_PORT_IN_CONTAINER}"


def test_custom_subnets_flow_into_allow_and_network_create(tmp_path, monkeypatch):
    """Überschriebene Subnets müssen in `Allow`-Zeilen UND `network create
    --subnet` landen — sonst erlauben/pinnt die Config andere Netze, als die
    Agent-Container tatsächlich haben."""
    env = {egress.EGRESS_SUBNET_VAR: "10.44.0.0/24", egress.SANDBOX_SUBNET_VAR: "10.45.0.0/24"}
    cfg = egress.EgressConfig.from_env(env)
    assert cfg.client_subnets == ("10.45.0.0/24", "10.44.0.0/24")
    text = egress.render_proxy_config(cfg)
    assert "Allow 10.44.0.0/24" in text and "Allow 10.45.0.0/24" in text
    calls = _capture(monkeypatch)
    egress.ensure_networks(cfg)
    assert calls[0] == ["network", "create", "--subnet", "10.44.0.0/24", egress.EGRESS_NET]
    assert calls[1] == ["network", "create", "--internal", "--subnet", "10.45.0.0/24", egress.SANDBOX_NET]


def test_write_proxy_config_writes_conf_filter_and_logdir(tmp_path):
    cfg = egress.EgressConfig(conf_dir=tmp_path / "egress")
    conf, flt, log_dir = egress.write_proxy_config(cfg)
    assert conf.is_file() and flt.is_file() and log_dir.is_dir()
    assert oct(conf.stat().st_mode & 0o777) == "0o600"
    assert oct(flt.stat().st_mode & 0o777) == "0o600"
    assert oct(log_dir.stat().st_mode & 0o777) == "0o700"
    # Logdatei vorab angelegt (Eigentümer = Host-User, der Proxy läuft mit derselben uid).
    log_file = log_dir / egress.LOG_NAME
    assert log_file.is_file() and oct(log_file.stat().st_mode & 0o777) == "0o600"
    assert "FilterDefaultDeny Yes" in conf.read_text()
    assert "github" in flt.read_text()


def test_log_path_matches_container_mount(tmp_path):
    """Der in-Container-Logpfad muss auf `cfg.log_path` abbilden (Mount
    `{conf_dir}/log:/log`) — sonst findet `collect_denied` die Datei nie und
    die Denied-Auswertung (Injection-Signal) bleibt still tot."""
    cfg = egress.EgressConfig(conf_dir=tmp_path / "egress")
    host_log = cfg.log_path
    container_log = egress.LOG_PATH
    assert container_log == "/log/" + host_log.relative_to(cfg.conf_dir / "log").as_posix()
    egress.write_proxy_config(cfg)
    assert host_log.parent.is_dir(), "Log-Verzeichnis muss vor dem Proxy-Start existieren"


# ---------------------------------------------------------------- Docker
def _capture(monkeypatch):
    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = "true"
        stderr = ""

    monkeypatch.setattr(egress, "_docker", lambda *args: calls.append(list(args)) or R())
    return calls


def test_start_pins_subnets_hardens_proxy_and_mounts(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(egress, "_docker_rootless", lambda: False)
    monkeypatch.setattr(egress, "_port_open", lambda port: True)
    cfg = egress.EgressConfig(conf_dir=tmp_path)
    assert egress.start(cfg) == egress.PROXY_CONTAINER
    # Subnets sind GEpinnt (ohne Pin träfen die Allow-Zeilen nie die echten Container-IPs)
    assert calls[0] == ["network", "create", "--subnet", egress.EGRESS_SUBNET, egress.EGRESS_NET]
    assert calls[1] == ["network", "create", "--internal", "--subnet", egress.SANDBOX_SUBNET, egress.SANDBOX_NET]
    assert calls[2] == ["rm", "-f", egress.PROXY_CONTAINER]
    run = calls[3]
    assert run[0] == "run" and "-d" in run
    assert run[run.index("--network") + 1] == egress.EGRESS_NET
    assert calls[4] == ["network", "connect", egress.SANDBOX_NET, egress.PROXY_CONTAINER]
    # Härtung des (untrusted Verkehr verarbeitenden) Proxys
    assert run[run.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" not in run
    assert run[run.index("-u") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert run[run.index("--security-opt") + 1] == "no-new-privileges"
    assert "--read-only" in run and "--memory" in run and "--pids-limit" in run
    assert f"127.0.0.1:{cfg.port}:{egress.PROXY_PORT_IN_CONTAINER}" in run
    assert f"{tmp_path}/tinyproxy.conf:{egress.CONF_PATH}:ro" in run
    assert f"{tmp_path}/filter.txt:{egress.FILTER_PATH}:ro" in run
    assert f"{tmp_path}/log:/log" in run
    assert run[-1] == cfg.image


def test_start_fails_loud_and_removes_container_when_proxy_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "_port_open", lambda port: False)
    monkeypatch.setattr(egress, "READY_TIMEOUT", 0)
    calls = _capture(monkeypatch)
    with pytest.raises(RuntimeError, match="nicht erreichbar"):
        egress.start(egress.EgressConfig(conf_dir=tmp_path))
    assert calls[-1] == ["rm", "-f", egress.PROXY_CONTAINER]
    assert any(c[:1] == ["logs"] for c in calls)


def test_start_fails_when_container_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "_port_open", lambda port: True)
    monkeypatch.setattr(egress, "is_running", lambda: False)
    _capture(monkeypatch)
    with pytest.raises(RuntimeError, match="nicht erreichbar"):
        egress.start(egress.EgressConfig(conf_dir=tmp_path))


def test_wait_ready_polls_until_port_opens(monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(egress, "is_running", lambda: True)
    monkeypatch.setattr(egress, "_port_open", lambda port: next(answers))
    monkeypatch.setattr(egress, "READY_POLL_INTERVAL", 0)
    egress._wait_ready(egress.EgressConfig())


def test_default_image_is_pinned_by_digest():
    assert "@sha256:" in egress.DEFAULT_IMAGE


def _fake_docker(monkeypatch, responses):
    """`_docker` mit Skript: (returncode, stdout, stderr) je Aufruf, in Reihenfolge."""
    calls: list[list[str]] = []
    queue = list(responses)

    def run(*args):
        calls.append(list(args))
        rc, out, err = queue.pop(0)
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()

    monkeypatch.setattr(egress, "_docker", run)
    return calls


def test_existing_network_with_wrong_config_is_rejected(monkeypatch):
    """Ein vorhandenes `fabrik-sandbox` OHNE internal wäre stille Isolationslücke."""
    _fake_docker(
        monkeypatch, [(1, "", "network with name fabrik-egress already exists"), (0, "false 172.27.0.0/16 ", "")]
    )
    egress._ensure_network(egress.EGRESS_NET, egress.EGRESS_SUBNET, internal=False)  # passt → ok
    _fake_docker(monkeypatch, [(1, "", "already exists"), (0, "false 172.28.0.0/16 ", "")])
    with pytest.raises(RuntimeError, match="anderer Konfiguration"):
        egress._ensure_network(egress.SANDBOX_NET, egress.SANDBOX_SUBNET, internal=True)
    _fake_docker(monkeypatch, [(1, "", "already exists"), (0, "true 10.9.0.0/16 ", "")])
    with pytest.raises(RuntimeError, match="anderer Konfiguration"):
        egress._ensure_network(egress.SANDBOX_NET, egress.SANDBOX_SUBNET, internal=True)
    _fake_docker(monkeypatch, [(1, "", "already exists"), (0, "true 172.28.0.0/16 ", "")])
    egress._ensure_network(egress.SANDBOX_NET, egress.SANDBOX_SUBNET, internal=True)  # passt → ok


def test_docker_missing_or_hanging_raises_runtime_error(monkeypatch):
    def boom(exc):
        def run(*a, **k):
            raise exc

        return run

    monkeypatch.setattr(egress.subprocess, "run", boom(FileNotFoundError()))
    with pytest.raises(RuntimeError, match="docker nicht gefunden"):
        egress._docker("ps")
    monkeypatch.setattr(egress.subprocess, "run", boom(subprocess.TimeoutExpired("docker", 1)))
    with pytest.raises(RuntimeError, match="Timeout"):
        egress._docker("ps")


def test_is_running_reads_container_state(monkeypatch):
    calls = _capture(monkeypatch)
    assert egress.is_running() is True
    assert calls[0] == ["inspect", "-f", "{{.State.Running}}", egress.PROXY_CONTAINER]


# ---------------------------------------------------------------- Denied-Log
# Zeilen aus einem echten Lauf von tinyproxy 1.11.0 (Quell-IP = Bridge-Gateway bei published Port).
_LOG = [
    "CONNECT   Nov 07 22:51:04.029 [1]: Connect (file descriptor 5): 172.28.0.2",
    "CONNECT   Nov 07 22:51:04.029 [1]: Request (file descriptor 5): CONNECT example.org:443 HTTP/1.1",
    'NOTICE    Nov 07 22:51:04.030 [1]: Proxying refused on filtered domain "example.org"',
    "CONNECT   Nov 07 22:51:04.041 [1]: Connect (file descriptor 6): 172.28.0.3",
    "CONNECT   Nov 07 22:51:04.041 [1]: Request (file descriptor 6): CONNECT github.com:443 HTTP/1.1",
    'INFO      Nov 07 22:51:04.045 [1]: Established connection to host "github.com"',
    "CONNECT   Nov 07 22:51:04.050 [1]: Connect (file descriptor 5): 172.28.0.4",
    "CONNECT   Nov 07 22:51:04.051 [1]: Request (file descriptor 5): GET http://169.254.169.254/ HTTP/1.1",
    'NOTICE    Nov 07 22:51:04.052 [1]: Proxying refused on filtered domain "169.254.169.254"',
]


def test_parse_denied_reads_real_tinyproxy_format_and_ignores_allowed_traffic():
    ev = egress.parse_denied(_LOG)
    assert [(e.source, e.target) for e in ev] == [("172.28.0.2", "example.org"), ("172.28.0.4", "169.254.169.254")]
    assert ev[0].ts == "Nov 07 22:51:04.030"


def test_parse_denied_sanitizes_attacker_controlled_target():
    line = 'NOTICE    Nov 07 22:51:04.030 [1]: Proxying refused on filtered domain "a\x1b[31m\tb' + "x" * 400 + '"'
    (ev,) = egress.parse_denied([line])
    assert "\x1b" not in ev.target and "\t" not in ev.target and len(ev.target) == 253


def test_collect_denied_dedupes_within_batch_and_tracks_offset(tmp_path):
    log = tmp_path / "tinyproxy.log"
    state = tmp_path / "offset"
    log.write_text("\n".join(_LOG + _LOG) + "\n")
    assert len(egress.collect_denied(log, state)) == 2  # Duplikate im selben Abgleich fallen weg
    assert state.read_text().strip() == str(log.stat().st_size)
    assert egress.collect_denied(log, state) == []  # nichts Neues
    with log.open("a") as f:
        f.write("\n".join(_LOG[:3]) + "\n")
    ev = egress.collect_denied(log, state)
    assert [(e.source, e.target) for e in ev] == [("172.28.0.2", "example.org")]  # neuer Abgleich meldet erneut


def test_collect_denied_leaves_partial_last_line_for_next_poll(tmp_path):
    log = tmp_path / "tinyproxy.log"
    state = tmp_path / "offset"
    full = "\n".join(_LOG[:3]) + "\n"
    log.write_text(full + _LOG[3][:20])  # Proxy schreibt gerade
    egress.collect_denied(log, state)
    assert state.read_text() == str(len(full.encode()))
    with log.open("a") as f:
        f.write(_LOG[3][20:] + "\n" + "\n".join(_LOG[4:]) + "\n")
    assert [e.target for e in egress.collect_denied(log, state)] == ["169.254.169.254"]


def test_collect_denied_without_log_is_empty(tmp_path):
    assert egress.collect_denied(tmp_path / "fehlt.log", tmp_path / "offset") == []


def test_collect_denied_unreadable_log_is_an_error_not_silence(tmp_path):
    """Ein nicht lesbares Log (z. B. root:0600) darf die Auswertung nicht still totlegen."""
    log = tmp_path / "tinyproxy.log"
    log.write_text("x\n")
    log.chmod(0)
    if os.access(log, os.R_OK):  # root liest trotzdem
        pytest.skip("läuft als root")
    with pytest.raises(PermissionError):
        egress.collect_denied(log, tmp_path / "offset")


def test_collect_denied_resets_offset_on_log_rotation(tmp_path):
    """Log rotiert/geschnitten (neue Datei ist kürzer) → Offset auf 0 zurück,
    neue Entries werden NICHT überlesen."""
    log = tmp_path / "tinyproxy.log"
    state = tmp_path / "offset"
    log.write_text("\n".join(_LOG) + "\n")
    egress.collect_denied(log, state)  # Offset = Fullsize
    log.write_text("\n".join(_LOG[6:]) + "\n")
    ev = egress.collect_denied(log, state)
    assert [e.target for e in ev] == ["169.254.169.254"]
    assert state.read_text().strip() == str(log.stat().st_size)


def test_proxy_user_is_container_root_under_rootless_docker(monkeypatch):
    # rootless: Container-root = Host-User (User-Namespace); Host-uid im Container wäre eine subuid
    # ohne Zugriff auf die 0600-Config.
    assert egress.proxy_user(rootless=True) == "0:0"
    assert egress.proxy_user(rootless=False) == f"{os.getuid()}:{os.getgid()}"
    monkeypatch.setattr(egress, "_docker_rootless", lambda: True)
    assert egress.proxy_user() == "0:0"


def test_docker_rootless_detection(monkeypatch):
    class P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: P(0, '["name=seccomp","name=rootless"]'))
    assert egress._docker_rootless() is True
    monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: P(0, '["name=seccomp"]'))
    assert egress._docker_rootless() is False
    monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: P(1, "rootless"))
    assert egress._docker_rootless() is False

    def missing(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(egress.subprocess, "run", missing)
    assert egress._docker_rootless() is False
