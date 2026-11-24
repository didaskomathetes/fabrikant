"""Import side effect, first in the package: adopt legacy `FABRIK_*` env vars before anything reads
the environment (ADR 0070). Must stay the first import of `fabrikant/__init__.py`."""

from . import legacy

legacy.adopt_legacy_env()
