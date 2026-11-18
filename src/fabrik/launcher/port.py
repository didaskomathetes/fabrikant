"""Run-Launcher-Port (Ports & Adapters / Hexagonal).

Der Port ist der vom Core (``engine.py``) definierte Vertrag für das
**Ausführungs-Substrat**: *wo* ein vollständig spezifizierter Run-Work-Call
ausgeführt wird. Die **Scheduling-Policy** (Claim, Dedup, Vorrang,
``queued``-Records) lebt im Core und bleibt substrat-unabhängig; der Launcher
entscheidet nur, ob das Work in einem Thread (in-process), einem k8s-Job oder
einer ECS-Task läuft.

Strukturell (Duck-Typing) — Adapter müssen nicht erben, sie müssen nur die
Signatur erfüllen (wie :class:`fabrik.backends.port.AgentBackend`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class RunLauncher(Protocol):
    """Substrat, das Run-Work ausführt (in-process Threads, k8s-Jobs, ECS-Tasks).

    `name` ist ein Class-Attribut (Substrat-Identifikator, z. B. "inprocess").
    Work-Callables sind Zero-Arg-Closures, die der Core baut (Claim + ``run_loop``
    + Release); der Launcher führt sie aus und weiß nichts über Runs.
    """

    name: str

    def submit(self, work: Callable[[], None]) -> None:
        """Fire-and-forget Work-Item (Trigger-Runs, Retry-Versuche).

        In-process: in den Worker-Pool einreihen. Remote: Job/Task dispatchen.
        Darf für eine normale Einreichung nicht werfen."""
        ...

    def submit_priority(self, work: Callable[[], None]) -> None:
        """Work-Item mit Vorrang, das auf Kapazität warten darf, in einem
        isolierten Kontext (manuelle/Resume-Runs).

        In-process: eigener Daemon-Thread (blockiert auf den Slot, belegt keinen
        Pool-Worker). Startet der Kontext nicht, wird ``RuntimeError``
        weitergereicht — der Core räumt dann Claim/Wartezähler auf."""
        ...

    def status(self) -> dict:
        """Substrat-Beobachtbarkeit (in-process: ``workers``/``busy``/``queue``)."""
        ...

    def shutdown(self) -> None:
        """Substrat drainen (Server-Ende). Idempotent, No-Op wenn nichts läuft."""
        ...
