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

import copy
import json
import re
from collections.abc import Callable
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


# Schema keywords whose sub-schemas are conditions or alternatives, not the object's own key list:
# making them strict would change which branch matches instead of reporting a key.
_CONDITIONAL = frozenset({"if", "then", "else", "not", "oneOf", "anyOf"})


def _object_branch(branches: object) -> dict | None:
    """The one object-shaped branch of a oneOf/anyOf whose branches all declare a `type`, else None.

    Only then is the choice unambiguous: a mapping can match no other branch, so checking its keys
    against that branch cannot change which branch matches (`slack: <url> | {url: ...}`)."""
    if not isinstance(branches, list) or not all(isinstance(b, dict) and "type" in b for b in branches):
        return None
    objects = [b for b in branches if "object" in (b["type"] if isinstance(b["type"], list) else [b["type"]])]
    return objects[0] if len(objects) == 1 else None


def _strict(node: object, parent: str = "") -> object:
    """Copy of a schema node in which every object with `properties` rejects other keys.

    The alternatives themselves stay untouched; an unambiguous object branch (`_object_branch`) is
    additionally checked strictly via `if: {type: object}`, so its own keys are reported too."""
    if isinstance(node, list):
        return [_strict(n, parent) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: v if k in _CONDITIONAL else _strict(v, k) for k, v in node.items()}
    if "properties" in node and "additionalProperties" not in node and parent not in _CONDITIONAL:
        out["additionalProperties"] = False
    for keyword in ("oneOf", "anyOf"):
        branch = _object_branch(node.get(keyword))
        if branch is not None:
            all_of = out.get("allOf")
            out["allOf"] = [
                *(all_of if isinstance(all_of, list) else []),
                {"if": {"type": "object"}, "then": _strict(branch)},
            ]
    return out


@lru_cache(maxsize=1)
def _strict_validator() -> Draft202012Validator:
    strict = _strict(copy.deepcopy(load_schema()))
    assert isinstance(strict, dict)
    return Draft202012Validator(strict)


def _plugin_declares(instance: object) -> Callable[[str], bool] | None:
    """Whether a key is one a registered plugin step declares in its own schema (ADR 0069): a literal
    `properties` key or one matching a `patternProperties` pattern.

    None for a plugin whose keys are unknowable — no schema, no key list, a schema that admits any
    further key (`additionalProperties` other than false) or a pattern Python cannot compile: none of
    its keys is reported (the scaffolded `greet` reads `who` without declaring it)."""
    from .steps import registry

    stype = instance.get("type") if isinstance(instance, dict) else None
    plugin = registry.get_step(stype) if isinstance(stype, str) else None
    if plugin is None:
        return lambda key: False
    schema = plugin.schema or {}
    names = set(schema.get("properties") or {})
    if not (names or schema.get("patternProperties")) or schema.get("additionalProperties", False) is not False:
        return None
    try:
        patterns = [re.compile(p) for p in schema.get("patternProperties") or {}]
    except re.error:
        return None
    # JSON-Schema patterns are unanchored, hence search, not match.
    return lambda key: key in names or any(p.search(key) for p in patterns)


def unknown_keys(spec: dict) -> list[str]:
    """Keys the schema does not know, as `path: key` messages (empty = none).

    Only a warning, never an error: the engine ignores unknown keys, so a typo or a key from a newer
    fabrikant version silently has no effect. `x-` keys (YAML anchors) are the documented escape hatch."""
    public = {k: v for k, v in spec.items() if not k.startswith("_")}
    found: list[str] = []
    for err in _strict_validator().iter_errors(public):
        if err.validator != "additionalProperties" or not isinstance(err.instance, dict):
            continue
        plugin_declares = _plugin_declares(err.instance)
        if plugin_declares is None:
            continue
        known = set(err.schema.get("properties") or {})
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        # PyYAML (YAML 1.1) reads an unquoted `on` key as bool True; retry.py maps it back, so it is known.
        names = ("on" if k is True else str(k) for k in err.instance)
        found += [
            f"{path}: {k}" for k in names if k not in known and not k.startswith(("x-", "_")) and not plugin_declares(k)
        ]
    return sorted(found)
