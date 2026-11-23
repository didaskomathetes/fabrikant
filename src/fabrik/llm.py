"""LLM-Provider-Registry: mehrere OpenAI-kompatible Endpunkte, umschaltbar.

Provider werden per Env konfiguriert (fabrik.env):

  - Legacy (ein Endpunkt, wird Provider ``default``):
        FABRIK_LLM_URL=http://127.0.0.1:11434/v1/chat/completions
        FABRIK_LLM_MODEL=llama3.2:3b
        # FABRIK_LLM_KEY=
  - Named (beliebig viele, z. B. Ollama + lokale llama.cpp):
        FABRIK_LLM_PROVIDERS=ollama,llama-cpp
        FABRIK_LLM_OLLAMA_URL=http://127.0.0.1:11434/v1/chat/completions
        FABRIK_LLM_OLLAMA_MODEL=llama3.2:3b
        FABRIK_LLM_LLAMA_CPP_URL=http://127.0.0.1:8080/v1/chat/completions
        FABRIK_LLM_LLAMA_CPP_MODEL=Qwen3.8-27B-GGUF
  - CLI statt HTTP (``_CLI`` statt ``_URL``; Name muss in FABRIK_LLM_PROVIDERS
    stehen; Auth = CLI-Login, s. ``llm_cli``):
        FABRIK_LLM_PROVIDERS=claude
        FABRIK_LLM_CLAUDE_CLI=claude
        FABRIK_LLM_CLAUDE_MODEL=claude-sonnet-5-5

Der aktive Provider ist zur Laufzeit umschaltbar (UI: ``POST /api/llm``) und
wird in ``state/active_llm`` persistiert (überlebt Neustarts). Ohne explizite
Wahl gilt der Default (erster Named-Provider, sonst ``default``).

Ein Step kann seinen Provider per ``provider: <name>`` pinnen und das Modell
per ``model: <name>`` überschreiben (``steps/llm.py``). ``complete()`` ist der
gemeinsame Call für Chat und llm-Step (HTTP oder CLI).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
from collections.abc import Mapping
from typing import Any

from . import config, connections, httpsafe, llm_cli
from .i18n import t
from .tokens import TokenCounts

log = logging.getLogger("fabrik.llm")
_ACTIVE_FILE = config.STATE_DIR / "active_llm"
_guard = threading.Lock()
_active_cache: str | None = None  # None = noch nicht aufgelöst


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _named_providers() -> list[str]:
    raw = _env("FABRIK_LLM_PROVIDERS")
    if not raw:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


def _provider_cfg(name: str) -> dict | None:
    """Config eines Named-Providers aus FABRIK_LLM_<NAME>_{URL,MODEL,KEY}.

    Der Name wird für den Env-Präfix normalisiert (``llama-cpp`` → ``LLAMA_CPP``),
    da Env-Variablen keine Bindestriche als Shell-Namen tragen."""
    prefix = "FABRIK_LLM_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_"
    url = _env(prefix + "URL")
    cli = _env(prefix + "CLI")
    model = _env(prefix + "MODEL")
    if not model or (not url and cli not in llm_cli.CLIS):
        return None
    if cli:
        return {"name": name, "url": "", "cli": cli, "model": model, "key": ""}
    return {"name": name, "url": url, "model": model, "key": _env(prefix + "KEY")}


def _legacy_provider() -> dict | None:
    """Legacy FABRIK_LLM_URL/MODEL/KEY → Provider 'default'."""
    url = _env("FABRIK_LLM_URL")
    model = _env("FABRIK_LLM_MODEL")
    if not url or not model:
        return None
    return {"name": "default", "url": url, "model": model, "key": _env("FABRIK_LLM_KEY")}


def env_providers() -> list[dict]:
    """Env-konfigurierte Provider (Named zuerst, dann Legacy 'default'); schreibgeschützt."""
    out: list[dict] = []
    seen: set[str] = set()
    for name in _named_providers():
        cfg = _provider_cfg(name)
        if cfg and cfg["name"] not in seen:
            out.append(cfg)
            seen.add(cfg["name"])
    legacy = _legacy_provider()
    if legacy and legacy["name"] not in seen:
        out.append(legacy)
    return out


def list_providers() -> list[dict]:
    """Alle Provider: Env-Provider, dazu UI-Verbindungen (ADR 0052; gleicher Name → UI ersetzt Env)."""
    merged = {p["name"]: p for p in env_providers()}
    try:
        for c in connections.db_connections(connections.ConnectionKind.LLM):
            merged[c.name] = c.as_llm_provider()
    except Exception:  # DB nicht verfügbar → Env-Provider bleiben nutzbar
        log.warning(t("llm.ui_connections_unreadable_env_providers"), exc_info=True)
    return list(merged.values())


def get_provider(name: str) -> dict | None:
    for p in list_providers():
        if p["name"] == name:
            return p
    return None


def default_name() -> str | None:
    """Fallback-Aktiver: als Default markierte UI-Verbindung, sonst erster Provider, sonst None."""
    providers = list_providers()
    try:
        flagged = connections.default_connection(connections.ConnectionKind.LLM)
    except Exception:
        flagged = None
    if flagged and any(p["name"] == flagged.name for p in providers):
        return flagged.name
    return providers[0]["name"] if providers else None


def _read_persisted() -> str | None:
    try:
        v = _ACTIVE_FILE.read_text().strip()
        return v or None
    except FileNotFoundError:
        return None


def active_name() -> str | None:
    """Name des aktiven Providers (Cache → persistiert → Default)."""
    global _active_cache
    with _guard:
        if _active_cache and get_provider(_active_cache):
            return _active_cache
        persisted = _read_persisted()
        if persisted and get_provider(persisted):
            _active_cache = persisted
            return persisted
        _active_cache = default_name()
        return _active_cache


def set_active(name: str) -> str:
    """Aktiven Provider setzen + persistieren. Wirft ValueError bei unbekanntem Name."""
    global _active_cache
    with _guard:
        if not get_provider(name):
            known = ", ".join(p["name"] for p in list_providers()) or t("llm.none")
            raise ValueError(t("llm.llm_provider_unknown_available", name=name, known=known))
        _ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ACTIVE_FILE.write_text(name + "\n")
        _active_cache = name
        return name


def active_provider() -> dict | None:
    """Config des aktiven Providers (None = kein LLM konfiguriert)."""
    name = active_name()
    return get_provider(name) if name else None


def reset_cache() -> None:
    """In-Memory-Cache leeren (Tests / nach Env-Änderung)."""
    global _active_cache
    with _guard:
        _active_cache = None


def complete(
    prov: dict,
    system: str,
    user: str,
    *,
    model: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2000,
    timeout: int = 180,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Ein Chat-Completion-Call gegen ``prov`` (HTTP oder CLI).

    → ``{"text": str | None, "finish_reason": str, "usage": {prompt_tokens,
    completion_tokens, total_tokens, cache_read_tokens, cache_write_tokens}}``. ``text`` None/leer = leere Antwort
    (Aufrufer entscheidet). Transport-Fehler werfen. CLI-Provider ignorieren
    ``temperature``/``max_tokens``; ``cancel_event`` killt dort die CLI.
    """
    model = model or prov["model"]
    if prov.get("cli"):
        text, tokens = llm_cli.complete(prov["cli"], model, system, user, timeout, cancel_event)
        return {"text": text, "finish_reason": "stop", "usage": TokenCounts.from_poll(tokens).usage_fields()}
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if prov.get("key"):
        headers["Authorization"] = f"Bearer {prov['key']}"
    req = urllib.request.Request(prov["url"], data=json.dumps(payload).encode(), headers=headers, method="POST")
    with httpsafe.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    choice = (data.get("choices") or [{}])[0]
    return {
        "text": (choice.get("message") or {}).get("content"),
        "finish_reason": choice.get("finish_reason", "?"),
        "usage": _usage(data.get("usage")),
    }


def _usage(u: object) -> dict[str, int]:
    """OpenAI-Usage-Block → Step-Usage-Token-Felder; malformiert → Nullen (Drittanbieter-APIs
    dürfen einen erfolgreichen Call nicht in einen Failure drehen).

    Gecachte Prompt-Tokens stehen in ``prompt_tokens_details.cached_tokens`` (in ``prompt_tokens``
    enthalten); Cache-Writes meldet die Chat-Completions-API nicht."""
    if not isinstance(u, Mapping):
        return TokenCounts().usage_fields()
    details = u.get("prompt_tokens_details")
    cached = _token_count(details.get("cached_tokens")) if isinstance(details, Mapping) else 0
    fields = TokenCounts(
        _token_count(u.get("prompt_tokens")), _token_count(u.get("completion_tokens")), cached
    ).usage_fields()
    # Ein gemeldetes total_tokens hat Vorrang (manche Anbieter zählen z. B. Reasoning extra).
    fields["total_tokens"] = _token_count(u.get("total_tokens"))
    return fields


def _token_count(v: Any) -> int:
    """Ein Usage-Feld → int; malformiert (``"n/a"``, ``inf``, ``nan``, Liste …) → 0.

    Pro Feld, damit ein kaputtes Feld die gültigen nicht mit nullt. Über
    ``float``, damit ``5.5`` und ``"5.5"`` gleich (abgeschnitten → 5) zählen."""
    try:
        return int(float(v or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
