"""Step: llm (OpenAI-kompatibel, strukturierte JSON-Ausgabe + Schema-Check)."""

from __future__ import annotations

import json
import re

from .. import llm as llm_registry
from ..billing import for_step as billing_for_step
from ..context import _cap, _substitute, untrusted_blocks
from ..failures import classify_exception, failed
from ..i18n import t as _t
from ..runmodel import FailureKind
from ..tokens import TokenCounts
from .extra_context import render_extra_context

# finish_reason=length → one retry with max_tokens × factor. Reasoning models
# (Qwen3, …) use the budget for thinking and otherwise return empty content.
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
    """Subset validation: required keys + type checks + optional enum whitelist
    (no jsonschema dependency).

    Schema value: type string ("string", "integer", …) OR dict
    {"type": "string", "enum": ["a", "b"], "default": "a"} — enum restricts the
    value to a whitelist (e.g. a label that later ends up in shell commands).
    ``default`` makes the key optional: if missing, ``parsed`` is complemented with
    the default (in place) and it is checked like an LLM value. LLMs
    like to omit empty fields, even if the prompt demands them.
    """
    errs = []
    for key, spec in schema.items():
        typ, enum = (spec.get("type"), spec.get("enum")) if isinstance(spec, dict) else (spec, None)
        if key not in parsed:
            if isinstance(spec, dict) and "default" in spec:
                parsed[key] = spec["default"]
            else:
                errs.append(_t("steps.llm.missing", key=key))
                continue
        t: type | tuple[type, ...] | None = _SCHEMA_TYPES.get(typ) if isinstance(typ, str) else None
        v = parsed[key]
        if t is None:
            errs.append(f"'{key}': unbekannter Schema-Typ '{typ}'")
        elif typ == "integer" and isinstance(v, bool):
            errs.append(_t("steps.llm.should_be_integer_is_bool", key=key))
        elif not isinstance(v, t):
            errs.append(_t("steps.llm.should_be_is", key=key, typ=typ, v3=type(v).__name__))
        elif enum is not None and v not in enum:
            errs.append(_t("steps.llm.value_not_in_allowed_values", key=key, v=v, enum=enum))
    return errs


def _extract_json_object(text: str) -> dict | None:
    """Returns the JSON object from an LLM answer, despite prose around it.

    If the answer contains several top-level objects (the model corrects itself:
    ``{…} Moment, … corrected: {…}``), the last one wins. A greedy regex from the
    first ``{`` to the last ``}`` would grab both including prose and fail.
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
    """Provider for the step: ``provider: <name>`` pins, otherwise the active one.

    Raises:
        _ProviderError: pinned provider unknown or none configured.
    """
    prov_name = step.get("provider")
    if prov_name:
        prov = llm_registry.get_provider(prov_name)
        if not prov:
            known = ", ".join(p["name"] for p in llm_registry.list_providers()) or _t("steps.llm.none")
            raise _ProviderError(_t("steps.llm.llm_provider_unknown_available", prov_name=prov_name, known=known))
        return prov
    prov = llm_registry.active_provider()
    if not prov:
        raise _ProviderError(_t("steps.llm.no_llm_configured_fabrik_llm"))
    return prov


def _build_prompt(step: dict, ctx: dict) -> str:
    """Prompt with ctx substitution; ``untrusted`` variables are appended as marked
    data blocks (prompt-injection protection, guiding principle 7)."""
    prompt = _substitute(step.get("prompt", ""), ctx)
    untrusted = step.get("untrusted") or []
    prompt += untrusted_blocks([(v, str(ctx.get(v, ""))) for v in untrusted])
    return prompt


def _complete(prov: dict, step: dict, prompt: str, model: str, emit) -> dict:
    """LLM call; on ``finish_reason=length`` one retry with multiplied
    ``max_tokens`` (usage of both calls summed). Exceptions propagate."""
    max_tokens = int(step.get("max_tokens", _DEFAULT_MAX_TOKENS))

    def _call(budget: int) -> dict:
        return llm_registry.complete(
            prov,
            step.get("system", _t("steps.llm.answer_only_with_valid_json")),
            prompt,
            model=model,
            temperature=float(step.get("temperature", 0.2)),
            max_tokens=budget,
            timeout=int(step.get("timeout", 180)),
        )

    res = _call(max_tokens)
    if res.get("finish_reason") == "length":
        retry_budget = max_tokens * _TRUNCATION_RETRY_FACTOR
        emit(_t("steps.llm.llm_answer_truncated_max_tokens", max_tokens=max_tokens, retry_budget=retry_budget))
        first_usage = res["usage"]
        res = _call(retry_budget)
        res["usage"] = {k: v + first_usage.get(k, 0) for k, v in res["usage"].items()}
    return res


def _extract_json(text: str) -> tuple[str, dict | None]:
    """Removes a ```json fence (if present) and extracts the object.

    Returns ``(cleaned text, object or None)``."""
    m = re.search(r"```(?:json)?\n(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    return text, _extract_json_object(text)


def _inject_schema_keys(parsed: dict, schema: dict, ctx: dict, emit) -> list[str]:
    """Takes LLM values into the ctx — ONLY schema keys, and only if they do not
    exist yet. Otherwise untrusted input via LLM output could overwrite trusted
    keys like workdir/root → execution path (guiding principle 2).

    Returns ``key=value`` short forms of the taken-over keys."""
    injected = []
    for k, v in parsed.items():
        if k not in schema:
            emit(_t("steps.llm.llm_key_not_in_the", k=k))
            continue
        if k in ctx:
            emit(_t("steps.llm.llm_key_already_exists_in", k=k))
            continue
        ctx[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
        injected.append(f"{k}={str(v)[:40]}")
    return injected


def _step_llm(ctx: dict, step: dict, emit) -> dict:
    """Structured LLM call: prompt + output schema → JSON → variable store.

    untrusted: [vars] — external content is marked as data
    (prompt-injection protection, guiding principle 7).

    Provider selection: ``provider: <name>`` pins a configured provider,
    otherwise the active one applies (switchable in the UI). ``model: <name>`` overrides the
    provider model. Truncated answer (finish_reason=length) → one retry
    with multiplied ``max_tokens``."""
    try:
        prov = _resolve_provider(step)
    except _ProviderError as e:
        return failed(str(e), FailureKind.ENVIRONMENT, output="")
    model = (step.get("model") or prov["model"]).strip()
    prompt = _build_prompt(step, ctx) + render_extra_context(ctx, step, emit)
    untrusted = step.get("untrusted") or []
    emit(
        f"  LLM {model} (prompt {len(prompt)} chars"
        + (_t("steps.llm.untrusted_blocks", v1=len(untrusted)) if untrusted else "")
        + ")"
    )
    try:
        res = _complete(prov, step, prompt, model, emit)
        text = (res["text"] or "").strip()
    except Exception as e:
        return failed(f"LLM-Call fehlgeschlagen: {e}", classify_exception(e), output="")
    usage = {
        "model": model,
        "provider": prov.get("name") or step.get("provider") or "active",
        **res["usage"],
        "billing": billing_for_step(step).value,
    }
    text, parsed = _extract_json(text)
    if parsed is None:
        summary = _t("steps.llm.llm_returned_no_json_object")
        if res.get("finish_reason") == "length":
            max_tokens = int(step.get("max_tokens", _DEFAULT_MAX_TOKENS))
            summary += _t("steps.llm.truncated_even_with_max_tokens", v1=max_tokens * _TRUNCATION_RETRY_FACTOR)
        return {"ok": False, "output": _cap(text), "summary": summary, "usage": usage}
    schema = step.get("schema") or {}
    errs = _validate_schema(parsed, schema)
    if errs:
        return {"ok": False, "output": _cap(text), "summary": "Schema-Validierung: " + "; ".join(errs)}
    injected = _inject_schema_keys(parsed, schema, ctx, emit)
    emit(f"  LLM usage: {TokenCounts.from_usage(usage).summary()}")
    return {
        "ok": True,
        "output": _cap(text),
        "summary": ", ".join(injected) if injected else _t("steps.llm.no_schema_llm_output_only"),
        "usage": usage,
    }
