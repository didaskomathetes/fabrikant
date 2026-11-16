"""Step: llm (OpenAI-kompatibel, strukturierte JSON-Ausgabe + Schema-Check)."""

from __future__ import annotations

import json
import re

from .. import llm as llm_registry
from ..context import _cap, _substitute, untrusted_blocks
from ..failures import classify_exception, failed
from ..runmodel import FailureKind
from .extra_context import render_extra_context

# finish_reason=length → ein Retry mit max_tokens × Faktor. Reasoning-Modelle
# (Qwen3, …) verbrauchen das Budget fürs Denken und liefern sonst leeren Content.
_TRUNCATION_RETRY_FACTOR = 4
_DEFAULT_MAX_TOKENS = 2000

_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "list": list,
    "dict": dict,
}


def _validate_schema(parsed: dict, schema: dict) -> list[str]:
    """Subset-Validierung: required-Keys + Typ-Checks + optionale Enum-Whitelist
    (keine jsonschema-Dependency).

    Schema-Wert: Typ-String ("string", "integer", …) ODER Dict
    {"type": "string", "enum": ["a", "b"], "default": "a"} — enum schränkt den
    Wert auf eine Whitelist ein (z. B. Label, das später in Shell-Kommandos
    landet). ``default`` macht den Key optional: fehlt er, wird ``parsed`` mit
    dem Default ergänzt (in-place) und dieser wie ein LLM-Wert geprüft. LLMs
    lassen leere Felder gern weg, auch wenn der Prompt sie verlangt.
    """
    errs = []
    for key, spec in schema.items():
        typ, enum = (spec.get("type"), spec.get("enum")) if isinstance(spec, dict) else (spec, None)
        if key not in parsed:
            if isinstance(spec, dict) and "default" in spec:
                parsed[key] = spec["default"]
            else:
                errs.append(f"'{key}' fehlt")
                continue
        t: type | tuple[type, ...] | None = _SCHEMA_TYPES.get(typ) if isinstance(typ, str) else None
        v = parsed[key]
        if t is None:
            errs.append(f"'{key}': unbekannter Schema-Typ '{typ}'")
        elif typ == "integer" and isinstance(v, bool):
            errs.append(f"'{key}' soll integer sein, ist bool")
        elif not isinstance(v, t):
            errs.append(f"'{key}' soll {typ} sein, ist {type(v).__name__}")
        elif enum is not None and v not in enum:
            errs.append(f"'{key}': Wert {v!r} nicht in erlaubten Werten {enum}")
    return errs


def _extract_json_object(text: str) -> dict | None:
    """Liefert das JSON-Object aus einer LLM-Antwort, trotz Prosa drumherum.

    Enthält die Antwort mehrere Top-Level-Objects (Modell korrigiert sich:
    ``{…} Moment, … korrigiert: {…}``), gewinnt das letzte. Ein gieriger Regex von der
    ersten ``{`` bis zur letzten ``}`` würde beide samt Prosa greifen und scheitern.
    """
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return whole if isinstance(whole, dict) else None
    decoder = json.JSONDecoder()
    found = None
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            end = pos + 1
        else:
            if isinstance(obj, dict):
                found = obj
        pos = text.find("{", end)
    return found


class _ProviderError(Exception):
    """Kein nutzbarer LLM-Provider (Konfigurationsfehler → FailureKind.ENVIRONMENT)."""


def _resolve_provider(step: dict) -> dict:
    """Provider für den Step: ``provider: <name>`` pinnt, sonst der aktive.

    Raises:
        _ProviderError: gepinnter Provider unbekannt bzw. keiner konfiguriert.
    """
    prov_name = step.get("provider")
    if prov_name:
        prov = llm_registry.get_provider(prov_name)
        if not prov:
            known = ", ".join(p["name"] for p in llm_registry.list_providers()) or "(keine)"
            raise _ProviderError(f"LLM-Provider '{prov_name}' unbekannt (verfügbar: {known})")
        return prov
    prov = llm_registry.active_provider()
    if not prov:
        raise _ProviderError("kein LLM konfiguriert (FABRIK_LLM_URL + FABRIK_LLM_MODEL oder FABRIK_LLM_PROVIDERS)")
    return prov


def _build_prompt(step: dict, ctx: dict) -> str:
    """Prompt mit ctx-Substitution; ``untrusted``-Variablen werden als markierte
    Datenblöcke angehängt (Prompt-Injection-Schutz, Leitprinzip 7)."""
    prompt = _substitute(step.get("prompt", ""), ctx)
    untrusted = step.get("untrusted") or []
    prompt += untrusted_blocks([(v, str(ctx.get(v, ""))) for v in untrusted])
    return prompt


def _complete(prov: dict, step: dict, prompt: str, model: str, emit) -> dict:
    """LLM-Call; bei ``finish_reason=length`` ein Retry mit vervielfachtem
    ``max_tokens`` (Usage beider Calls summiert). Exceptions propagieren."""
    max_tokens = int(step.get("max_tokens", _DEFAULT_MAX_TOKENS))

    def _call(budget: int) -> dict:
        return llm_registry.complete(
            prov,
            step.get("system", "Antworte NUR mit validem JSON. Kein Markdown, keine Kommentare."),
            prompt,
            model=model,
            temperature=float(step.get("temperature", 0.2)),
            max_tokens=budget,
            timeout=int(step.get("timeout", 180)),
        )

    res = _call(max_tokens)
    if res.get("finish_reason") == "length":
        retry_budget = max_tokens * _TRUNCATION_RETRY_FACTOR
        emit(f"  ⚠ LLM-Antwort abgeschnitten (max_tokens={max_tokens}) — Retry mit max_tokens={retry_budget}")
        first_usage = res["usage"]
        res = _call(retry_budget)
        res["usage"] = {k: v + first_usage.get(k, 0) for k, v in res["usage"].items()}
    return res


def _extract_json(text: str) -> tuple[str, dict | None]:
    """Entfernt einen ```json-Fence (falls vorhanden) und extrahiert das Object.

    Liefert ``(bereinigter Text, Object oder None)``."""
    m = re.search(r"```(?:json)?\n(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    return text, _extract_json_object(text)


def _inject_schema_keys(parsed: dict, schema: dict, ctx: dict, emit) -> list[str]:
    """Übernimmt LLM-Werte in den ctx — NUR Schema-Keys, und nur wenn sie noch
    nicht existieren. Sonst kann Untrusted-Input via LLM-Ausgabe vertrauenswürdige
    Keys wie workdir/root überschreiben → Execution-Path (Leitprinzip 2).

    Liefert ``key=wert``-Kurzformen der übernommenen Keys."""
    injected = []
    for k, v in parsed.items():
        if k not in schema:
            emit(f"  ⚠ llm: Key '{k}' nicht im Schema — ignoriert (ctx-Isolation)")
            continue
        if k in ctx:
            emit(f"  ⚠ llm: Key '{k}' existiert bereits im ctx — nicht überschrieben")
            continue
        ctx[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
        injected.append(f"{k}={str(v)[:40]}")
    return injected


def _step_llm(ctx: dict, step: dict, emit) -> dict:
    """Strukturierter LLM-Call: Prompt + Output-Schema → JSON → Variable-Store.

    untrusted: [vars] — externe Inhalte werden als Daten markiert
    (Prompt-Injection-Schutz, Leitprinzip 7).

    Provider-Auswahl: ``provider: <name>`` pinnt einen konfigurierten Provider,
    sonst gilt der aktive (UI-umschaltbar). ``model: <name>`` überschreibt das
    Provider-Modell. Abgeschnittene Antwort (finish_reason=length) → ein Retry
    mit vervielfachtem ``max_tokens``."""
    try:
        prov = _resolve_provider(step)
    except _ProviderError as e:
        return failed(str(e), FailureKind.ENVIRONMENT, output="")
    model = (step.get("model") or prov["model"]).strip()
    prompt = _build_prompt(step, ctx) + render_extra_context(ctx, step, emit)
    untrusted = step.get("untrusted") or []
    emit(
        f"  LLM {model} (prompt {len(prompt)} chars"
        + (f", {len(untrusted)} untrusted-Blöcke" if untrusted else "")
        + ")"
    )
    try:
        res = _complete(prov, step, prompt, model, emit)
        text = (res["text"] or "").strip()
    except Exception as e:
        return failed(f"LLM-Call fehlgeschlagen: {e}", classify_exception(e), output="")
    usage = {"model": model, "provider": prov.get("name") or step.get("provider") or "active", **res["usage"]}
    text, parsed = _extract_json(text)
    if parsed is None:
        summary = "LLM lieferte kein JSON-Object"
        if res.get("finish_reason") == "length":
            max_tokens = int(step.get("max_tokens", _DEFAULT_MAX_TOKENS))
            summary += f" (abgeschnitten, auch mit max_tokens={max_tokens * _TRUNCATION_RETRY_FACTOR})"
        return {"ok": False, "output": _cap(text), "summary": summary, "usage": usage}
    schema = step.get("schema") or {}
    errs = _validate_schema(parsed, schema)
    if errs:
        return {"ok": False, "output": _cap(text), "summary": "Schema-Validierung: " + "; ".join(errs)}
    injected = _inject_schema_keys(parsed, schema, ctx, emit)
    emit(
        f"  LLM usage: {usage['total_tokens']} tokens ({usage['prompt_tokens']} in / {usage['completion_tokens']} out)"
    )
    return {
        "ok": True,
        "output": _cap(text),
        "summary": ", ".join(injected) if injected else "kein Schema — LLM-Output nur im Log",
        "usage": usage,
    }
