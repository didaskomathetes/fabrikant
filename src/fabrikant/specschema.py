"""JSON-schema validation of the spec files (issue #131).

The schema (`fabrikant/fabrikant-spec.schema.json`, shipped in the package) is the machine-readable
single source of truth for the spec format: validation (this module) and the
generated reference (`docs/spec-reference.md` via
`scripts/generate-spec-reference.py`) both derive from it.

The schema checks the STRUCTURE (types, enums, required fields, ranges) of the
EXPANDED spec (after vars/template/relay expansion). Semantic checks
(backend registry, cron-vs-retry intervals, alert-route cycles,
template parameters, reserved vars) stay in `specs._validate_spec` —
the schema must not reject anything the Python validation accepts
(parity, no regressions).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

# Lives in the package (not the repo root) so it is included in wheel/non-editable installs.
SCHEMA_PATH = Path(__file__).resolve().parent / "fabrikant-spec.schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """Load the spec schema (once per process, cached afterwards)."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    validator = Draft202012Validator(load_schema())
    # One-time meta-validation: a broken schema is a programming error.
    Draft202012Validator.check_schema(validator.schema)
    return validator


def validate_spec(spec: dict) -> list[str]:
    """Check a spec against the JSON schema; returns a list of error messages.

    Empty list = valid. Paths are JSON-pointer-like (`loops/develop/steps/0`),
    so the message fits directly into the spec-invalid error lists. Internal
    bookkeeping keys (`_path`, …) are filtered out beforehand — the
    schema describes the user format, not the runtime bookkeeping."""
    public = {k: v for k, v in spec.items() if not k.startswith("_")}
    errors = [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(_validator().iter_errors(public), key=lambda e: list(e.absolute_path))
    ]
    return errors
