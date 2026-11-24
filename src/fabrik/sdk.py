"""Public plugin API of fabrik (ADR 0069) — the only module whose names follow semantic versioning.

A custom step is a plain function registered under a new type name::

    from fabrik.sdk import ok, failed, FailureKind

    def deploy(ctx, step, emit):
        emit(f"deploying {step['target']}")
        return ok("deployed") if _deploy(step["target"]) else failed("deploy broke", FailureKind.OUTCOME)

    # pyproject.toml of your plugin package:
    # [project.entry-points."fabrik.steps"]
    # deploy = "my_pkg.steps:deploy"

``ctx`` carries the run context (``root`` = workdir, ``env``, emitted variables, …); ``step`` is the
step mapping from the spec; ``emit(msg)`` writes a line to the run log. Use ``fabrik.steps.registry
.StepPlugin`` as the entry-point target if the step needs a JSON schema or the git worktree.
"""

from __future__ import annotations

from typing import Any

from .context import _substitute as substitute
from .failures import FailureKind, failed
from .steps.registry import StepHandler, StepPlugin, StepRegistryError, register_step

__all__ = [
    "FailureKind",
    "StepHandler",
    "StepPlugin",
    "StepRegistryError",
    "failed",
    "ok",
    "register_step",
    "substitute",
]


def ok(summary: str, **extra: Any) -> dict:
    """Successful step result: ``ok("deployed", output=...)``."""
    return {"ok": True, "summary": summary, **extra}
