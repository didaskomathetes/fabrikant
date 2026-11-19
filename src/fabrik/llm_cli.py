"""CLI-LLM-Provider: Chat-Completion über `claude -p` / `copilot` statt HTTP.

Ein Provider mit ``FABRIK_LLM_<NAME>_CLI=claude|copilot`` (statt ``_URL``)
ruft die lokale CLI auf — Auth kommt aus der CLI-Anmeldung, kein API-Key.

Wiederverwendung: die One-Shot-Agent-Adapter (`backends/`) liefern Prozess-
Mechanik (Popen, Pipe-Drain, Cancel) und Output-Parsing. Die Chat-Varianten
hier überschreiben nur das argv: **keine Tools** (reine Text-Antwort, kein
Shell-/Datei-Zugriff aus dem Chat), keine Repo-Instruktionen, neutrale
Workdir. `temperature`/`max_tokens` kennen die CLIs nicht — ignoriert.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

from . import config
from .backends._oneshot import OneShotCliBackend
from .backends.claude_code import ClaudeCodeBackend
from .backends.copilot import CopilotBackend
from .failures import StepFailure, classify_agent_output
from .i18n import t
from .runmodel import FailureKind

_POLL_INTERVAL_S = 0.2
# Neutrale Workdir: die CLIs laden sonst CLAUDE.md/AGENTS.md des Checkouts.
_WORKDIR = config.STATE_DIR / "llm-cli"


class LlmCliError(StepFailure, RuntimeError):
    """CLI-Call fehlgeschlagen, abgebrochen oder Timeout; trägt die Fehlerklasse (ADR 0035)."""


class _ClaudeChat(ClaudeCodeBackend):
    """`claude -p` ohne Tools; System-Prompt per Datei (kein ARG_MAX)."""

    def __init__(self, model: str, system_file: str):
        super().__init__(model=model)
        self.system_file = system_file

    def _build_cmd(self) -> list[str]:
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--tools",
            "",
            "--no-session-persistence",
            "--system-prompt-file",
            self.system_file,
        ]


class _CopilotChat(CopilotBackend):
    """`copilot` ohne Tools/MCP/Custom-Instructions (System-Prompt via stdin)."""

    def _build_cmd(self) -> list[str]:
        cmd = ["copilot", "--output-format", "json", "--no-ask-user"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + ["--available-tools", "", "--disable-builtin-mcps", "--no-custom-instructions"]


CLIS = ("claude", "copilot")


def _run(backend: OneShotCliBackend, prompt: str, timeout: int, cancel_event: threading.Event | None) -> dict:
    """Prompt an den One-Shot-Adapter, bis Ende pollen; Cancel/Timeout killt die CLI."""
    _WORKDIR.mkdir(parents=True, exist_ok=True)
    workdir = str(_WORKDIR)
    sid = backend.create_task(workdir, "chat")
    backend.send_prompt(sid, prompt, workdir)
    deadline = time.monotonic() + timeout
    while True:
        res = backend.poll(sid, workdir)
        if res["status"] != "running":
            return res
        if cancel_event is not None and cancel_event.is_set():
            backend.cancel(sid, workdir)
            raise LlmCliError(t("llm_cli.cancelled"))
        if time.monotonic() > deadline:
            backend.cancel(sid, workdir)
            raise LlmCliError(f"Timeout nach {timeout}s")
        time.sleep(_POLL_INTERVAL_S)


def complete(
    cli: str, model: str, system: str, user: str, timeout: int, cancel_event: threading.Event | None = None
) -> tuple[str, dict]:
    """→ ``(text, {"input": n, "output": n})``. Wirft ``LlmCliError``."""
    if cli == "claude":
        fd, path = tempfile.mkstemp(prefix="fabrik-sys-", suffix=".md")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(system)
            res = _run(_ClaudeChat(model, path), user, timeout, cancel_event)
        finally:
            os.unlink(path)
    elif cli == "copilot":
        res = _run(_CopilotChat(model=model), f"{system}\n\n---\n\n{user}", timeout, cancel_event)
    else:
        raise LlmCliError(f"unbekannte CLI '{cli}' (erlaubt: {', '.join(CLIS)})", FailureKind.ENVIRONMENT)
    if res["status"] != "done":
        text = str(res.get("text") or "")
        failure = classify_agent_output(text)
        raise LlmCliError(f"{cli}: {text or 'fehlgeschlagen'}", failure.kind, failure.retry_after)
    return res.get("text") or "", res.get("tokens") or {}
