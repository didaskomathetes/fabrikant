"""Per-Run-Agent-Sandbox (ADR 0054): Env, docker-run-Rendering, git-Austausch, Container-Stop."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fabrik import egress, sandbox
from fabrik.config import HARNESS_DIR, HARNESS_LOCAL_FILE, HARNESS_PRIVATE_DIR

_REAL_STOP_CONTAINER = sandbox.stop_container  # vor dem autouse-Mock
_EX = sandbox.Exchange(tmpdir=Path("/x/ex"), base="abc", ref="refs/fabrik/sandbox/sid123")


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch):
    """`finish_exchange` stoppt den Container — in Tests ohne echtes docker; Aufrufe werden gesammelt."""
    stopped: list[str] = []
    monkeypatch.setattr(sandbox, "stop_container", stopped.append)
    return stopped


def test_sandbox_enabled_parsing(monkeypatch):
    monkeypatch.delenv(sandbox.ENABLE_VAR, raising=False)
    assert sandbox.sandbox_enabled() is False
    for val in ("1", "true", "YES", " on "):
        monkeypatch.setenv(sandbox.ENABLE_VAR, val)
        assert sandbox.sandbox_enabled() is True
    monkeypatch.setenv(sandbox.ENABLE_VAR, "0")
    assert sandbox.sandbox_enabled() is False


def test_from_env_defaults_and_overrides(monkeypatch):
    for var in (
        sandbox.IMAGE_VAR,
        sandbox.MEMORY_VAR,
        sandbox.CPUS_VAR,
        sandbox.USER_VAR,
        sandbox.PIDS_LIMIT_VAR,
        sandbox.NETWORK_VAR,
        sandbox.PROXY_URL_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = sandbox.SandboxConfig.from_env()
    assert cfg.image == sandbox.DEFAULT_IMAGE
    assert cfg.memory == "4g" and cfg.cpus == "2" and cfg.pids_limit == 512
    assert cfg.network == "fabrik-sandbox"
    assert cfg.proxy_url == egress.agent_proxy_url()  # Default: der von `fabrik egress start` gestartete Proxy

    env = {
        sandbox.IMAGE_VAR: "my/img:1",
        sandbox.MEMORY_VAR: "8g",
        sandbox.CPUS_VAR: "4",
        sandbox.USER_VAR: "1000:1000",
        sandbox.PIDS_LIMIT_VAR: "128",
        sandbox.PROXY_URL_VAR: "http://fabrik-egress-proxy:8888",
    }
    cfg = sandbox.SandboxConfig.from_env(env)
    assert cfg.image == "my/img:1" and cfg.memory == "8g" and cfg.cpus == "4"
    assert cfg.user == "1000:1000" and cfg.pids_limit == 128
    assert cfg.proxy_url == "http://fabrik-egress-proxy:8888"


def test_from_env_invalid_pids_limit_raises_cleanly(monkeypatch):
    """Nicht-numerische PIDs-Limit → saubere RuntimeError mit Var-Namen
    (gleiche Fehlerklasse wie egress' Port-Validation)."""
    with pytest.raises(RuntimeError, match=sandbox.PIDS_LIMIT_VAR):
        sandbox.SandboxConfig.from_env({sandbox.PIDS_LIMIT_VAR: "fünfhundert"})


def test_container_name_is_docker_safe():
    name = sandbox.container_name("copilot-abc123XYZ/def")
    assert name.startswith("fabrik-agent-")
    assert "/" not in name and len(name) <= 63


def _cfg(proxy_url: str = "http://proxy:8888") -> sandbox.SandboxConfig:
    return sandbox.SandboxConfig(image="test/img:1", user="1000:1000", proxy_url=proxy_url)


def _effective_env(cmd: list[str], host_env: dict[str, str] | None = None) -> dict[str, str]:
    """Die im Container effektive Env: `-e K=V` (fester Wert) bzw. `-e K` (Wert aus der Env des
    docker-CLI-Prozesses = `host_env`); letztes Flag gewinnt (Docker-Semantik bei doppeltem -e)."""
    env: dict[str, str] = {}
    for i, arg in enumerate(cmd):
        if arg == "-e" and i + 1 < len(cmd):
            k, sep, v = cmd[i + 1].partition("=")
            env[k] = v if sep else (host_env or {})[k]
    return env


def test_render_docker_cmd_has_hardening_and_mounts():
    host_env = {"PATH": "/usr/bin", "TZ": "UTC", "GH_TOKEN": "ghs_SECRET"}
    cmd = sandbox.render_docker_cmd(_cfg(), "sid123", ["claude", "--print"], _EX, host_env)
    assert cmd[:2] == ["docker", "run"]
    assert "--rm" in cmd and "--init" in cmd and sandbox.container_name("sid123") in cmd
    # Prompt kommt via stdin: ohne -i hängt docker stdin nicht an (kein -t: kein TTY)
    assert "-i" in cmd and "-t" not in cmd
    assert cmd[cmd.index("--network") + 1] == "fabrik-sandbox"
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert cmd[cmd.index("--memory") + 1] == "4g"
    assert cmd[cmd.index("--cpus") + 1] == "2"
    assert cmd[cmd.index("--pids-limit") + 1] == "512"
    assert cmd[cmd.index("--memory-swap") + 1] == "4g"  # = memory: kein Swap
    assert cmd[cmd.index("--pull") + 1] == "never"
    assert "--read-only" in cmd
    assert cmd[cmd.index("-u") + 1] == "1000:1000"
    # Kein Worktree-Mount: Bundle read-only hinein, nur das Austauschverzeichnis schreibbar.
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
    assert mounts == ["/x/ex/base.bundle:/in/base.bundle:ro", "/x/ex/out:/out"]
    # Arbeitsklon im tmpfs (exec für Build-Tools, Größe begrenzt), read-only root fs.
    tmpfs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--tmpfs"]
    assert tmpfs == [
        "/tmp:size=256m",
        "/home/agent:rw,mode=1777,size=256m",
        "/work:rw,exec,mode=1777,size=2g",
    ]
    assert cmd[cmd.index("-w") + 1] == "/work"
    # Env als explizite -e-Flags (kein --env-file, kein os.environ).
    env = _effective_env(cmd, host_env)
    assert env["TZ"] == "UTC" and env["GH_TOKEN"] == "ghs_SECRET"
    # Secrets nie im argv (`ps`, `docker inspect`): nur `-e GH_TOKEN`, der Wert kommt aus der CLI-Env.
    assert "ghs_SECRET" not in " ".join(cmd) and "GH_TOKEN" in cmd
    assert "PATH" not in env  # Host-PATH würde die Image-Binaries verdecken
    assert env["HOME"] == "/home/agent"
    # Proxy-Env nur wenn konfiguriert.
    assert env["HTTP_PROXY"] == "http://proxy:8888" and env["HTTPS_PROXY"] == "http://proxy:8888"
    assert env["http_proxy"] == env["https_proxy"] == "http://proxy:8888"
    # Loopback am Proxy vorbei (lokale Test-Server), in beiden Schreibweisen.
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
    # Entrypoint-Skript mit Basis-Ref, dann der Agent-Befehl.
    assert cmd[cmd.index("--entrypoint") + 1] == "sh"
    i = cmd.index("test/img:1")
    assert cmd[i + 1 :] == ["-c", sandbox.entrypoint_script(), "fabrik-sandbox", _EX.ref, "claude", "--print"]


def test_realistic_host_env_is_stripped_and_overridden():
    """Reale ADR-0050-Env (HOME=/root, XDG_*, TMPDIR): im Container müssen die
    Host-Pfade weg sein — sonst zeigt HOME auf die READ-ONLY-Root-FS und jeder
    Run bricht mit Permission denied."""
    host_env = {
        "HOME": "/root",
        "PATH": "/root/.local/bin:/usr/local/bin:/usr/bin",
        "XDG_CONFIG_HOME": "/root/.config",
        "XDG_CACHE_HOME": "/root/.cache",
        "UV_CACHE_DIR": "/root/.cache/uv",
        "TMPDIR": "/tmp/fabrik-host",
        "TZ": "UTC",
        "USER": "dom",
    }
    cmd = sandbox.render_docker_cmd(_cfg(proxy_url=""), "s", ["codex"], _EX, host_env)
    env = _effective_env(cmd, host_env)
    assert env["HOME"] == "/home/agent"
    assert env["XDG_CONFIG_HOME"] == "/home/agent/.config"
    assert env["XDG_CACHE_HOME"] == "/home/agent/.cache"
    assert env["TMPDIR"] == "/tmp"
    assert env["UV_CACHE_DIR"] == "/work/.uv-cache"  # neben dem Klon /work/repo, nicht darin
    assert env["TZ"] == "UTC" and env["USER"] == "dom"
    assert "PATH" not in env
    # Kein Host-Pfad darf in der effektiven Env landen.
    assert not any(str(v).startswith("/root") for v in env.values())


def test_render_docker_cmd_without_proxy_has_no_proxy_env():
    cmd = sandbox.render_docker_cmd(_cfg(proxy_url=""), "s", ["codex"], _EX, {"PATH": "/usr/bin"})
    assert "HTTP_PROXY" not in " ".join(cmd)
    assert "NO_PROXY" not in " ".join(cmd)


def test_stop_container_calls_docker_rm_f(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox.subprocess, "run", lambda *a, **k: calls.append(list(a[0])) or type("R", (), {"returncode": 0})()
    )
    _REAL_STOP_CONTAINER("sid/1")
    assert calls == [["docker", "rm", "-f", sandbox.container_name("sid/1")]]


def test_stop_container_survives_hanging_docker(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k["timeout"])  # stop_container setzt immer ein Timeout

    monkeypatch.setattr(sandbox.subprocess, "run", hang)
    _REAL_STOP_CONTAINER("sid")  # kein Fehler: cancel() darf nicht mit hängen


def _fake_run(calls, ps_out="a\nb\n", rc=0):
    def run(cmd, **k):
        calls.append(list(cmd))
        return type("R", (), {"returncode": rc, "stdout": ps_out if cmd[1] == "ps" else ""})()

    return run


def test_sweep_removes_labelled_containers(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_run(calls))
    assert sandbox.sweep_stale_containers() == 2
    assert calls[0] == ["docker", "ps", "-aq", "--filter", f"label={sandbox.AGENT_LABEL}"]
    assert calls[1] == ["docker", "rm", "-f", "a", "b"]


def test_sweep_nothing_to_remove(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_run(calls, ps_out=""))
    assert sandbox.sweep_stale_containers() == 0
    assert len(calls) == 1


def test_sweep_ps_failure_removes_nothing(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox.subprocess, "run", _fake_run(calls, rc=1))
    assert sandbox.sweep_stale_containers() == 0
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 1)])
def test_sweep_is_best_effort(monkeypatch, caplog, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox.sweep_stale_containers() == 0
    assert "Sandbox-Sweep übersprungen" in caplog.text


def test_render_docker_cmd_labels_container():
    cmd = sandbox.render_docker_cmd(_cfg(), "s", ["codex"], _EX, {"PATH": "/usr/bin"})
    assert cmd[cmd.index("--label") + 1] == sandbox.AGENT_LABEL


# --- git-Austausch (ohne Docker: das Entrypoint-Skript läuft lokal mit Host-Pfaden) -----------------


def _sh(cwd: Path, *cmd: str) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Worktree mit einem Commit: a.txt, gone.txt, bin.dat (binär)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _sh(wt, "git", "init", "-q", "-b", "main")
    _sh(wt, "git", "config", "user.name", "t")
    _sh(wt, "git", "config", "user.email", "t@t")
    (wt / "a.txt").write_text("eins\n")
    (wt / "gone.txt").write_text("weg\n")
    (wt / "bin.dat").write_bytes(bytes(range(256)))
    (wt / ".gitignore").write_text("ignored.log\n")
    _sh(wt, "git", "add", "-A")
    _sh(wt, "git", "commit", "-q", "-m", "init")
    return wt


def _run_container(ex: sandbox.Exchange, tmp_path: Path, agent: str) -> int:
    """Das Entrypoint-Skript wie im Container ausführen — `agent` ist ein sh-Snippet im Klon."""
    script = sandbox.entrypoint_script(
        work_dir=str(tmp_path / "container-work"), in_bundle=str(ex.in_bundle), out_dir=str(ex.out_dir)
    )
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    p = subprocess.run(["sh", "-c", script, "fabrik-sandbox", ex.ref, "sh", "-c", agent], env=env, capture_output=True)
    assert b"fabrik-base" not in p.stdout  # git-Ausgaben gehen nach stderr, stdout gehört dem Agenten
    return p.returncode


def _state(wt: Path) -> tuple[str, str, str]:
    return _sh(wt, "git", "rev-parse", "HEAD"), _sh(wt, "git", "diff", "--cached"), _sh(wt, "git", "for-each-ref")


def test_roundtrip_applies_agent_changes_as_uncommitted(repo, tmp_path):
    head, staged, refs = _state(repo)
    ex = sandbox.prepare_exchange("sid-1", str(repo))
    assert ex.base == head.strip()  # sauberer Worktree → HEAD ist die Basis
    agent = (
        "printf 'zwei\\n' >> a.txt && rm gone.txt && echo neu > new.txt && echo x > ignored.log && printf 'agent-out'"
    )
    assert _run_container(ex, tmp_path, agent) == 0
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert (repo / "a.txt").read_text() == "eins\nzwei\n"
    assert not (repo / "gone.txt").exists()
    assert (repo / "new.txt").read_text() == "neu\n"
    assert not (repo / "ignored.log").exists()  # ignorierte Dateien bleiben im Container
    assert (repo / "bin.dat").read_bytes() == bytes(range(256))
    # HEAD, Index und Refs unverändert; Austauschverzeichnis weg.
    assert _state(repo) == (head, staged, refs)
    assert not ex.tmpdir.exists()


def test_roundtrip_carries_uncommitted_host_state_without_touching_index(repo, tmp_path):
    (repo / "a.txt").write_text("host-geaendert\n")
    (repo / "untracked.txt").write_text("vom vorigen Step\n")
    head, staged, _ = _state(repo)
    ex = sandbox.prepare_exchange("sid-2", str(repo))
    assert ex.base != head.strip()  # temporärer Commit mit dem Host-Stand
    assert _sh(repo, "git", "status", "--porcelain") == " M a.txt\n?? untracked.txt\n"  # Index unberührt
    agent = "test \"$(cat untracked.txt)\" = 'vom vorigen Step' && echo agent >> a.txt"
    assert _run_container(ex, tmp_path, agent) == 0
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert (repo / "a.txt").read_text() == "host-geaendert\nagent\n"
    assert (repo / "untracked.txt").read_text() == "vom vorigen Step\n"
    assert _state(repo)[:2] == (head, staged)


def test_agent_commits_are_collapsed_into_worktree_changes(repo, tmp_path):
    head = _state(repo)[0]
    ex = sandbox.prepare_exchange("sid-3", str(repo))
    agent = "echo c >> a.txt && git -c user.name=a -c user.email=a@a commit -qam agent && echo d > d.txt"
    assert _run_container(ex, tmp_path, agent) == 0
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert _state(repo)[0] == head
    assert (repo / "a.txt").read_text() == "eins\nc\n" and (repo / "d.txt").exists()


def test_failed_agent_keeps_exit_code_and_changes_are_applied(repo, tmp_path):
    ex = sandbox.prepare_exchange("sid-4", str(repo))
    assert _run_container(ex, tmp_path, "echo halb >> a.txt; exit 3") == 3
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert (repo / "a.txt").read_text() == "eins\nhalb\n"


def _harness(repo: Path) -> Path:
    """Austauschverzeichnis wie bei Relay: lokal ausgeschlossen, mit Arbeitskopie und Local-Datei."""
    h = repo / HARNESS_DIR
    (repo / ".git" / "info" / "exclude").write_text(f"/{HARNESS_DIR}/\n")
    (h / "audit" / "work").mkdir(parents=True)
    (h / HARNESS_LOCAL_FILE).write_text("audit/work/\n")
    (h / "BRIEF.md").write_text("brief\n")
    (h / "audit" / "work" / "clone.txt").write_text("host scratch\n")
    return h


def test_harness_dir_travels_both_ways_but_local_paths_stay(repo, tmp_path):
    h = _harness(repo)
    head, staged, _ = _state(repo)
    assert _sh(repo, "git", "status", "--porcelain") == ""  # nur Lokal-Ignoriertes
    ex = sandbox.prepare_exchange("sid-h", str(repo))
    assert ex.base != head.strip()  # trotz sauberem Status: das Austauschverzeichnis reist mit
    agent = (
        f'test "$(cat {HARNESS_DIR}/BRIEF.md)" = brief && test ! -e {HARNESS_DIR}/audit/work'
        f" && mkdir -p {HARNESS_DIR}/audit && echo report > {HARNESS_DIR}/audit/AUDIT.md"
        f" && echo evidence > {HARNESS_DIR}/audit/ignored.log"  # .gitignore des Repos gilt hier nicht
        f" && mkdir -p {HARNESS_DIR}/audit/work && echo c > {HARNESS_DIR}/audit/work/scratch"
    )
    assert _run_container(ex, tmp_path, agent) == 0
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert (h / "audit" / "AUDIT.md").read_text() == "report\n"
    assert (h / "audit" / "ignored.log").read_text() == "evidence\n"
    assert not (h / "audit" / "work" / "scratch").exists()
    assert (h / "audit" / "work" / "clone.txt").read_text() == "host scratch\n"
    assert _state(repo)[:2] == (head, staged)
    assert _sh(repo, "git", "status", "--porcelain") == ""


def test_embedded_repo_in_harness_dir_is_not_carried(repo):
    h = _harness(repo)
    nested = h / "nest"
    nested.mkdir()
    _sh(nested, "git", "init", "-q")
    (nested / "z.txt").write_text("z\n")
    ex = sandbox.prepare_exchange("sid-n", str(repo))
    carried = _sh(repo, "git", "ls-tree", "-r", "--name-only", ex.base, "--", HARNESS_DIR).split()
    assert carried == [f"{HARNESS_DIR}/{HARNESS_LOCAL_FILE}", f"{HARNESS_DIR}/BRIEF.md"]
    sandbox.discard_exchange(ex, str(repo))


def test_entrypoint_without_harness_dir_still_returns_changes(repo, tmp_path):
    # Kein Austauschverzeichnis (Nicht-Relay-Step): Staging der Carry-Pfade ist ein No-Op.
    ex = sandbox.prepare_exchange("sid-e", str(repo))
    assert _run_container(ex, tmp_path, "echo neu > new.txt") == 0
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert (repo / "new.txt").read_text() == "neu\n"
    assert not (repo / HARNESS_DIR).exists()


def test_no_result_bundle_changes_nothing(repo):
    ex = sandbox.prepare_exchange("sid-5", str(repo))
    assert sandbox.apply_result(ex, str(repo)) is False
    sandbox.discard_exchange(ex, str(repo))
    assert _sh(repo, "git", "status", "--porcelain") == ""
    assert _sh(repo, "git", "for-each-ref", "refs/fabrik") == ""


def _forge_result_head(ex: sandbox.Exchange, repo: Path, tree_entries: str, parent: str) -> None:
    """Ergebnis-Bundle eines manipulierten Containers: Commit mit beliebigem Tree (mktree),
    als `HEAD` gebündelt wie im Entrypoint — gebaut in einem Wegwerf-Klon."""
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input=tree_entries, check=True, capture_output=True, text=True
    ).stdout.strip()
    commit = _sh(repo, "git", "commit-tree", tree, *(["-p", parent] if parent else []), "-m", "evil").strip()
    forge = ex.tmpdir / "forge"
    _sh(ex.tmpdir, "git", "init", "-q", str(forge))
    _sh(forge, "git", "fetch", "-q", str(repo), commit)
    _sh(forge, "git", "update-ref", "--no-deref", "HEAD", commit)  # kein Checkout: .GIT im Tree
    _sh(forge, "git", "bundle", "create", "-q", str(ex.result_bundle), "HEAD")


def _blob(repo: Path, text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=text, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_result_touching_dotgit_is_rejected(repo, tmp_path):
    ex = sandbox.prepare_exchange("sid-6", str(repo))
    hooks = subprocess.run(
        ["git", "mktree"], cwd=repo, input=f"100755 blob {_blob(repo, 'evil')}\tpost-checkout\n",
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    entries = f"040000 tree {hooks}\t.GIT\n100644 blob {_blob(repo, 'x')}\ta.txt\n"
    _forge_result_head(ex, repo, entries, ex.base)
    before = _sh(repo, "git", "status", "--porcelain")
    with pytest.raises(sandbox.SandboxError):
        sandbox.apply_result(ex, str(repo))
    assert _sh(repo, "git", "status", "--porcelain") == before
    assert not (repo / ".GIT").exists()
    sandbox.discard_exchange(ex, str(repo))


def test_result_touching_private_harness_dir_is_rejected(repo, tmp_path):
    ex = sandbox.prepare_exchange("sid-9", str(repo))
    checkin = _blob(repo, '{"status": "done"}\n')
    harness = subprocess.run(
        ["git", "mktree"], cwd=repo, input=f"100644 blob {checkin}\tcheckin.json\n",
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    entries = f"040000 tree {harness}\t{HARNESS_PRIVATE_DIR}\n100644 blob {_blob(repo, 'x')}\ta.txt\n"
    _forge_result_head(ex, repo, entries, ex.base)
    before = _sh(repo, "git", "status", "--porcelain")
    with pytest.raises(sandbox.SandboxError, match="Harness"):
        sandbox.apply_result(ex, str(repo))
    assert _sh(repo, "git", "status", "--porcelain") == before
    assert not (repo / HARNESS_PRIVATE_DIR).exists()
    sandbox.discard_exchange(ex, str(repo))


def test_result_not_descending_from_base_is_rejected(repo):
    ex = sandbox.prepare_exchange("sid-7", str(repo))
    _forge_result_head(ex, repo, f"100644 blob {_blob(repo, 'fremd')}\tfremd.txt\n", "")
    with pytest.raises(sandbox.SandboxError, match="Basis-Commit"):
        sandbox.apply_result(ex, str(repo))
    assert not (repo / "fremd.txt").exists()
    sandbox.discard_exchange(ex, str(repo))


def test_symlinked_result_bundle_is_rejected(repo, tmp_path):
    ex = sandbox.prepare_exchange("sid-8", str(repo))
    secret = tmp_path / "host-secret"
    secret.write_text("geheim")
    ex.result_bundle.symlink_to(secret)
    assert "keine reguläre Datei" in (sandbox.finish_exchange(ex, str(repo)) or "")
    assert not ex.tmpdir.exists()


@pytest.mark.parametrize("path", [".git/config", "sub/.GIT/hooks/x", ".Git"])
def test_reject_unsafe_paths(path):
    with pytest.raises(sandbox.SandboxError):
        sandbox._reject_unsafe_paths(["ok.txt", path])


def test_reject_unsafe_paths_allows_lookalikes():
    sandbox._reject_unsafe_paths(
        [
            ".gitignore",
            ".github/workflows/ci.yml",
            "a/.gitkeep",
            f"{HARNESS_PRIVATE_DIR}-evil/x",
            f"sub/{HARNESS_PRIVATE_DIR}",
        ]
    )


def test_effective_user_defaults(monkeypatch):
    """Explizit > host uid:gid > (als root) unprivilegierter User."""
    assert sandbox.SandboxConfig(user="7:8").effective_user == "7:8"
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 1234)
    monkeypatch.setattr(sandbox.os, "getgid", lambda: 4321)
    assert sandbox.SandboxConfig().effective_user == "1234:4321"
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 0)
    assert sandbox.SandboxConfig().effective_user == sandbox.UNPRIVILEGED_USER


def test_docker_cli_env_adds_daemon_vars_but_not_to_container():
    host = {"DOCKER_HOST": "unix:///run/user/1000/docker.sock", "XDG_RUNTIME_DIR": "/run/user/1000", "SECRET": "x"}
    cli_env = sandbox.docker_cli_env({"TZ": "UTC"}, host)
    assert cli_env == {"TZ": "UTC", "DOCKER_HOST": host["DOCKER_HOST"], "XDG_RUNTIME_DIR": "/run/user/1000"}
    # ... und landen nicht im Container: `render_docker_cmd` bekommt nur die gescrubte Env.
    cmd = sandbox.render_docker_cmd(_cfg(), "s", ["a"], _EX, {"TZ": "UTC"})
    assert "DOCKER_HOST" not in " ".join(cmd)


def test_prepare_exchange_requires_worktree_root(repo):
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(sandbox.SandboxError, match="Worktree-Wurzel"):
        sandbox.prepare_exchange("sid-sub", str(sub))
    assert _sh(repo, "git", "for-each-ref", "refs/fabrik") == ""


def test_finish_exchange_stops_container_before_reading(repo, _no_real_docker):
    ex = sandbox.prepare_exchange("sid-stop", str(repo))
    assert sandbox.finish_exchange(ex, str(repo)) is None
    assert _no_real_docker == ["sid-stop"]


def test_finish_exchange_reports_oserror_instead_of_raising(repo, monkeypatch):
    ex = sandbox.prepare_exchange("sid-os", str(repo))
    (ex.out_dir / sandbox.RESULT_BUNDLE_NAME).write_bytes(b"x")

    def boom(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(sandbox, "apply_result", boom)
    assert "nope" in (sandbox.finish_exchange(ex, str(repo)) or "")
    assert not ex.tmpdir.exists()


def test_result_with_symlink_is_rejected(repo):
    ex = sandbox.prepare_exchange("sid-link", str(repo))
    _forge_result_head(ex, repo, f"120000 blob {_blob(repo, '/root/.config/fabrik/fabrik.env')}\tlink\n", ex.base)
    with pytest.raises(sandbox.SandboxError, match="Symlink"):
        sandbox.apply_result(ex, str(repo))
    assert not (repo / "link").exists() and not (repo / "link").is_symlink()
    sandbox.discard_exchange(ex, str(repo))


def test_result_with_gitlink_is_rejected(repo):
    ex = sandbox.prepare_exchange("sid-sub2", str(repo))
    _forge_result_head(ex, repo, f"160000 commit {ex.base}\tsubmod\n", ex.base)
    with pytest.raises(sandbox.SandboxError, match="Submodul"):
        sandbox.apply_result(ex, str(repo))
    sandbox.discard_exchange(ex, str(repo))


def test_oversized_bundle_and_patch_are_rejected(repo, monkeypatch):
    ex = sandbox.prepare_exchange("sid-big", str(repo))
    _forge_result_head(ex, repo, f"100644 blob {_blob(repo, 'x' * 100)}\ta.txt\n", ex.base)
    monkeypatch.setattr(sandbox, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(sandbox.SandboxError, match="Bundle|bundle"):
        sandbox.apply_result(ex, str(repo))
    monkeypatch.setattr(sandbox, "MAX_BUNDLE_BYTES", 10**9)
    monkeypatch.setattr(sandbox, "MAX_PATCH_BYTES", 10)
    with pytest.raises(sandbox.SandboxError, match="Patch zu groß"):
        sandbox.apply_result(ex, str(repo))
    assert (repo / "a.txt").read_text() == "eins\n"  # nichts angewendet
    sandbox.discard_exchange(ex, str(repo))


def test_parse_raw_diff():
    raw = ":100644 120000 aaa bbb M\0a.txt\0:000000 100755 000 ccc A\0sub/run.sh\0"
    assert sandbox._parse_raw_diff(raw) == [("120000", "a.txt"), ("100755", "sub/run.sh")]
