"""Deployment profile (`homelab|cloud`) and adapter registry (ADR 0056).

The profile is resolved once at startup (CLI > ``FABRIKANT_PROFILE`` > default)
and selects the concrete implementation per port. Only config and composition root
(`cli.py`) know the profile name; business logic gets ready-made adapters
(guard: ``tests/test_profiles.py``).

New port: register ``Port`` member + adapter per profile in ``_build_default_registry``
— the selection logic stays unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from . import runstore
from .i18n import t

PROFILE_ENV = "FABRIKANT_PROFILE"


class Profile(StrEnum):
    """Deployment-Profil."""

    HOMELAB = "homelab"
    CLOUD = "cloud"


class Port(StrEnum):
    """Replaceable infrastructure interface (later e.g. secrets, queue, worker)."""

    STORAGE = "storage"


DEFAULT_PROFILE = Profile.HOMELAB


class ProfileError(ValueError):
    """Invalid profile or unresolvable adapter."""


def resolve_profile(cli: str | None = None, env: Mapping[str, str] | None = None) -> Profile:
    """Determine the profile: ``cli`` > env ``FABRIKANT_PROFILE`` > default.

    Empty values count as unset. Invalid → ``ProfileError`` with the allowed values."""
    env = os.environ if env is None else env
    raw = (cli or "").strip() or env.get(PROFILE_ENV, "").strip()
    if not raw:
        return DEFAULT_PROFILE
    try:
        return Profile(raw.lower())
    except ValueError:
        allowed = ", ".join(p.value for p in Profile)
        raise ProfileError(t("profiles.invalid_profile_allowed", raw=raw, allowed=allowed)) from None


def _local_storage() -> Any:
    """Homelab storage: the existing local run store (ADR 0034).

    ``runstore.STORE`` is exactly the instance through which engine, server and CLI read and write;
    at call time it delegates to the local backend chosen via ``FABRIKANT_RUN_STORE``
    (JSON files or SQLite)."""
    return runstore.STORE


def _cloud_storage() -> NoReturn:
    raise NotImplementedError(t("profiles.cloud_storage_postgres_is_not"))


Factory = Callable[[], Any]


class AdapterRegistry:
    """Maps ``(profile, port)`` to an adapter factory."""

    def __init__(self) -> None:
        self._factories: dict[tuple[Profile, Port], Factory] = {}

    def register(self, profile: Profile, port: Port, factory: Factory) -> None:
        """Register an adapter factory (double registration is an error)."""
        key = (profile, port)
        if key in self._factories:
            raise ProfileError(t("profiles.adapter_for_already_registered", v1=profile.value, v2=port.value))
        self._factories[key] = factory

    def resolve(self, profile: Profile, port: Port | str) -> Any:
        """Build the adapter for ``profile``/``port``. Unknown port/missing adapter → ``ProfileError``."""
        try:
            factory = self._factories[(profile, Port(port))]
        except (ValueError, KeyError):
            raise ProfileError(t("profiles.no_adapter_for_port_in", v1=str(port), v2=profile.value)) from None
        return factory()


def _build_default_registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(Profile.HOMELAB, Port.STORAGE, _local_storage)
    reg.register(Profile.CLOUD, Port.STORAGE, _cloud_storage)
    return reg


DEFAULT_REGISTRY = _build_default_registry()


@dataclass(frozen=True)
class Adapters:
    """Resolved adapters of a profile — built in the composition root and passed on.

    ``storage`` offers the operations of :class:`fabrikant.runstore.BaseRunStore`."""

    storage: Any


def compose(profile: Profile, registry: AdapterRegistry | None = None) -> Adapters:
    """Composition root: resolve all ports once for ``profile``."""
    reg = registry or DEFAULT_REGISTRY
    return Adapters(storage=reg.resolve(profile, Port.STORAGE))
