"""Context helpers: output cap, variable substitution, untrusted blocks, risk-tier policy."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping

from . import config
from .i18n import t as _t

# Placeholder syntax: {name} with a real identifier (first letter/_,
# then letters/digits/_). Shared by _substitute AND the shell guard
# (steps/shell.py) — both must recognize exactly the same syntax so the
# guard misses no real placeholders (no false negative) and does not wrongly
# trigger on literal braces like {123} (no false positive).
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _cap(s: str | None, limit: int = config.MAX_STEP_OUTPUT) -> str:
    """Truncates to ``limit`` characters (with a marker if truncated)."""
    if not s:
        return ""
    return s[:limit] + (_t("context.truncated") if len(s) > limit else "")


def _substitute(text: str, ctx: Mapping[str, object]) -> str:
    def repl(m):
        return str(ctx.get(m.group(1), m.group(0)))

    return _PLACEHOLDER_RE.sub(repl, text)


def missing_vars(text: str, ctx: Mapping[str, object]) -> list[str]:
    """Placeholders in `text` for which `ctx` has no value (sorted, without duplicates)."""
    return sorted({name for name in _PLACEHOLDER_RE.findall(text) if name not in ctx})


# Prompt tags that separate data from instructions (`<untrusted>`, `<context>`). Neutralized
# in the content so text cannot close a block or fake a "trusted" block.
_PROMPT_TAG_RE = re.compile(r"<(?=\s*/?\s*(?:untrusted|context)\b)", re.IGNORECASE)


def neutralize_prompt_tags(text: str) -> str:
    """Replaces ``<`` before ``untrusted``/``context`` tags (opening and closing) with ``&lt;``."""
    return _PROMPT_TAG_RE.sub("&lt;", text)


def prompt_block(tag: str, name: str, text: str) -> str:
    """``<tag name="…">text</tag>`` — ``name`` escaped, tag break-outs in the text neutralized."""
    return f'<{tag} name="{html.escape(name, quote=True)}">\n{neutralize_prompt_tags(text)}\n</{tag}>'


def untrusted_blocks(items: list[tuple[str, str]]) -> str:
    """External content as `<untrusted>` blocks including a warning notice (guiding principle 7); empty without items."""
    if not items:
        return ""
    blocks = "\n".join(prompt_block("untrusted", name, text) for name, text in items)
    return f"\n\n{_t('context.untrusted_notice')}\n{blocks}"


def step_workdir(ctx: Mapping[str, object], step: Mapping[str, object]) -> str:
    """Working directory of a step: ``step.workdir`` (template) overrides ``ctx['workdir']``."""
    return _substitute(str(step.get("workdir") or ctx.get("workdir") or "."), ctx)


def _cond_matches(cond: dict, ctx: dict) -> bool:
    """`{var: value | [values]}`: EVERY variable (AND) has one of the values (OR).
    Missing variable → no match."""
    for var, allowed in cond.items():
        if var not in ctx:
            return False
        values = allowed if isinstance(allowed, list) else [allowed]
        if str(ctx[var]) not in [str(v) for v in values]:
            return False
    return True


def tier_policy(tier) -> dict:
    """Tier → behavior — one function (P1/P2 extend the substrate).
    P0: T1 = auto after checks, T2+ = approval at the end (irreversible/accountability)."""
    t = int(tier) if tier is not None else 1
    return {"tier": t, "requires_approval": t >= 2}
