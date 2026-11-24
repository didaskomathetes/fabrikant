#!/usr/bin/env python3
"""Generiert docs/spec-reference.md aus src/fabrikant/fabrikant-spec.schema.json (Issue #131).

Das Schema ist die Single Source of Truth für das Spec-Format; die Referenz
wird daraus abgeleitet, damit Doku und Validierung nicht auseinanderlaufen:

    python scripts/generate-spec-reference.py          # schreibt docs/spec-reference.md
    python scripts/generate-spec-reference.py --check  # Exit 1, wenn die Doku veraltet ist

Nur `description`-Felder des Schemas landen in der Doku — neue Felder ohne
Description werden als TODO markiert, damit Lücken sichtbar bleiben.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "src" / "fabrikant" / "fabrikant-spec.schema.json"
OUT_PATH = REPO_ROOT / "docs" / "spec-reference.md"

HEADER = """# Spec-Referenz

Maschinenlesbare Single Source of Truth: [`src/fabrikant/fabrikant-spec.schema.json`](../src/fabrikant/fabrikant-spec.schema.json).
Diese Seite wird daraus generiert (`scripts/generate-spec-reference.py`) — nicht von Hand editieren.

Das Schema prüft die **Struktur** (Typen, Enums, Pflichtfelder, Bereiche) der *expandierten* Spec
(nach `vars:`/Template-/Relay-Expansion). Semantische Prüfungen (Backend-Registry,
Cron-vs-Retry-Intervalle, Alert-Route-Zyklen, Template-Parameter) laufen zusätzlich in
`src/fabrikant/specs.py`. `fabrikant validate` beides.

Legende: **Pflicht** = required, *kursiv* = optional. Typen in ``Code``.
"""


def _ref_name(sub: dict) -> str | None:
    ref = sub.get("$ref", "")
    return ref.rsplit("/", 1)[-1] if ref else None


def _type_of(sub: dict) -> str:
    """Kompakte Typ-Darstellung eines Sub-Schemas."""
    ref = _ref_name(sub)
    if ref:
        return f"→ `{ref}`"
    if "const" in sub:
        return f"= `{sub['const']}`"
    if "enum" in sub:
        return " \\| ".join(f"`{v}`" for v in sub["enum"])
    t = sub.get("type")
    if t == "object":
        return "object"
    if t == "array":
        items = sub.get("items")
        inner = _type_of(items) if isinstance(items, dict) else ""
        return f"list{f' of {inner}' if inner else ''}"
    if t is None and "oneOf" in sub:
        parts = [_type_of(s) for s in sub["oneOf"]]
        return " / ".join(p for p in parts if p) or "object"
    return str(t)


def _desc(sub: dict, defs: dict) -> str:
    d = sub.get("description", "")
    if not d and (ref := _ref_name(sub)):
        d = defs.get(ref, {}).get("description", "")
    return d.replace("|", "\\|") if d else "_TODO: Beschreibung fehlt im Schema._"


def _props_table(props: dict, defs: dict, *, required: set[str] | None = None) -> list[str]:
    lines = []
    for name, sub in props.items():
        req = "Pflicht" if (required and name in required) else "optional"
        lines.append(f"| `{name}` | {_type_of(sub)} | {req} | {_desc(sub, defs)} |")
    return lines


def _section(title: str, sub: dict, defs: dict, *, intro: str | None = None) -> list[str]:
    lines = [f"## {title}", ""]
    if intro:
        lines += [intro, ""]
    elif sub.get("description"):
        lines += [sub["description"], ""]
    props = sub.get("properties", {})
    if props:
        lines += ["| Feld | Typ | Status | Beschreibung |", "|---|---|---|---|"]
        lines += _props_table(props, defs, required=set(sub.get("required", [])))
        lines.append("")
    return lines


def generate() -> str:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    out: list[str] = [HEADER]

    out += _section(
        "Top-Level",
        schema,
        defs,
        intro="Eine Projekt-Spec (`*.fabrikant.yaml` / `*.fabrikant.json`) definiert Loops, die auf dem "
        "Projekt-`root` laufen. `loops` ist das einzige Pflichtfeld; alle anderen Felder sind optional.",
    )

    for name, title in (
        ("loop", "Loop"),
        ("step", "Step"),
        ("trigger", "Trigger"),
        ("retry", "Retry-Block"),
        ("context_source", "extra_context-Eintrag"),
        ("relay", "Relay-Block"),
    ):
        out += _section(title, defs[name], defs)

    # allOf-Regeln (Pflichtfelder je Step-/Trigger-Typ) als Tabelle
    step_rules = [
        (r["if"]["properties"]["type"]["const"], r["then"].get("required", []))
        for r in defs["step"].get("allOf", [])
        if "required" in r.get("then", {})
    ]
    if step_rules:
        out += ["## Step-Typen: Pflichtfelder", "", "| Typ | Pflichtfelder |", "|---|---|"]
        for t, req in step_rules:
            out.append(f"| `{t}` | {', '.join(f'`{r}`' for r in req) or '—'} |")
        out.append("")
    trig_rules = [
        (r["if"]["properties"]["type"]["const"], r["then"].get("required", []))
        for r in defs["trigger"].get("allOf", [])
        if "required" in r.get("then", {})
    ]
    if trig_rules:
        out += ["## Trigger-Typen: Pflichtfelder", "", "| Typ | Pflichtfelder |", "|---|---|"]
        for t, req in trig_rules:
            out.append(f"| `{t}` | {', '.join(f'`{r}`' for r in req) or '—'} |")
        out.append("")

    out.append(
        "_Generiert aus `src/fabrikant/fabrikant-spec.schema.json` — Änderungen bitte am Schema vornehmen "
        "und dieses Skript neu laufen lassen._"
    )
    return "\n".join(out) + "\n"


def main() -> int:
    text = generate()
    if "--check" in sys.argv:
        if OUT_PATH.is_file() and OUT_PATH.read_text(encoding="utf-8") == text:
            print(f"OK: {OUT_PATH.relative_to(REPO_ROOT)} ist aktuell")
            return 0
        print(f"VERALTET: {OUT_PATH.relative_to(REPO_ROOT)} weicht vom Schema ab (ohne --check neu generieren)")
        return 1
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"geschrieben: {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
