"""Spec expansion: loop templates (`use:`) and variables (`vars:`) — ADR 0047.

Order in `expand_spec`: first templates, then `vars` over the whole spec.
Only keys from `vars`/`with` are replaced; all other `{name}` stay
runtime variables of the engine (`{issue_number}`, `{root}`, …).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .i18n import t

try:
    import yaml
except ImportError:
    yaml = None

from . import config
from .context import _PLACEHOLDER_RE
from .slack import ALERT_VARS

# Runtime variables set by the engine: a `vars` key of the same name would shadow them.
RESERVED_VARS = frozenset(
    {
        "project",
        "root",
        "workdir",
        "loop",
        "trigger",
        "note",
        "run_id",
        "log",
        "spec_path",
        "issue_number",
        "issue_title",
        "issue_body",
        "labels",
        "pr_number",
        "pr_title",
        "pr_body",
        "pr_head_ref",
        "pr_head_sha",
        "branch",
        "head",
        *config.DEDUPE_VARS,
        *ALERT_VARS,
    }
)
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_USE_KEYS = ("use", "with", "insert")


def substitute(obj: Any, mapping: dict) -> Any:
    """Replaces `{key}` (only keys from `mapping`) recursively in all strings.

    If a string consists of a single placeholder only, the value is inserted type-preserved
    (list, number — as a copy, so loops do not share containers); otherwise as text."""
    if isinstance(obj, str):
        m = _PLACEHOLDER_RE.fullmatch(obj)
        if m and m.group(1) in mapping:
            return copy.deepcopy(mapping[m.group(1)])
        return _PLACEHOLDER_RE.sub(lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0), obj)
    if isinstance(obj, list):
        return [substitute(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, mapping) for k, v in obj.items()}
    return obj


def placeholders(obj) -> set[str]:
    """All `{name}` placeholders in the strings of `obj` (recursively)."""
    if isinstance(obj, str):
        return set(_PLACEHOLDER_RE.findall(obj))
    if isinstance(obj, list):
        return set().union(*map(placeholders, obj))
    if isinstance(obj, dict):
        return set().union(*map(placeholders, obj.values()))
    return set()


def resolve_vars(spec_vars: dict) -> dict:
    """Resolve `vars` that reference other `vars` (`a: "{b}"`); cycles → ValueError."""
    resolved: dict = {}

    def resolve(key: str, chain: tuple[str, ...]):
        if key in resolved:
            return resolved[key]
        if key in chain:
            raise ValueError(f"vars: zyklische Referenz {' → '.join((*chain, key))}")
        value = spec_vars[key]
        refs = placeholders(value) & spec_vars.keys()
        resolved[key] = substitute(value, {r: resolve(r, (*chain, key)) for r in refs})
        return resolved[key]

    for k in spec_vars:
        resolve(k, ())
    return resolved


def _check_vars(spec_vars) -> list[str]:
    if spec_vars is None:
        return []
    if not isinstance(spec_vars, dict):
        return [t("spec_expand.vars_must_be_a_mapping")]
    errs = []
    for k in spec_vars:
        if not isinstance(k, str) or not _PLACEHOLDER_RE.fullmatch("{" + k + "}"):
            errs.append(t("spec_expand.vars_key_is_not_an", k=k))
        elif k in RESERVED_VARS:
            errs.append(t("spec_expand.vars_is_a_reserved_runtime", k=k))
    return errs


def find_template(name: str, spec_path: Path | None) -> Path | None:
    """Template-Datei suchen: `<spec-ordner>/templates`, `FABRIK_DATA/templates`, mitgelieferte."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return None
    dirs = [config.USER_TEMPLATES_DIR, config.TEMPLATES_DIR]
    if spec_path:
        dirs.insert(0, Path(spec_path).parent / "templates")
    for d in dirs:
        f = d / f"{name}{config.TEMPLATE_SUFFIX}"
        if f.is_file():
            return f
    return None


def _load_template(name: str, spec_path: Path | None) -> dict:
    f = find_template(name, spec_path)
    if f is None:
        raise ValueError(t("spec_expand.template_not_found_name_letters", name=name))
    data = yaml.safe_load(f.read_text()) if yaml else None
    if not isinstance(data, dict) or not isinstance(data.get("loop"), dict):
        raise ValueError(t("spec_expand.template_mapping_with_loop_expected", name=name, f=f))
    if not isinstance(data.get("params") or {}, dict):
        raise ValueError(t("spec_expand.template_params_must_be_a", name=name, f=f))
    return data


def _insert_steps(steps: list, insert, where: str) -> list:
    """`insert: [{after: <step>, steps: [...]}]` — Zusatz-Steps hinter benannte Template-Steps."""
    if not isinstance(insert, list):
        raise ValueError(t("spec_expand.insert_must_be_a_list", where=where))
    steps = list(steps)
    for item in insert:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("after"), str)
            or not isinstance(item.get("steps"), list)
        ):
            raise ValueError(t("spec_expand.insert_expects", where=where))
        names = [s.get("name") if isinstance(s, dict) else None for s in steps]
        if item.get("after") not in names:
            raise ValueError(t("spec_expand.insert_after_is_not_a", where=where, v2=item.get("after")))
        i = names.index(item["after"]) + 1
        steps[i:i] = item["steps"]
    return steps


def expand_loop(lname: str, ldef: dict, spec_vars: dict, spec_path: Path | None) -> dict:
    """A `use:` loop → concrete loop (template defaults < vars < with; loop keys override)."""
    where = f"loop {lname}"
    tpl = _load_template(ldef["use"], spec_path)
    params = tpl.get("params") or {}
    given = ldef.get("with") or {}
    if not isinstance(given, dict):
        raise ValueError(t("spec_expand.with_must_be_a_mapping", where=where))
    if "insert" in ldef and "steps" in ldef:
        raise ValueError(t("spec_expand.insert_cannot_be_combined_with", where=where))
    # Unknown with keys (typos) otherwise: silent default or literal {x} at runtime.
    unknown = sorted(set(given) - set(params) - placeholders(tpl["loop"]) - placeholders(ldef.get("insert")))
    if unknown:
        raise ValueError(
            t("spec_expand.template_does_not_know_with", where=where, v2=ldef["use"], v3=", ".join(unknown))
        )
    given = substitute(given, spec_vars)
    mapping = {k: v for k, v in params.items() if v is not None}
    mapping.update(spec_vars)
    mapping.update(given)
    missing = sorted(k for k, v in params.items() if v is None and k not in mapping)
    if missing:
        raise ValueError(t("spec_expand.template_needs_parameters", where=where, v2=ldef["use"], v3=", ".join(missing)))
    loop = substitute(tpl["loop"], mapping)
    if "insert" in ldef:
        loop["steps"] = _insert_steps(loop.get("steps") or [], substitute(ldef["insert"], mapping), where)
    loop.update({k: v for k, v in ldef.items() if k not in _USE_KEYS})
    return loop


def expand_spec(data: dict, spec_path: Path | None = None) -> dict:
    """Expand templates, then insert `vars` everywhere. Returns a new spec."""
    errs = _check_vars(data.get("vars"))
    if errs:
        raise ValueError(t("spec_expand.spec_invalid") + "\n  ".join(errs))
    spec_vars = resolve_vars(data.get("vars") or {})
    loops = data.get("loops")
    out = dict(data)
    if isinstance(loops, dict):
        out["loops"] = {
            ln: expand_loop(ln, ld, spec_vars, spec_path) if isinstance(ld, dict) and "use" in ld else ld
            for ln, ld in loops.items()
        }
    if spec_vars:
        out = {k: (v if k == "vars" else substitute(v, spec_vars)) for k, v in out.items()}
    return out
