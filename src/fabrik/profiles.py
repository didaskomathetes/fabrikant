"""Deployment-Profil (`homelab|cloud`) und Adapter-Registry (ADR 0056).

Das Profil wird einmal beim Start aufgelöst (CLI > ``FABRIK_PROFILE`` > Default)
und wählt pro Port die konkrete Implementierung. Nur Config und Composition Root
(`cli.py`) kennen den Profilnamen; Business-Logik bekommt fertige Adapter
(Guard: ``tests/test_profiles.py``).

Neuer Port: ``Port``-Member + Adapter pro Profil in ``_build_default_registry``
registrieren — die Auswahl-Logik bleibt unverändert.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from . import runstore

PROFILE_ENV = "FABRIK_PROFILE"


class Profile(StrEnum):
    """Deployment-Profil."""

    HOMELAB = "homelab"
    CLOUD = "cloud"


class Port(StrEnum):
    """Austauschbare Infrastruktur-Schnittstelle (später z. B. Secrets, Queue, Worker)."""

    STORAGE = "storage"


DEFAULT_PROFILE = Profile.HOMELAB


class ProfileError(ValueError):
    """Ungültiges Profil oder nicht auflösbarer Adapter."""


def resolve_profile(cli: str | None = None, env: Mapping[str, str] | None = None) -> Profile:
    """Profil bestimmen: ``cli`` > Env ``FABRIK_PROFILE`` > Default.

    Leere Werte zählen als nicht gesetzt. Ungültig → ``ProfileError`` mit den erlaubten Werten."""
    env = os.environ if env is None else env
    raw = (cli or "").strip() or env.get(PROFILE_ENV, "").strip()
    if not raw:
        return DEFAULT_PROFILE
    try:
        return Profile(raw.lower())
    except ValueError:
        allowed = ", ".join(p.value for p in Profile)
        raise ProfileError(f"Ungültiges Profil {raw!r} (erlaubt: {allowed})") from None


def _local_storage() -> Any:
    """Homelab-Storage: der bestehende lokale Run-Store (ADR 0034).

    ``runstore.STORE`` ist genau die Instanz, über die Engine, Server und CLI lesen und schreiben;
    sie delegiert zur Aufrufzeit an das per ``FABRIK_RUN_STORE`` gewählte lokale Backend
    (JSON-Dateien oder SQLite)."""
    return runstore.STORE


def _cloud_storage() -> NoReturn:
    raise NotImplementedError("Cloud-Storage (Postgres) ist noch nicht implementiert — siehe Folge-Issues (#19, #28)")


Factory = Callable[[], Any]


class AdapterRegistry:
    """Bildet ``(Profil, Port)`` auf eine Adapter-Factory ab."""

    def __init__(self) -> None:
        self._factories: dict[tuple[Profile, Port], Factory] = {}

    def register(self, profile: Profile, port: Port, factory: Factory) -> None:
        """Adapter-Factory registrieren (Doppelregistrierung ist ein Fehler)."""
        key = (profile, port)
        if key in self._factories:
            raise ProfileError(f"Adapter für {profile.value}/{port.value} bereits registriert")
        self._factories[key] = factory

    def resolve(self, profile: Profile, port: Port | str) -> Any:
        """Adapter für ``profile``/``port`` bauen. Unbekannter Port/fehlender Adapter → ``ProfileError``."""
        try:
            factory = self._factories[(profile, Port(port))]
        except (ValueError, KeyError):
            raise ProfileError(f"Kein Adapter für Port {str(port)!r} im Profil {profile.value!r}") from None
        return factory()


def _build_default_registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register(Profile.HOMELAB, Port.STORAGE, _local_storage)
    reg.register(Profile.CLOUD, Port.STORAGE, _cloud_storage)
    return reg


DEFAULT_REGISTRY = _build_default_registry()


@dataclass(frozen=True)
class Adapters:
    """Aufgelöste Adapter eines Profils — wird im Composition Root gebaut und weitergereicht.

    ``storage`` bietet die Operationen von :class:`fabrik.runstore.BaseRunStore`."""

    storage: Any


def compose(profile: Profile, registry: AdapterRegistry | None = None) -> Adapters:
    """Composition Root: alle Ports einmalig für ``profile`` auflösen."""
    reg = registry or DEFAULT_REGISTRY
    return Adapters(storage=reg.resolve(profile, Port.STORAGE))
