"""Run launcher port (ports & adapters / hexagonal).

The port is the contract defined by the core (``engine.py``) for the
**execution substrate**: *where* a fully specified run-work call
is executed. The **scheduling policy** (claim, dedup, priority,
``queued`` records) lives in the core and stays substrate-independent; the launcher
only decides whether the work runs in a thread (in-process), a k8s job or
an ECS task.

Structural (duck typing) — adapters do not have to inherit, they only have to fulfill the
signature (like :class:`fabrikant.backends.port.AgentBackend`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypedDict, runtime_checkable


class LauncherStatus(TypedDict):
    """Substrate observability: pool size (`workers`), busy workers (`busy`),
    queued work (`queue`). In-process: the worker pool; remote substrates
    report the corresponding capacity figures."""

    workers: int
    busy: int
    queue: int


@runtime_checkable
class RunLauncher(Protocol):
    """Substrate that executes run work (in-process threads, k8s jobs, ECS tasks).

    `name` is a class attribute (substrate identifier, e.g. "inprocess").
    Work callables are zero-arg closures built by the core (claim + ``run_loop``
    + release); the launcher executes them and knows nothing about runs.
    """

    name: str

    def submit(self, work: Callable[[], None]) -> None:
        """Fire-and-forget work item (trigger runs, retry attempts).

        In-process: queue into the worker pool. Remote: dispatch job/task.
        Must not raise for a normal submission."""
        ...

    def submit_priority(self, work: Callable[[], None]) -> None:
        """Work item with priority that may wait for capacity, in an
        isolated context (manual/resume runs).

        In-process: own daemon thread (blocks on the slot, occupies no
        pool worker). If the context does not start, ``RuntimeError`` is
        passed on — the core then cleans up claim/wait counter."""
        ...

    def status(self) -> LauncherStatus:
        """Substrat-Beobachtbarkeit (in-process: ``workers``/``busy``/``queue``)."""
        ...

    def shutdown(self) -> None:
        """Drain the substrate (server end). Idempotent, no-op if nothing is running."""
        ...
