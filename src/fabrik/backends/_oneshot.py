"""OneShotCliBackend: gemeinsame One-Shot-CLI-Mechanik für CLI-Adapter.

Bündelt das, was `claude_code`, `codex` und `copilot` identisch brauchen:
Session-Registry, `Popen` (Prompt via stdin, eigene Prozessgruppe), den
`poll`-Liveness/Staleness-Loop und `killpg`-Cancel. Adapter (Subklassen)
definieren nur noch die Unterschiede:

  - `name`            Class-Attribut (Backend-Identifikator)
  - `_cli()`          Binary-Name für `health()`
  - `_build_cmd()`    argv OHNE Prompt (Prompt geht via stdin)
  - `_parse(out, err, rc)` → {status, text, tokens, cost, tools}

Ein `failed`-Ergebnis trägt zusätzlich `exit_code` und `stderr_tail` (die letzten
`STDERR_TAIL_CHARS` Zeichen von stderr): `_parse` kürzt seinen `text` auf den Anfang, bei
einem Absturz (Crash-Banner der CLI, Python-Traceback) steht die Ursache aber am Ende.

Optionale Hooks: `_env()` (zusätzliches Prozess-Env), `_sid_prefix()`,
`_stale_after()` (None = Staleness-Check deaktiviert, z. B. claude_code).
`extra_env` (vom Agent-Step gesetzt, z. B. die GitHub-Identity) überlagert
das Prozess-Env aller Adapter.

Der Prompt wird immer als BYTES geschrieben (kein `text=True`) — einheitlich
über alle Adapter; `poll` dekodiert stdout/stderr.

stdout/stderr werden ab dem Spawn von einem Daemon-Thread (`_PipeDrain`)
geleert. Würden sie erst nach Prozessende gelesen, füllt sich bei großem
Output (z. B. copilot-JSONL) der Pipe-Puffer (~64 KiB), die CLI blockiert
beim Schreiben und verwirft beim Shutdown den Rest (copilot:
`Timed out dispose: PromptMode.stdout`) — inkl. finalem `result`-Event.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid

from .. import procs, sandbox
from ..agentenv import build_agent_env
from ..billing import Billing
from ..i18n import t
from ..runmodel import FailureKind
from ..tokens import TokenCounts

# Ende von stderr, das ein `failed`-Ergebnis mitnimmt — groß genug für einen Crash-Banner samt
# Stack (Bun-Panic, Python-Traceback), klein genug für Run-Record und Run-Log.
STDERR_TAIL_CHARS = 4000


def _tail(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    """Letzte `limit` Zeichen von `text`, ab einer Zeilengrenze (sofern es im Ausschnitt eine gibt)."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return cut[newline + 1 :] if newline != -1 else cut


class _PipeDrain:
    """Liest stdout/stderr eines Prozesses im Hintergrund (`communicate()`),
    damit die CLI nie an einem vollen Pipe-Puffer blockiert."""

    def __init__(self, proc: subprocess.Popen):
        self._result: tuple = (b"", b"")
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(proc,), daemon=True)
        self._thread.start()

    def _run(self, proc: subprocess.Popen) -> None:
        try:
            self._result = proc.communicate()
        except BaseException as e:  # noqa: BLE001 — im Aufrufer-Thread re-raisen
            self._error = e

    def result(self) -> tuple:
        """Wartet auf EOF beider Streams → `(stdout, stderr)`."""
        self._thread.join()
        if self._error is not None:
            raise self._error
        return self._result


class OneShotCliBackend:
    """Base für One-Shot-CLI-Adapter (ein Subprozess pro Task, läuft bis zum Ende)."""

    name = "oneshot"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900):
        # endpoint ist für CLI-Adapter irrelevant (Interface-Kompatibilität zum Port).
        self.model = model
        self.stale_after = stale_after
        self.extra_env: dict[str, str] = {}
        self._sessions: dict[str, dict] = {}

    # ------------------------------------------------------------------ Hooks
    def _cli(self) -> str:
        raise NotImplementedError

    def _build_cmd(self) -> list[str]:
        raise NotImplementedError

    def _parse(self, out: str, err: str, returncode: int) -> dict:
        raise NotImplementedError

    def billing(self) -> Billing:
        """Abrechnungsart aus der übergebenen Auth (``fabrik.billing``); Default: nicht ableitbar."""
        return Billing.UNKNOWN

    def _env(self) -> dict | None:
        """Zusätzliche Env-Variablen für den Subprozess (Overlay auf der Agent-Env-Allowlist,
        ADR 0050). Das Basis-`os.environ` gehört NICHT dazu — die Allowlist (`agentenv`)
        liefert die Basis."""
        return None

    def _sid_prefix(self) -> str:
        return f"{self.name}-"

    def _stale_after(self) -> int | None:
        return self.stale_after

    # ------------------------------------------------------------- Mechanik
    def health(self) -> bool:
        return shutil.which(self._cli()) is not None

    def diagnose(self) -> str | None:
        cli = self._cli()
        return None if shutil.which(cli) else t("backends._oneshot.backend_cli_not_in_path", v1=self.name, cli=cli)

    def unhealthy_kind(self) -> FailureKind:
        return FailureKind.ENVIRONMENT  # unhealthy heißt hier: CLI nicht im PATH

    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        sid = f"{self._sid_prefix()}{uuid.uuid4().hex[:12]}"
        self._sessions[sid] = {"proc": None, "workdir": workdir, "started": time.time()}
        return sid

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        s = self._sessions[session_id]
        if s["proc"] is not None:
            raise RuntimeError(t("backends._oneshot.one_shot_backend_exactly_one", v1=self.name))
        kwargs: dict = dict(
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # eigene PG — cancel() kann die Gruppe killen
        )
        env = build_agent_env({**(self._env() or {}), **self.extra_env})
        cmd = self._build_cmd()
        if sandbox.sandbox_enabled():
            # ADR 0054: Agent-Befehl im gehärteten Container; Worktree-Stand per git-Bundle
            # hinein, Ergebnis in poll() zurück (kein Mount des Worktrees).
            cfg = sandbox.SandboxConfig.from_env()
            s["sandbox"] = sandbox.prepare_exchange(session_id, directory)
            cmd = sandbox.render_docker_cmd(cfg, session_id, cmd, s["sandbox"], env)
        kwargs["env"] = sandbox.docker_cli_env(env) if sandbox.sandbox_enabled() else env
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError:
            self._discard_sandbox(s, directory)
            raise
        procs.track(proc)  # Server-Ende beendet die Gruppe mit (eigene Session: kein Ctrl+C)
        if proc.stdin is None:  # pragma: no cover — stdin=PIPE, nur Typsicherheit
            raise RuntimeError(t("backends._oneshot.popen_without_stdin", v1=self.name))
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        # stdin ist zu (EOF für die CLI). Auf None setzen, damit poll()s
        # proc.communicate() den geschlossenen Stream nicht erneut flushen
        # will (sonst: ValueError: flush of closed file).
        proc.stdin = None
        s["proc"] = proc
        s["drain"] = _PipeDrain(proc)

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status: running|done|failed, text, tokens, cost, tools}

        Liveness/Staleness ist hier; die Terminal-Auswertung (done/failed,
        Text, Tokens, Cost) delegiert an `_parse`.
        """
        s = self._sessions[session_id]
        proc = s["proc"]
        base = {"tokens": TokenCounts().as_poll(), "cost": 0.0, "tools": []}
        if proc is None:
            return {**base, "status": "running", "text": ""}
        rc = proc.poll()
        if rc is None:
            stale = self._stale_after()
            if stale is not None:
                age = time.time() - s["started"]
                if age > stale:
                    if "sandbox" in s:  # Container stoppen, Austausch verwerfen
                        self.cancel(session_id, directory)
                    # `retryable`: hängende CLI ist transient — eine frische
                    # Session heilt es (Agent-Step-Retry, ADR 0035).
                    return {
                        **base,
                        "status": "failed",
                        "retryable": True,
                        "text": t("backends._oneshot.inactive_for_s_no_activity", v1=self.name, v2=int(age)),
                    }
            return {**base, "status": "running", "text": ""}
        procs.untrack(proc)
        drain = s.get("drain") or _PipeDrain(proc)
        out_b, err_b = drain.result()
        out = out_b.decode(errors="replace") if isinstance(out_b, bytes) else (out_b or "")
        err = err_b.decode(errors="replace") if isinstance(err_b, bytes) else (err_b or "")
        exchange = s.pop("sandbox", None)
        if exchange is not None:
            # Ergebnis des Containers in den Worktree übernehmen — auch bei Exit ≠ 0
            # (wie ein Agent auf dem Host, dessen Änderungen nach einem Fehler liegen bleiben).
            error = sandbox.finish_exchange(exchange, directory)
            if error:
                return self._with_exit({**base, "status": "failed", "text": f"{self.name}: Sandbox — {error}"}, rc, err)
        return self._with_exit(self._parse(out, err, rc), rc, err)

    @staticmethod
    def _with_exit(res: dict, returncode: int, err: str) -> dict:
        """`failed`-Ergebnis um `exit_code` und `stderr_tail` ergänzen (Moduldocstring)."""
        if res.get("status") != "failed":
            return res
        return {**res, "exit_code": returncode, "stderr_tail": _tail(err)}

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """One-Shot-CLIs haben keinen abrufbaren Dialog → leer.

        Der Final-Output liegt in `poll()`'s `text`; `_step_agent_backend`
        emittiert ihn ins Run-Log (`agent final: …`) und in die Session-Log-
        DB (message-Event)."""
        return []

    def cancel(self, session_id: str, directory: str) -> None:
        s = self._sessions.get(session_id)
        if not s:
            return
        # Zuerst: poll() darf das Ergebnis eines abgebrochenen Laufs nicht mehr übernehmen.
        exchange = s.pop("sandbox", None)
        proc = s["proc"]
        if proc is not None and proc.poll() is None:
            procs.kill_group(proc)
            procs.untrack(proc)
        if exchange is not None:
            # killpg beendet nur die docker-CLI — der Container läuft weiter.
            sandbox.stop_container(session_id)
            sandbox.discard_exchange(exchange, directory)

    @staticmethod
    def _discard_sandbox(s: dict, directory: str) -> None:
        exchange = s.pop("sandbox", None)
        if exchange is not None:
            sandbox.discard_exchange(exchange, directory)
