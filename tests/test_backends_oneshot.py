"""OneShotCliBackend: Pipe-Mechanik gegen einen ECHTEN Subprozess (kein Fake)."""

import sys
import time

from fabrik.backends import OneShotCliBackend

# Deutlich größer als der Linux-Pipe-Puffer (64 KiB).
_BIG_OUTPUT_BYTES = 1_000_000
_MARKER = "END-OF-STREAM"
_POLL_DEADLINE_S = 20.0


class _EchoBackend(OneShotCliBackend):
    """Kind schreibt viel stdout + stderr und beendet sich dann."""

    name = "echo"

    def _cli(self) -> str:
        return sys.executable

    def _build_cmd(self) -> list[str]:
        script = (
            "import sys; sys.stdin.read();"
            f"sys.stderr.write('e' * {_BIG_OUTPUT_BYTES});"
            f"sys.stdout.write('o' * {_BIG_OUTPUT_BYTES} + '{_MARKER}')"
        )
        return [sys.executable, "-c", script]

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        return {"status": "done" if returncode == 0 else "failed", "text": out, "err": err}


def test_large_output_does_not_block_child_and_is_complete(tmp_path):
    """Regression (issue #153): stdout wurde erst nach Prozessende gelesen →
    Pipe voll, CLI blockiert beim Schreiben (copilot verwirft dann beim
    Shutdown den Rest inkl. `result`-Event). Der Drain-Thread muss das
    verhindern: Kind terminiert, Output ist vollständig."""
    be = _EchoBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    deadline = time.monotonic() + _POLL_DEADLINE_S
    r = be.poll(sid, str(tmp_path))
    while r["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        r = be.poll(sid, str(tmp_path))
    assert r["status"] == "done"
    assert r["text"].endswith(_MARKER)
    assert len(r["text"]) == _BIG_OUTPUT_BYTES + len(_MARKER)
    assert len(r["err"]) == _BIG_OUTPUT_BYTES


class _EnvBackend(_EchoBackend):
    """Kind gibt zwei Env-Variablen aus (prüft extra_env + Basis-Env)."""

    def _build_cmd(self) -> list[str]:
        script = (
            "import os, sys; sys.stdin.read(); print(os.environ.get('GH_TOKEN'), os.environ.get('PATH') is not None)"
        )
        return [sys.executable, "-c", script]


def _final(be, tmp_path):
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    deadline = time.monotonic() + _POLL_DEADLINE_S
    r = be.poll(sid, str(tmp_path))
    while r["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        r = be.poll(sid, str(tmp_path))
    return r


def test_extra_env_overlays_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "personal")
    be = _EnvBackend()
    be.extra_env = {"GH_TOKEN": "app-token"}
    assert _final(be, tmp_path)["text"].split() == ["app-token", "True"]


def test_extra_env_overlays_adapter_env(tmp_path, monkeypatch):
    """ADR 0050: `_env()` liefert ZUSÄTZE (kein `os.environ`-Klon) — der Subprozess
    sieht Allowlist + Adapter-Zusatz, aber KEINE Host-Secrets."""
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")

    class _LeakProbe(_EchoBackend):
        def _build_cmd(self) -> list[str]:
            script = (
                "import os, sys; sys.stdin.read(); print(os.environ.get('GH_TOKEN'), os.environ.get('GITHUB_TOKEN'))"
            )
            return [sys.executable, "-c", script]

    class _AdapterEnv(_LeakProbe):
        def _env(self) -> dict:
            return {"GH_TOKEN": "adapter"}

    be = _AdapterEnv()
    assert _final(be, tmp_path)["text"].split() == ["adapter", "None"]
    be = _AdapterEnv()
    be.extra_env = {"GH_TOKEN": "app-token"}
    assert _final(be, tmp_path)["text"].split() == ["app-token", "None"]


def test_base_env_is_scrubbed_for_subprocess(tmp_path, monkeypatch):
    """ADR 0050: Der Subprozess sieht nur die Allowlist — Secrets aus `os.environ`
    (z. B. GITHUB_TOKEN) landen NICHT in der Agent-Umgebung."""
    monkeypatch.setenv("GITHUB_TOKEN", "leak-check")
    monkeypatch.delenv("FABRIK_AGENT_ENV_EXTRA", raising=False)
    be = _EnvBackend()
    assert _final(be, tmp_path)["text"].split() == ["None", "True"]


def _git_repo(path) -> None:
    import subprocess

    for cmd in (
        ["init", "-q"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
    ):
        subprocess.run(["git", *cmd], cwd=path, check=True, capture_output=True)


def test_sandbox_enabled_wraps_cmd_in_docker_run(tmp_path, monkeypatch):
    """ADR 0054: Mit `FABRIK_SANDBOX=1` wird der Agent-Befehl in `docker run`
    gehüllt (Hardening-Flags, git-Austausch statt Worktree-Mount, gescrubte Env als -e-Flags);
    poll() übernimmt das Ergebnis und räumt den Austausch auf."""
    import io

    from fabrik import sandbox

    _git_repo(tmp_path)
    monkeypatch.setattr(sandbox, "sandbox_enabled", lambda env=None: True)
    monkeypatch.setattr(
        sandbox.SandboxConfig,
        "from_env",
        classmethod(lambda cls, env=None: sandbox.SandboxConfig(image="test/img:1", user="1000:1000")),
    )
    captured: dict = {}

    class _FakeProc:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.stdin = io.BytesIO()

        def poll(self):
            return 0

        def communicate(self):
            return (b"ok", b"")

    import subprocess
    import types

    from fabrik.backends import _oneshot

    # Nur das Backend bekommt den Fake — die git-Aufrufe der Sandbox laufen echt.
    fake_subprocess = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess) if k.isupper()})
    fake_subprocess.Popen = _FakeProc
    monkeypatch.setattr(_oneshot, "subprocess", fake_subprocess)
    be = _EnvBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    exchange = be._sessions[sid]["sandbox"]
    cmd = captured["cmd"]
    assert cmd[0] == "docker" and cmd[1] == "run"
    assert sandbox.container_name(sid) in cmd
    assert not any(str(tmp_path) + ":" in arg for arg in cmd)  # kein Worktree-Mount
    assert f"{exchange.in_bundle}:/in/base.bundle:ro" in cmd and exchange.in_bundle.is_file()
    assert "test/img:1" in cmd
    assert cmd[-1].startswith("import os, sys")  # Agent-Command überlebt die Hülle
    # gescrubte Env: kein GITHUB_TOKEN; Container-Defaults gewinnen (HOME=/home/agent,
    # Host-HOME wird gestrippt) — s. ADR 0054
    assert "GITHUB_TOKEN" not in " ".join(cmd)
    assert "HOME=/home/agent" in cmd
    # Prozessende: kein Ergebnis-Bundle → nichts übernommen, Austausch aufgeräumt.
    assert be.poll(sid, str(tmp_path))["status"] == "done"
    assert not exchange.tmpdir.exists() and "sandbox" not in be._sessions[sid]


def test_sandbox_result_rejection_fails_the_step(tmp_path, monkeypatch):
    from fabrik import sandbox

    be = _EnvBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    be._sessions[sid]["sandbox"] = object()
    monkeypatch.setattr(sandbox, "finish_exchange", lambda ex, wd: "Ergebnis abgewiesen: Pfad '.git/x' berührt .git")
    deadline = time.monotonic() + 10.0
    r = be.poll(sid, str(tmp_path))
    while r["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        r = be.poll(sid, str(tmp_path))
    assert r["status"] == "failed" and "berührt .git" in r["text"]


def test_cancel_discards_sandbox_exchange(tmp_path, monkeypatch):
    """Abgebrochener Run: Ergebnis wird nicht übernommen, Bundle-Verzeichnis und Refs verschwinden."""
    from fabrik import sandbox

    discarded: list = []
    monkeypatch.setattr(sandbox, "discard_exchange", lambda ex, wd: discarded.append(ex))
    be = _EnvBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    marker = object()
    be._sessions[sid]["sandbox"] = marker
    be.cancel(sid, str(tmp_path))
    assert discarded == [marker] and "sandbox" not in be._sessions[sid]


def test_cancel_stops_sandbox_container(monkeypatch, tmp_path):
    """cancel() muss den Agent-Container mit `docker rm -f` beenden — ein
    killpg auf die docker-CLI-Prozessgruppe beendet das laufende Image NICHT
    (der Agent würde nur vom Prozess-Handle entkoppelt, läuft aber weiter).
    Maßgeblich ist, ob DIESER Run einen Austausch hat — nicht das aktuelle Env-Flag."""
    from fabrik import sandbox

    class _SlowBackend(OneShotCliBackend):
        name = "slow"

        def _cli(self) -> str:
            return sys.executable

        def _build_cmd(self) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        def _parse(self, out: str, err: str, returncode: int) -> dict:
            return {"status": "done", "text": out}

    be = _SlowBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    time.sleep(0.3)  # Prozess ist alive
    stopped: list[str] = []
    discarded: list = []
    monkeypatch.setattr(sandbox, "stop_container", lambda session_id: stopped.append(session_id))
    monkeypatch.setattr(sandbox, "discard_exchange", lambda ex, wd: discarded.append(ex))
    marker = object()
    be._sessions[sid]["sandbox"] = marker  # Run lief in der Sandbox (Env-Flag ist hier aus)
    be.cancel(sid, str(tmp_path))
    assert stopped == [sid] and discarded == [marker]
    assert "sandbox" not in be._sessions[sid]  # zuerst entfernt: poll() übernimmt nichts mehr
    # SIGKILL-Wirkung: bis zur Reapung ein Tick
    deadline = time.monotonic() + 5.0
    r = be.poll(sid, str(tmp_path))
    while r["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        r = be.poll(sid, str(tmp_path))
    assert r["status"] != "running"


def test_cancel_without_sandbox_does_not_touch_docker(monkeypatch, tmp_path):
    from fabrik import sandbox

    monkeypatch.setattr(sandbox, "stop_container", lambda sid: (_ for _ in ()).throw(AssertionError("docker")))
    be = _EnvBackend()
    sid = be.create_task(str(tmp_path), "t")
    be.send_prompt(sid, "prompt", str(tmp_path))
    be.cancel(sid, str(tmp_path))


class _CrashBackend(_EchoBackend):
    """Kind schreibt einen langen Crash-Banner nach stderr und stirbt an SIGSEGV."""

    def _build_cmd(self) -> list[str]:
        script = (
            "import os, signal, sys; sys.stdin.read();"
            "sys.stderr.write('=' * 60 + '\\n' + 'noise line\\n' * 2000 + 'panic: Segmentation fault at address 0x0\\n');"
            "sys.stderr.flush(); os.kill(os.getpid(), signal.SIGSEGV)"
        )
        return [sys.executable, "-c", script]

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        # wie die echten Adapter: nur der ANFANG von stderr im Text
        return {"status": "done" if returncode == 0 else "failed", "text": err[:300]}


def test_failed_result_keeps_exit_code_and_stderr_tail(tmp_path):
    """Ein Absturz der CLI: `_parse` behält nur den Banner-Anfang, die Ursache steht am Ende von
    stderr — `poll` liefert Exit-Code und dieses Ende mit (zeilenweise abgeschnitten, begrenzt)."""
    from fabrik.backends._oneshot import STDERR_TAIL_CHARS

    r = _final(_CrashBackend(), tmp_path)
    assert r["status"] == "failed"
    assert r["exit_code"] == -11
    assert "Segmentation fault at address" not in r["text"]
    assert r["stderr_tail"].endswith("panic: Segmentation fault at address 0x0")
    assert len(r["stderr_tail"]) <= STDERR_TAIL_CHARS
    assert r["stderr_tail"].startswith("noise line")


def test_done_result_has_no_exit_fields(tmp_path):
    r = _final(_EnvBackend(), tmp_path)
    assert r["status"] == "done"
    assert "exit_code" not in r
    assert "stderr_tail" not in r


def test_tail_short_text_and_no_newline():
    from fabrik.backends._oneshot import _tail

    assert _tail("kurz\n") == "kurz"
    assert _tail("x" * 50, limit=10) == "x" * 10
    assert _tail("a\nbb\nccc", limit=6) == "ccc"
