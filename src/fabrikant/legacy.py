"""Compatibility with the pre-rename `fabrik` names (ADR 0070): read the old, prefer the new.

Covers environment variables (`FABRIK_*`), the env file (`fabrik.env`), spec files
(`*.fabrik.yaml`, `.fabrik/fabrik.yaml`), the state database (`state/fabrik.db`) and webhook
headers (`X-Fabrik-*`). New names always win when both exist; using an old one logs a warning once.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from pathlib import Path

LEGACY_ENV_PREFIX = "FABRIK_"
ENV_PREFIX = "FABRIKANT_"
LEGACY_ENV_FILE = "fabrik.env"
LEGACY_SPEC_GLOBS = ("*.fabrik.yaml", "*.fabrik.json")
LEGACY_TARGET_SPEC_RELPATH = Path(".fabrik") / "fabrik.yaml"
LEGACY_SPEC_SUFFIX = ".fabrik"
LEGACY_DB_FILENAME = "fabrik.db"
LEGACY_HEADER_PREFIX = "X-Fabrik-"
HEADER_PREFIX = "X-Fabrikant-"

_log = logging.getLogger("fabrikant.legacy")
_warned: set[str] = set()


def warn_once(category: str, detail: str) -> None:
    """Log one deprecation warning per `category` (env, env file, db, header, …) and process."""
    if category not in _warned:
        _warned.add(category)
        _log.warning("deprecated pre-rename name in use: %s %s (see docs/migration-fabrikant.md)", category, detail)


def adopt_legacy_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy every `FABRIK_X` to `FABRIKANT_X` unless the new variable is set; returns the adopted names."""
    env = os.environ if environ is None else environ
    adopted = []
    for key in sorted(k for k in env if k.startswith(LEGACY_ENV_PREFIX)):
        new = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX) :]
        if new not in env:
            env[new] = env[key]
            adopted.append(key)
    if adopted:
        warn_once("environment variable(s)", ", ".join(adopted))
    return adopted


def existing_env_file(directory: Path, new_name: str) -> Path:
    """`directory/new_name`, or the legacy `fabrik.env` if only that one exists."""
    new = directory / new_name
    old = directory / LEGACY_ENV_FILE
    if not new.is_file() and old.is_file():
        warn_once("env file", str(old))
        return old
    return new


def state_db_filename(state_dir: Path, new_name: str) -> str:
    """New DB filename, or the legacy one when only the old file exists (keeps existing run history)."""
    if not (state_dir / new_name).exists() and (state_dir / LEGACY_DB_FILENAME).exists():
        warn_once("state database", str(state_dir / LEGACY_DB_FILENAME))
        return LEGACY_DB_FILENAME
    return new_name


def legacy_header(new_header: str) -> str:
    """`X-Fabrikant-Foo` -> `X-Fabrik-Foo` (the pre-rename webhook header)."""
    return LEGACY_HEADER_PREFIX + new_header.removeprefix(HEADER_PREFIX)
