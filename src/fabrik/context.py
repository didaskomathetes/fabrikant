"""Kontext-Helfer: Output-Cap, Variable-Substitution, Untrusted-Blöcke, Risk-Tier-Policy."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping

from . import config
from .i18n import t as _t

# Platzhalter-Syntax: {name} mit echtem Identifikator (erster Buchstabe/_,
# danach Buchstaben/Ziffern/_). Geteilt von _substitute UND dem Shell-Guard
# (steps/shell.py) — beide müssen exakt dieselbe Syntax erkennen, damit der
# Guard keine echten Platzhalter verpasst (kein False-Negative) und keine
# Literal-Braces wie {123} fälschlich triggert (kein False-Positive).
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _cap(s: str | None, limit: int = config.MAX_STEP_OUTPUT) -> str:
    """Kürzt auf ``limit`` Zeichen (mit Marker, wenn gekürzt)."""
    if not s:
        return ""
    return s[:limit] + (_t("context.truncated") if len(s) > limit else "")


def _substitute(text: str, ctx: Mapping[str, object]) -> str:
    def repl(m):
        return str(ctx.get(m.group(1), m.group(0)))

    return _PLACEHOLDER_RE.sub(repl, text)


def missing_vars(text: str, ctx: Mapping[str, object]) -> list[str]:
    """Platzhalter in `text`, für die `ctx` keinen Wert hat (sortiert, ohne Duplikate)."""
    return sorted({name for name in _PLACEHOLDER_RE.findall(text) if name not in ctx})


# Prompt-Tags, die Daten von Anweisungen trennen (`<untrusted>`, `<context>`). Im Inhalt
# neutralisiert, damit Text einen Block nicht schließen und keinen „trusted“ Block vortäuschen kann.
_PROMPT_TAG_RE = re.compile(r"<(?=\s*/?\s*(?:untrusted|context)\b)", re.IGNORECASE)


def neutralize_prompt_tags(text: str) -> str:
    """Ersetzt ``<`` vor ``untrusted``/``context``-Tags (öffnend wie schließend) durch ``&lt;``."""
    return _PROMPT_TAG_RE.sub("&lt;", text)


def prompt_block(tag: str, name: str, text: str) -> str:
    """``<tag name="…">text</tag>`` — ``name`` escaped, Tag-Ausbrüche im Text neutralisiert."""
    return f'<{tag} name="{html.escape(name, quote=True)}">\n{neutralize_prompt_tags(text)}\n</{tag}>'


def untrusted_blocks(items: list[tuple[str, str]]) -> str:
    """Externe Inhalte als `<untrusted>`-Blöcke samt Warnhinweis (Leitprinzip 7); leer ohne Items."""
    if not items:
        return ""
    blocks = "\n".join(prompt_block("untrusted", name, text) for name, text in items)
    return f"\n\n{_t('context.untrusted_notice')}\n{blocks}"


def step_workdir(ctx: Mapping[str, object], step: Mapping[str, object]) -> str:
    """Arbeitsverzeichnis eines Steps: ``step.workdir`` (Template) überschreibt ``ctx['workdir']``."""
    return _substitute(str(step.get("workdir") or ctx.get("workdir") or "."), ctx)


def _cond_matches(cond: dict, ctx: dict) -> bool:
    """`{var: wert | [werte]}`: JEDE Variable (AND) hat einen der Werte (OR).
    Fehlende Variable → keine Übereinstimmung."""
    for var, allowed in cond.items():
        if var not in ctx:
            return False
        values = allowed if isinstance(allowed, list) else [allowed]
        if str(ctx[var]) not in [str(v) for v in values]:
            return False
    return True


def tier_policy(tier) -> dict:
    """Tier→Verhalten — eine Funktion (P1/P2 erweitern das Substrat).
    P0: T1 = auto nach Checks, T2+ = Approval am Ende (irreversibel/Accountability)."""
    t = int(tier) if tier is not None else 1
    return {"tier": t, "requires_approval": t >= 2}
