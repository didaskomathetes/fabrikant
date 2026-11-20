"""JSON-Schema-Validierung der Spec-Dateien (Issue #131).

Das Schema (`schema/fabrik-spec.schema.json`) ist die maschinenlesbare
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

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

# src/fabrik/specschema.py → 3 Ebenen hoch = Repo-Root (wie config.FABRIK_HOME)
SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "fabrik-spec.schema.json"


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
