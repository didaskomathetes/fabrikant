"""Deployment-Profil + Adapter-Registry (ADR 0056)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fabrik import cli, profiles, runstore
from fabrik.profiles import AdapterRegistry, Port, Profile, ProfileError

SRC = Path(profiles.__file__).parent
# Einzige Module, die Profilnamen kennen dürfen: Profil-Mechanismus + Composition Root.
ALLOWED = {"profiles.py", "cli.py"}


def test_default_is_homelab():
    assert profiles.resolve_profile(env={}) is Profile.HOMELAB


@pytest.mark.parametrize("value", ["homelab", "cloud", "CLOUD", " cloud "])
def test_valid_profiles_from_env(value):
    assert profiles.resolve_profile(env={profiles.PROFILE_ENV: value}) is Profile(value.strip().lower())


def test_empty_values_fall_back_to_default():
    assert profiles.resolve_profile("", env={profiles.PROFILE_ENV: " "}) is Profile.HOMELAB


def test_cli_beats_env():
    assert profiles.resolve_profile("cloud", env={profiles.PROFILE_ENV: "homelab"}) is Profile.CLOUD


def test_reads_os_environ(monkeypatch):
    monkeypatch.setenv(profiles.PROFILE_ENV, "cloud")
    assert profiles.resolve_profile() is Profile.CLOUD


def test_invalid_profile_names_allowed_values():
    with pytest.raises(ProfileError, match=r"'nope'.*homelab, cloud"):
        profiles.resolve_profile(env={profiles.PROFILE_ENV: "nope"})
    with pytest.raises(ProfileError):
        profiles.resolve_profile("nope", env={})


def test_homelab_storage_is_local_store():
    """Homelab löst auf genau die Store-Instanz auf, die die Business-Logik nutzt (ADR 0034)."""
    st = profiles.DEFAULT_REGISTRY.resolve(Profile.HOMELAB, Port.STORAGE)
    assert st is runstore.STORE
    assert isinstance(st.backend(), runstore.BaseRunStore)


def test_cloud_storage_placeholder_fails_clearly():
    with pytest.raises(NotImplementedError, match="Folge-Issues"):
        profiles.DEFAULT_REGISTRY.resolve(Profile.CLOUD, "storage")


def test_unknown_port_and_missing_adapter():
    reg = AdapterRegistry()
    with pytest.raises(ProfileError, match="'queue'"):
        reg.resolve(Profile.HOMELAB, "queue")
    with pytest.raises(ProfileError, match="storage"):
        reg.resolve(Profile.HOMELAB, Port.STORAGE)


def test_registry_is_extensible_and_rejects_duplicates():
    reg = AdapterRegistry()
    reg.register(Profile.CLOUD, Port.STORAGE, lambda: "pg")
    assert reg.resolve(Profile.CLOUD, Port.STORAGE) == "pg"
    with pytest.raises(ProfileError, match="bereits"):
        reg.register(Profile.CLOUD, Port.STORAGE, lambda: "other")


def test_compose_uses_given_registry():
    reg = AdapterRegistry()
    reg.register(Profile.HOMELAB, Port.STORAGE, lambda: "fake")
    assert profiles.compose(Profile.HOMELAB, reg).storage == "fake"
    assert profiles.compose(Profile.HOMELAB).storage is runstore.STORE


def test_cli_main_composes_adapters(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_list", lambda a: seen.setdefault("adapters", a.adapters))
    monkeypatch.delenv(profiles.PROFILE_ENV, raising=False)
    cli.main(["list"])
    assert seen["adapters"].storage is runstore.STORE


def test_cli_main_rejects_invalid_and_unimplemented_profile(monkeypatch, capsys):
    monkeypatch.delenv(profiles.PROFILE_ENV, raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["--profile", "nope", "list"])
    assert "homelab, cloud" in str(e.value)
    monkeypatch.setenv(profiles.PROFILE_ENV, "cloud")
    with pytest.raises(SystemExit) as e:
        cli.main(["list"])
    assert "nicht implementiert" in str(e.value)


def test_business_logic_does_not_know_profile_names():
    """Kein Profilname als String-Literal und kein Zugriff auf `Profile` außerhalb von Config/Composition Root."""
    names = {p.value for p in Profile}
    offenders = []
    for f in SRC.rglob("*.py"):
        if f.name in ALLOWED and f.parent == SRC:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() in names:
                offenders.append(f"{f.relative_to(SRC)}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id == "Profile":
                offenders.append(f"{f.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f"Profilname außerhalb Config/Composition Root: {offenders}"
