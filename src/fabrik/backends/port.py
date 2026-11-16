"""Agent-Backend-Port (Ports & Adapters / Hexagonal).

Der Port ist der vom Core (`steps/agent.py`) definierte Vertrag. Adapter
(`opencode`, `claude_code`, `codex`, `copilot`) implementieren ihn; der
Dispatcher hängt NUR vom Port ab, nie von konkreten Adaptern (Dependency
Rule). Strukturell (Duck-Typing) — Adapter müssen nicht erben, sie müssen
nur die Signatur erfüllen. `OneShotCliBackend` (`_oneshot.py`) ist eine
konkrete Base, die die gemeinsame One-Shot-CLI-Mechanik bündelt; sie ist
kein Teil des Ports, sondern eine Adapter-Hilfsklasse.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..runmodel import FailureKind


@runtime_checkable
class AgentBackend(Protocol):
    """Lebenszyklus einer Agent-Task: create_task → send_prompt → poll* → (cancel).

    `name` ist ein Class-Attribut (Backend-Identifikator, z. B. "copilot").
    `poll` liefert `{status: running|done|failed, text, tokens, cost, tools}`; bei `failed` optional
    `retryable` (transient, Session-Neustart lohnt) bzw. `failure_kind`/`retry_after` (ADR 0035).
    """

    name: str

    @classmethod
    def create(cls, step: dict, endpoint: str, model: str, stale_after: int) -> AgentBackend:
        """Konstruiert den Adapter; spezifische Optionen (sandbox, tools, …)
        extrahiert der Adapter selbst aus `step`/Env."""
        ...

    def health(self) -> bool:
        """True, wenn das Backend verfügbar ist (CLI im PATH / Server erreichbar)."""
        ...

    def diagnose(self) -> str | None:
        """Menschenlesbare Diagnose, wenn `health()` False ist (None = keine
        zusätzliche Information). Dient dem Health-Gate-Error im Agent-Step,
        damit ein unhealthy-Backend die Ursache nennt (z. B. falscher Port,
        fehlendes Passwort) statt nur 'unhealthy'."""
        ...

    def unhealthy_kind(self) -> FailureKind:
        """Fehlerklasse, wenn `health()` False ist (ADR 0035): Server kurz weg → `transient`,
        fehlende CLI oder abgelehnte Auth → `environment`."""
        ...

    def create_task(self, workdir: str, title: str, permissions: list | None = None) -> str:
        """Reserviert eine Task/Session und liefert die session_id."""
        ...

    def send_prompt(self, session_id: str, prompt: str, directory: str) -> None:
        """Startet die Ausführung des Prompts in der Session."""
        ...

    def poll(self, session_id: str, directory: str) -> dict:
        """→ {status, text, tokens, cost, tools} (idempotent, tick-orientiert)."""
        ...

    def transcript(self, session_id: str, directory: str) -> list[dict]:
        """Vollständiger Agent-Dialog als normalisierte Messages `[{info, parts}]`
        (älteste zuerst, nur Assistant). Dient dem Run-Log (Konversation).

        One-Shot-CLI-Backends haben keinen abrufbaren Dialog → liefern `[]`
        (deren Final-Output steckt bereits in `poll()`'s `text`)."""
        ...

    def cancel(self, session_id: str, directory: str) -> None:
        """Bricht die Session ab (no-op, wenn unbekannt/bereits beendet)."""
        ...
