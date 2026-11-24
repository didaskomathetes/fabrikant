"""CLI LLM provider: chat completion via `claude -p` / `copilot` instead of HTTP.

A provider with ``FABRIKANT_LLM_<NAME>_CLI=claude|copilot`` (instead of ``_URL``)
calls the local CLI — auth comes from the CLI login, no API key.

Reuse: the one-shot agent adapters (`backends/`) provide the process
mechanics (Popen, pipe drain, cancel) and output parsing. The chat variants
here only override the argv: **no tools** (pure text answer, no
shell/file access from chat), no repo instructions, neutral
workdir. `temperature`/`max_tokens` are unknown to the CLIs — ignored.
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
# Neutral workdir: the CLIs would otherwise load the checkout's CLAUDE.md/AGENTS.md.
_WORKDIR = config.STATE_DIR / "llm-cli"


class LlmCliError(StepFailure, RuntimeError):
    """CLI call failed, aborted or timed out; carries the failure class (ADR 0035)."""


class _ClaudeChat(ClaudeCodeBackend):
    """`claude -p` without tools; system prompt via file (no ARG_MAX)."""

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
    """Prompt to the one-shot adapter, poll until the end; cancel/timeout kills the CLI."""
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
        fd, path = tempfile.mkstemp(prefix="fabrikant-sys-", suffix=".md")
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
