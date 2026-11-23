"""Stufe B: deterministischer Prompt-Injection-Scan (ADR 0060 §2b) — kein LLM.

Jede Regel ist ein (id, Pattern, Schweregrad). Ein Treffer wird als
:class:`ScanFinding` mit einem kurzen Snippet protokolliert. Der Scan ist
rein heuristisch: er fängt offensichtliche Injection-Versuche ab, ist aber
kein Sicherheitsbeweis. ``quarantine``-Treffer isolieren die Quelle,
``suspicious``-Treffer markieren sie für Nachschau.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .clean import normalize_text

SUSPICIOUS = "suspicious"
QUARANTINE = "quarantine"

# CSS inside <style> blocks is markup, not injected prose: `display:none` in a stylesheet
# must not fire the hidden-text rules (false positive). Only those CSS rules are exempt
# (`Rule.css`); phrase, marker, exfiltration and blob rules still see the style content.
_STYLE_OPEN_RE = re.compile(r"<style\b[^>]*>", re.IGNORECASE)
_STYLE_CLOSE_RE = re.compile(r"</style\s*>", re.IGNORECASE)


def _strip_style_blocks(text: str) -> str:
    """Blank out ``<style>…</style>`` blocks in linear time.

    A single regex like ``<style\b[^>]*>.*?</style>`` backtracks to the end of the text
    for every unclosed ``<style>``, i.e. quadratic on hostile input. Unclosed blocks are
    left untouched (so nothing is silently hidden from the CSS rules' *absence*).
    """
    closes = [(m.start(), m.end()) for m in _STYLE_CLOSE_RE.finditer(text)]
    out: list[str] = []
    pos = 0
    ci = 0
    for m in _STYLE_OPEN_RE.finditer(text):
        if m.start() < pos:
            continue  # nested/overlapping opener inside an already blanked block
        while ci < len(closes) and closes[ci][0] < m.end():
            ci += 1
        if ci == len(closes):
            break  # no closing tag after this opener (nor any later one)
        out.append(text[pos : m.start()])
        out.append(" ")
        pos = closes[ci][1]
    out.append(text[pos:])
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    severity: str  # SUSPICIOUS | QUARANTINE
    css: bool = False  # CSS-hiding rule: not applied inside <style> blocks


def _c(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


# Schweregrad je Gruppe: Instruktion-Overrides und Exfiltration isolieren
# (quarantine), die übrigen markieren (suspicious).
_RULES: list[Rule] = [
    # --- Instruktion-Override (quarantine) ---
    Rule("override_ignore", _c(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions"), QUARANTINE),
    Rule(
        "override_ignore_de",
        _c(
            r"ignorier(e|en)\s+(sie\s+|du\s+)?(alle\s+)?(vorherigen|fr[üu]heren|oberen)\s+(anweisungen|regeln|instruktionen)"
        ),
        QUARANTINE,
    ),
    Rule("override_disregard", _c(r"disregard\s+(your|all|the)\s+(instructions|guidelines|rules)"), QUARANTINE),
    Rule("override_new_system", _c(r"(new|updated)\s+system\s+prompt"), QUARANTINE),
    # --- Prompt-Grenzmarker (quarantine) ---
    Rule("marker_system_tag", _c(r"</?system\s*>|<\|system\|>|<\|im_start\|>\s*system"), QUARANTINE),
    Rule("marker_sys_delim", _c(r"###\s*system\b|<<sys>>|\[system\]"), QUARANTINE),
    # --- Jailbreak / Rollenspiel (suspicious) ---
    Rule("jailbreak_dan", _c(r"\b(DAN|developer|jailbreak)\s+mode\b|(?-i:\bDAN\b).{0,80}\bmode\b"), SUSPICIOUS),
    Rule("jailbreak_actas", _c(r"\bact as if\b|\bpretend (to be|you are)\b|ab sofort bist du"), SUSPICIOUS),
    # --- Geheimer Text (suspicious) ---
    Rule("hidden_display_none", _c(r"display\s*:\s*none"), SUSPICIOUS, css=True),
    Rule("hidden_font0", _c(r"font-size\s*:\s*0(px)?\b"), SUSPICIOUS, css=True),
    Rule("hidden_indent", _c(r"text-indent\s*:\s*-?\d{3,}"), SUSPICIOUS, css=True),
    Rule(
        "hidden_samecolor",
        _c(
            r"color\s*:\s*#?fff(?:ffff)?[^{]{0,100}background[^{]{0,100}#?fff(?:ffff)?|background[^{]{0,100}#?fff(?:ffff)?[^{]{0,100}color\s*:\s*#?fff(?:ffff)?"
        ),
        SUSPICIOUS,
        css=True,
    ),
    # --- Encodierte Blöcke (suspicious) ---
    Rule("blob_base64", _c(r"[A-Za-z0-9+/]{200,}={0,2}"), SUSPICIOUS),
    Rule("blob_hex", _c(r"\b[0-9a-fA-F]{120,}\b"), SUSPICIOUS),
    # --- Exfiltration / Reverse Shell (quarantine) ---
    Rule("exfil_curl", _c(r"curl\s+[^|\n]*(-d|--data|--data-raw)\b"), QUARANTINE),
    Rule("exfil_wget_post", _c(r"wget\s+[^|\n]*--post-(data|file)\b"), QUARANTINE),
    Rule("exfil_nc", _c(r"\bnc\s+(-[elp]+\s+)?[^|\n]*\d{1,5}\s+-e\b|/dev/(tcp|udp)/"), QUARANTINE),
    Rule("exfil_b64pipe", _c(r"base64\s+(-d|--decode)\b[^|\n]*\|\s*(ba)?sh\b"), QUARANTINE),
]

_RULES_BY_ID = {r.id: r for r in _RULES}


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    severity: str
    snippet: str


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - 24)
    hi = min(len(text), end + 24)
    return (
        "…" + text[lo:hi].replace("\n", " ").strip() + "…"
        if (lo > 0 or hi < len(text))
        else text[lo:hi].replace("\n", " ").strip()
    )


def scan(text: str) -> list[ScanFinding]:
    """Text deterministisch scannen; liefert alle Treffer (geordnet nach Regel)."""
    # Fold compatibility forms (fullwidth etc.) and drop invisible characters so
    # that obfuscated phrases match; the same normalisation the agent-facing text gets.
    text = normalize_text(text)
    markup_free = _strip_style_blocks(text)  # view for the CSS-hiding rules
    findings: list[ScanFinding] = []
    for rule in _RULES:
        haystack = markup_free if rule.css else text
        for m in rule.pattern.finditer(haystack):
            findings.append(ScanFinding(rule.id, rule.severity, _snippet(haystack, m.start(), m.end())))
    return findings


def verdict(findings: list[ScanFinding]) -> str:
    """``clean`` / ``suspicious`` / ``quarantined`` aus den Treffern ableiten."""
    if any(f.severity == QUARANTINE for f in findings):
        return "quarantined"
    if findings:
        return "suspicious"
    return "clean"


def rules() -> list[Rule]:
    """Alle Scan-Regeln (für Tests/Doku)."""
    return list(_RULES)


def rule_by_id(rule_id: str) -> Rule | None:
    return _RULES_BY_ID.get(rule_id)
