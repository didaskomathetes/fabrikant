"""Step plugin registry (ADR 0069).

Built-in step types are dispatched by the engine (``config.STEP_TYPES``); this registry holds
the *third-party* types. A plugin is a callable ``(ctx, step, emit) -> result`` registered under a
new type name, either by calling :func:`register_step` or through the ``fabrik.steps`` entry-point
group (:func:`load_entry_points`). Plugins cannot shadow built-in types.

Result contract: the dict the built-in steps return — ``{"ok": bool, "summary": str, ...}``;
use ``fabrik.sdk.ok`` / ``fabrik.sdk.failed`` to build it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import config

#: ``(ctx, step, emit) -> {"ok": bool, "summary": str, ...}``
StepHandler = Callable[[dict, dict, Callable[[str], None]], dict]

#: Entry-point group third-party step types register under.
ENTRY_POINT_GROUP = "fabrik.steps"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_log = logging.getLogger(__name__)


class StepRegistryError(ValueError):
    """Invalid registration (bad name, shadowing a built-in, duplicate, non-callable handler)."""


@dataclass(frozen=True)
class StepPlugin:
    """A registered third-party step type."""

    name: str
    handler: StepHandler
    #: Optional JSON-Schema fragment validating the step mapping (e.g. its required fields).
    schema: dict[str, Any] | None = None
    #: True if the step needs the project's git worktree (loops with ``worktree: false`` reject it).
    needs_worktree: bool = False


_PLUGINS: dict[str, StepPlugin] = {}
_lock = threading.Lock()
_entry_points_loaded = False


def register_step(
    name: str,
    handler: StepHandler,
    *,
    schema: dict[str, Any] | None = None,
    needs_worktree: bool = False,
    replace: bool = False,
) -> None:
    """Register ``handler`` as step type ``name`` (lowercase ``[a-z][a-z0-9_]*``).

    Raises :class:`StepRegistryError` for an invalid or built-in name, a non-callable handler, or an
    already registered name (unless ``replace``)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise StepRegistryError(f"invalid step type name {name!r} (expected [a-z][a-z0-9_]*)")
    if name in config.STEP_TYPES:
        raise StepRegistryError(f"step type {name!r} is built in and cannot be overridden")
    if not callable(handler):
        raise StepRegistryError(f"handler for step type {name!r} is not callable")
    with _lock:
        if name in _PLUGINS and not replace:
            raise StepRegistryError(f"step type {name!r} is already registered")
        _PLUGINS[name] = StepPlugin(name, handler, schema, needs_worktree)


def unregister_step(name: str) -> None:
    """Remove a plugin step type (no-op if unknown). Mainly for tests."""
    with _lock:
        _PLUGINS.pop(name, None)


def get_step(name: str) -> StepPlugin | None:
    """The plugin registered as ``name``, or ``None``. Discovers entry points on first use."""
    load_entry_points()
    return _PLUGINS.get(name)


def plugin_types() -> list[str]:
    """Names of all registered plugin step types (sorted)."""
    load_entry_points()
    return sorted(_PLUGINS)


def known_types() -> tuple[str, ...]:
    """All valid step types: built-ins followed by plugins."""
    return (*config.STEP_TYPES, *plugin_types())


def worktree_types() -> frozenset[str]:
    """Step types that need the git worktree (built-ins plus plugins that declared it)."""
    load_entry_points()
    return frozenset(config.WORKTREE_STEP_TYPES) | {p.name for p in _PLUGINS.values() if p.needs_worktree}


def load_entry_points(*, force: bool = False) -> None:
    """Discover plugins via the ``fabrik.steps`` entry-point group (once per process unless ``force``).

    An entry point must resolve to a :data:`StepHandler`, or to a :class:`StepPlugin` for a step with
    a schema. A broken entry point is logged and skipped, never fatal."""
    global _entry_points_loaded
    with _lock:
        if _entry_points_loaded and not force:
            return
        _entry_points_loaded = True
    from importlib.metadata import entry_points

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = ep.load()
            if isinstance(target, StepPlugin):
                register_step(
                    ep.name, target.handler, schema=target.schema, needs_worktree=target.needs_worktree, replace=True
                )
            else:
                register_step(ep.name, target, replace=True)
        except Exception as exc:  # noqa: BLE001 — a broken plugin must not break fabrik
            _log.warning("step entry point %r failed to load: %s", ep.name, exc)
