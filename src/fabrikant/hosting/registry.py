"""Provider registry (ADR 0063).

Maps a provider *name* (``github``, ``gitlab``, ``codeberg``, …) to a *factory* that
builds the adapter. This is what turns the hosting port from "two hardcoded adapters"
into "an open set of registrable adapters": third parties can add a host (Codeberg, a
custom GitLab, Gitea, …) without touching fabrikant core.

A **factory** has the signature ``(repo, spec, role) -> Provider`` and encapsulates all
of that provider's construction (host resolution, credentials, …). Built-in adapters
register themselves at import; third-party adapters register via the
``fabrikant.hosting_provider`` entry point (see :func:`load_entry_points`) or by calling
:func:`register_provider` directly.

The registry is intentionally tiny and host-agnostic: the same mechanism is meant to be
reused for other pluggable ports (e.g. an issue tracker, ADR 0063 "future work").
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import HostingConfigError

if TYPE_CHECKING:
    from .base import Provider

#: A factory builds a :class:`Provider` from the selection context
#: (``repo``: an ``owner/name`` slug or URL or ``None``; ``spec``: the run spec;
#: ``role``: the GitHub dual-identity role, ignored by most adapters).
ProviderFactory = Callable[["str | None", "dict", "str"], "Provider"]

#: Entry-point group third-party hosting providers register under.
ENTRY_POINT_GROUP = "fabrikant.hosting_provider"

_log = logging.getLogger(__name__)

_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    """Register ``factory`` under ``name`` (e.g. ``"github"``, ``"codeberg"``).

    ``name`` is case-insensitive and trimmed. Raises :class:`HostingConfigError` if
    ``name`` is empty, ``factory`` is not callable, or ``name`` is already registered
    and ``replace`` is false.
    """
    name = name.strip().lower()
    if not name:
        raise HostingConfigError("provider name must be non-empty")
    if not callable(factory):
        raise HostingConfigError(f"provider factory for {name!r} must be callable")
    if not replace and name in _REGISTRY:
        raise HostingConfigError(f"provider {name!r} is already registered")
    _REGISTRY[name] = factory


def get_factory(name: str) -> ProviderFactory:
    """The factory registered under ``name``; raises :class:`HostingConfigError` if unknown."""
    name = name.strip().lower()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise HostingConfigError(f"unknown hosting provider {name!r}; registered: {known}") from None


def registered_providers() -> list[str]:
    """The names of all registered providers (sorted)."""
    return sorted(_REGISTRY)


def load_entry_points() -> None:
    """Discover third-party providers via the ``fabrikant.hosting_provider`` entry point.

    Each entry point must point to a **factory** (a callable ``(repo, spec, role) ->
    Provider``), not an adapter class. A broken entry point is skipped, never fatal —
    provider selection must not crash because an optional plugin failed to load.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — importlib.metadata is stdlib since 3.8
        return
    try:
        eps = entry_points()
        selected = (
            eps.select(group=ENTRY_POINT_GROUP)
            if hasattr(eps, "select")  # Python >= 3.10
            else eps.get(ENTRY_POINT_GROUP, [])  # pragma: no cover — Python 3.8/3.9
        )
    except Exception:  # pragma: no cover — never fail selection on a broken entry-point DB
        return
    for ep in selected:
        try:
            factory = ep.load()
        except Exception as exc:  # skip a plugin that fails to import, but say so
            _log.warning("hosting provider entry point %r failed to load: %s", ep.name, exc)
            continue
        register_provider(ep.name, factory, replace=True)
