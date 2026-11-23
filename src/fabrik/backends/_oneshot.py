"""OneShotCliBackend: shared one-shot CLI mechanics for CLI adapters.

Bundles what `claude_code`, `codex` and `copilot` need identically:
session registry, `Popen` (prompt via stdin, own process group), the
`poll` liveness/staleness loop and `killpg` cancel. Adapters (subclasses)
only define the differences:

  - `name`            class attribute (backend identifier)
  - `_cli()`          binary name for `health()`
  - `_build_cmd()`    argv WITHOUT prompt (prompt goes via stdin)
  - `_parse(out, err, rc)` → {status, text, tokens, cost, tools}

A `failed` result additionally carries `exit_code` and `stderr_tail` (the last
`STDERR_TAIL_CHARS` characters of stderr): `_parse` truncates its `text` to the beginning, but on
a crash (CLI crash banner, Python traceback) the cause is at the end.

Optional hooks: `_env()` (additional process env), `_sid_prefix()`,
`_stale_after()` (None = staleness check disabled, e.g. claude_code).
`extra_env` (set by the agent step, e.g. the GitHub identity) overlays
the process env of all adapters.

The prompt is always written as BYTES (no `text=True`) — uniform
across all adapters; `poll` decodes stdout/stderr.

From the spawn on, stdout/stderr are drained by a daemon thread (`_PipeDrain`).
If they were read only after the process ended, large output (e.g. copilot JSONL)
would fill the pipe buffer (~64 KiB), the CLI would block on
writing and discard the rest on shutdown (copilot:
`Timed out dispose: PromptMode.stdout`) — including the final `result` event.
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

# Tail of stderr carried by a `failed` result — large enough for a crash banner including
# stack (Bun panic, Python traceback), small enough for run record and run log.
STDERR_TAIL_CHARS = 4000


def _tail(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    """Last `limit` characters of `text`, starting at a line boundary (if there is one in the excerpt)."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return cut[newline + 1 :] if newline != -1 else cut


class _PipeDrain:
    """Reads a process's stdout/stderr in the background (`communicate()`),
    so the CLI never blocks on a full pipe buffer."""

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
    """Base for one-shot CLI adapters (one subprocess per task, runs to completion)."""

    name = "oneshot"

    def __init__(self, endpoint: str = "", model: str = "", stale_after: int = 900):
        # endpoint is irrelevant for CLI adapters (interface compatibility with the port).
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
        """Additional env variables for the subprocess (overlay on the agent env allowlist,
        ADR 0050). The base `os.environ` is NOT part of it — the allowlist (`agentenv`)
        provides the base."""
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
        return FailureKind.ENVIRONMENT  # unhealthy here means: CLI not in PATH

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
            start_new_session=True,  # own process group — cancel() can kill the group
        )
        env = build_agent_env({**(self._env() or {}), **self.extra_env})
        cmd = self._build_cmd()
        if sandbox.sandbox_enabled():
            # ADR 0054: agent command in the hardened container; worktree state goes in via git bundle,
            # result comes back in poll() (no mount of the worktree).
            cfg = sandbox.SandboxConfig.from_env()
            s["sandbox"] = sandbox.prepare_exchange(session_id, directory)
            cmd = sandbox.render_docker_cmd(cfg, session_id, cmd, s["sandbox"], env)
        kwargs["env"] = sandbox.docker_cli_env(env) if sandbox.sandbox_enabled() else env
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError:
            self._discard_sandbox(s, directory)
            raise
        procs.track(proc)  # Server end terminates the group too (own session: no Ctrl+C)
        if proc.stdin is None:  # pragma: no cover — stdin=PIPE, type safety only
            raise RuntimeError(t("backends._oneshot.popen_without_stdin", v1=self.name))
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        # stdin is closed (EOF for the CLI). Set to None so poll()'s
        # proc.communicate() does not try to flush the closed stream again
        # (otherwise: ValueError: flush of closed file).
        proc.stdin = None
        s["proc"] = proc
        s["drain"] = _PipeDrain(proc)

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status: running|done|failed, text, tokens, cost, tools}

        Liveness/staleness is here; the terminal evaluation (done/failed,
        text, tokens, cost) is delegated to `_parse`.
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
                    # `retryable`: a hanging CLI is transient — a fresh
                    # session heals it (agent step retry, ADR 0035).
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
            # Take over the container's result into the worktree — also on exit ≠ 0
            # (like an agent on the host whose changes remain after an error).
            error = sandbox.finish_exchange(exchange, directory)
            if error:
                return self._with_exit({**base, "status": "failed", "text": f"{self.name}: Sandbox — {error}"}, rc, err)
        return self._with_exit(self._parse(out, err, rc), rc, err)

    @staticmethod
    def _with_exit(res: dict, returncode: int, err: str) -> dict:
        """Add `exit_code` and `stderr_tail` to a `failed` result (module docstring)."""
        if res.get("status") != "failed":
            return res
        return {**res, "exit_code": returncode, "stderr_tail": _tail(err)}

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """One-shot CLIs have no retrievable dialog → empty.

        The final output is in `poll()`'s `text`; `_step_agent_backend`
        emits it into the run log (`agent final: …`) and the session-log
        DB (message event)."""
        return []

    def cancel(self, session_id: str, directory: str) -> None:
        s = self._sessions.get(session_id)
        if not s:
            return
        # First: poll() must no longer take over the result of a cancelled run.
        exchange = s.pop("sandbox", None)
        proc = s["proc"]
        if proc is not None and proc.poll() is None:
            procs.kill_group(proc)
            procs.untrack(proc)
        if exchange is not None:
            # killpg only terminates the docker CLI — the container keeps running.
            sandbox.stop_container(session_id)
            sandbox.discard_exchange(exchange, directory)

    @staticmethod
    def _discard_sandbox(s: dict, directory: str) -> None:
        exchange = s.pop("sandbox", None)
        if exchange is not None:
            sandbox.discard_exchange(exchange, directory)
