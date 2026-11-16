"""Gemeinsame Test-Helfer (kein Fixture-Modul: direkt importieren, `from helpers import …`)."""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import TypeVar

T = TypeVar("T")

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_script(filename: str, name: str) -> ModuleType:
    """Skript aus ``scripts/`` als Modul ``name`` laden (Dateinamen mit Bindestrich sind nicht importierbar).

    Registriert das Modul vor dem Ausführen in ``sys.modules`` — dataclasses lösen Annotationen darüber auf.
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None, filename
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def not_none(value: T | None) -> T:
    """Optionales Ergebnis, das der Test als vorhanden erwartet (assert + Typ-Narrowing in einem)."""
    assert value is not None
    return value


def http_error(
    code: int,
    url: str = "https://x",
    reason: str = "err",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> urllib.error.HTTPError:
    """``HTTPError`` wie von ``urlopen``: Header als ``Message``, ``body`` lesbar über ``.read()``."""
    msg = Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError(url, code, reason, msg, io.BytesIO(body))
