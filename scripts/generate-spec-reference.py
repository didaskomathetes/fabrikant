#!/usr/bin/env python3
"""Generate docs/spec-reference.md from src/fabrikant/fabrikant-spec.schema.json (issue #131).

The schema is the single source of truth for the spec format; the reference
is derived from it so that docs and validation do not drift apart:

    python scripts/generate-spec-reference.py          # writes docs/spec-reference.md
    python scripts/generate-spec-reference.py --check  # exit 1 if the docs are stale

Only `description` fields of the schema end up in the docs — new fields without a
description are marked as TODO so that gaps stay visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "src" / "fabrikant" / "fabrikant-spec.schema.json"
OUT_PATH = REPO_ROOT / "docs" / "spec-reference.md"

HEADER = """# Spec reference

Machine-readable single source of truth: [`src/fabrikant/fabrikant-spec.schema.json`](../src/fabrikant/fabrikant-spec.schema.json).
This page is generated from it (`scripts/generate-spec-reference.py`) — do not edit by hand.

The schema checks the **structure** (types, enums, required fields, ranges) of the *expanded* spec
(after `vars:`/template/relay expansion). Semantic checks (backend registry,
cron-vs-retry intervals, alert route cycles, template parameters) additionally run in
`src/fabrikant/specs.py`. `fabrikant validate` runs both and warns about keys the schema doesn't know
(a typo or a key from a newer fabrikant version would otherwise silently have no effect).

Legend: **required** = mandatory, *italic* = optional. Types in ``code``.
"""


def _ref_name(sub: dict) -> str | None:
    ref = sub.get("$ref", "")
    return ref.rsplit("/", 1)[-1] if ref else None


def _type_of(sub: dict) -> str:
    """Compact type representation of a sub-schema."""
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
    return d.replace("|", "\\|") if d else "_TODO: description missing in the schema._"


def _props_table(props: dict, defs: dict, *, required: set[str] | None = None) -> list[str]:
    lines = []
    for name, sub in props.items():
        req = "required" if (required and name in required) else "optional"
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
        lines += ["| Field | Type | Status | Description |", "|---|---|---|---|"]
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
        intro="A project spec (`*.fabrikant.yaml` / `*.fabrikant.json`) defines loops that run on the "
        "project `root`. `loops` is the only required field; all other fields are optional.",
    )

    for name, title in (
        ("loop", "Loop"),
        ("step", "Step"),
        ("trigger", "Trigger"),
        ("retry", "Retry block"),
        ("context_source", "extra_context entry"),
        ("relay", "Relay block"),
    ):
        out += _section(title, defs[name], defs)

    # allOf rules (required fields per step/trigger type) as a table
    step_rules = [
        (r["if"]["properties"]["type"]["const"], r["then"].get("required", []))
        for r in defs["step"].get("allOf", [])
        if "required" in r.get("then", {})
    ]
    if step_rules:
        out += ["## Step types: required fields", "", "| Type | Required fields |", "|---|---|"]
        for t, req in step_rules:
            out.append(f"| `{t}` | {', '.join(f'`{r}`' for r in req) or '—'} |")
        out.append("")
    trig_rules = [
        (r["if"]["properties"]["type"]["const"], r["then"].get("required", []))
        for r in defs["trigger"].get("allOf", [])
        if "required" in r.get("then", {})
    ]
    if trig_rules:
        out += ["## Trigger types: required fields", "", "| Type | Required fields |", "|---|---|"]
        for t, req in trig_rules:
            out.append(f"| `{t}` | {', '.join(f'`{r}`' for r in req) or '—'} |")
        out.append("")

    out.append(
        "_Generated from `src/fabrikant/fabrikant-spec.schema.json` — please make changes in the schema "
        "and re-run this script._"
    )
    return "\n".join(out) + "\n"


def main() -> int:
    text = generate()
    if "--check" in sys.argv:
        if OUT_PATH.is_file() and OUT_PATH.read_text(encoding="utf-8") == text:
            print(f"OK: {OUT_PATH.relative_to(REPO_ROOT)} is up to date")
            return 0
        print(f"STALE: {OUT_PATH.relative_to(REPO_ROOT)} differs from the schema (regenerate without --check)")
        return 1
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"written: {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
