"""Agent backend port (ports & adapters / hexagonal).

The port is the contract defined by the core (`steps/agent.py`). Adapters
(`opencode`, `claude_code`, `codex`, `copilot`) implement it; the
dispatcher depends ONLY on the port, never on concrete adapters (dependency
rule). Structural (duck typing) — adapters do not have to inherit, they only
have to fulfill the signature. `OneShotCliBackend` (`_oneshot.py`) is a
concrete base that bundles the shared one-shot CLI mechanics; it is
not part of the port but an adapter helper class.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..runmodel import FailureKind


@runtime_checkable
class AgentBackend(Protocol):
    """Lifecycle of an agent task: create_task → send_prompt → poll* → (cancel).

    `name` is a class attribute (backend identifier, e.g. "copilot").
    `poll` returns `{status: running|done|failed, text, tokens, cost, tools}`; on `failed` optionally
    `retryable` (transient, session restart worthwhile) or `failure_kind`/`retry_after` (ADR 0035).
    One-shot CLIs add `exit_code` and `stderr_tail` on `failed` (end of stderr, for crashes).
    Optional: `billing() -> fabrik.billing.Billing` (billing mode derived from the given auth);
    if absent, `unknown` applies.

    """

    name: str

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> AgentBackend:
        """Constructs the adapter; the adapter extracts specific options (sandbox, tools, …)
        from `step`/env itself."""
        ...

    def health(self) -> bool:
        """True if the backend is available (CLI in PATH / server reachable)."""
        ...

    def diagnose(self) -> str | None:
        """Human-readable diagnosis when `health()` is False (None = no
        additional information). Serves the health-gate error in the agent step,
        so an unhealthy backend names the cause (e.g. wrong port,
        missing password) instead of just 'unhealthy'."""
        ...

    def unhealthy_kind(self) -> FailureKind:
        """Failure class when `health()` is False (ADR 0035): server briefly gone → `transient`,
        missing CLI or rejected auth → `environment`."""
        ...

    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        """Reserves a task/session and returns the session_id."""
        ...

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        """Starts execution of the prompt in the session."""
        ...

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status, text, tokens, cost, tools} (idempotent, tick-orientiert)."""
        ...

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """Complete agent dialog as normalized messages `[{info, parts}]`
        (oldest first, assistant only). Serves the run log (conversation).

        One-shot CLI backends have no retrievable dialog → return `[]`
        (their final output is already in `poll()`'s `text`)."""
        ...

    def cancel(self, session_id: str, directory: str) -> None:
        """Aborts the session (no-op if unknown/already finished)."""
        ...
