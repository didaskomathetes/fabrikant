"""JSON-Schema-Validierung der Spec-Dateien (Issue #131).

Das Schema (`fabrikant/fabrikant-spec.schema.json`, im Paket mitgeliefert) ist die maschinenlesbare
Single Source of Truth für das Spec-Format: Validierung (dieses Modul) und
generierte Referenz (`docs/spec-reference.md` via
`scripts/generate-spec-reference.py`) leiten sich beide davon ab.

Das Schema prüft die STRUKTUR (Typen, Enums, Pflichtfelder, Bereiche) der
EXPANDIERTEN Spec (nach vars/Template/Relay-Expansion). Semantische Prüfungen
(Backend-Registry, Cron-vs-Retry-Intervalle, Alert-Route-Zyklen,
Template-Parameter, reservierte Vars) bleiben in `specs._validate_spec` —
das Schema darf nichts ablehnen, was die Python-Validierung akzeptiert
(Parität, keine Regressionen).
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

# Liegt im Paket (nicht im Repo-Root), damit es in Wheel-/Nicht-Editable-Installs enthalten ist.
SCHEMA_PATH = Path(__file__).resolve().parent / "fabrikant-spec.schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """Das Spec-Schema laden (einmal pro Prozess, danach gecacht)."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    validator = Draft202012Validator(load_schema())
    # Einmalige Meta-Validierung: ein defektes Schema ist ein Programmierfehler.
    Draft202012Validator.check_schema(validator.schema)
    return validator


def validate_spec(spec: dict) -> list[str]:
    """Spec gegen das JSON-Schema prüfen; liefert eine Liste von Fehlermeldungen.

    Leere Liste = gültig. Pfade sind JSON-Pointer-artig (`loops/develop/steps/0`),
    damit die Meldung direkt in `Spec ungültig:`-Fehlerlisten passt. Interne
    Buchführungsschlüssel (`_path`, …) werden vorher herausgefiltert — das
    Schema beschreibt das Nutzer-Format, nicht die Laufzeit-Buchführung."""
    public = {k: v for k, v in spec.items() if not k.startswith("_")}
    errors = [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(_validator().iter_errors(public), key=lambda e: list(e.absolute_path))
    ]
    return errors


# Schema keywords whose sub-schemas are conditions or alternatives, not the object's own key list:
# making them strict would change which branch matches instead of reporting a key.
_CONDITIONAL = frozenset({"if", "then", "else", "not", "oneOf", "anyOf"})


def _strict(node: object, parent: str = "") -> object:
    """Copy of a schema node in which every object with `properties` rejects other keys."""
    if isinstance(node, list):
        return [_strict(n, parent) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: v if k in _CONDITIONAL else _strict(v, k) for k, v in node.items()}
    if "properties" in node and "additionalProperties" not in node and parent not in _CONDITIONAL:
        out["additionalProperties"] = False
    return out


@lru_cache(maxsize=1)
def _strict_validator() -> Draft202012Validator:
    strict = _strict(copy.deepcopy(load_schema()))
    assert isinstance(strict, dict)
    return Draft202012Validator(strict)


def _plugin_keys(instance: object) -> set[str]:
    """Keys a registered plugin step declares in its own schema (ADR 0069)."""
    from .steps import registry

    stype = instance.get("type") if isinstance(instance, dict) else None
    plugin = registry.get_step(stype) if isinstance(stype, str) else None
    return set(((plugin.schema if plugin else None) or {}).get("properties") or {})


def unknown_keys(spec: dict) -> list[str]:
    """Keys the schema does not know, as `path: key` messages (empty = none).

    Only a warning, never an error: the engine ignores unknown keys, so a typo or a key from a newer
    fabrikant version silently has no effect. `x-` keys (YAML anchors) are the documented escape hatch."""
    public = {k: v for k, v in spec.items() if not k.startswith("_")}
    found: list[str] = []
    for err in _strict_validator().iter_errors(public):
        if err.validator != "additionalProperties" or not isinstance(err.instance, dict):
            continue
        known = set(err.schema.get("properties") or {}) | _plugin_keys(err.instance)
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        # PyYAML (YAML 1.1) reads an unquoted `on` key as bool True; retry.py maps it back, so it is known.
        names = ("on" if k is True else str(k) for k in err.instance)
        found += [f"{path}: {k}" for k in names if k not in known and not k.startswith(("x-", "_"))]
    return sorted(found)
