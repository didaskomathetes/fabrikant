"""Secret masking for logs & run records (env + env files + spec env)."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config
from .i18n import t

log = logging.getLogger("fabrikant.secrets")

_SECRET_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASS|CREDENTIAL)", re.I)
_EXTRA_SECRET_FILES = [
    config.FABRIKANT_HOME / config.ENV_FILE_NAME,
    Path(os.environ.get("FABRIKANT_ENV_FILE", config.DEFAULT_ENV_FILE)),
]
# Shorter values are not masked (otherwise the masking would hit arbitrary short words in the log).
MIN_SECRET_LEN = 8
_extra_secrets_cache: list[str] | None = None
_env_lock = threading.Lock()
_runtime_secrets: list[str] = []  # Spec-env-Values, pro Run registriert
_sources: list[Callable[[], list[str]]] = []  # weitere Secret-Quellen (z. B. UI-Connections)


def register_source(source: Callable[[], list[str]]) -> None:
    """Register an additional source for secret values (idempotent)."""
    if source not in _sources:
        _sources.append(source)


def _scan_environ() -> list[str]:
    """Read secret values from os.environ (iterates the environment — call only at quiet moments)."""
    return [v for k, v in os.environ.items() if v and len(v) >= MIN_SECRET_LEN and _SECRET_NAME_RE.search(k)]


_env_secrets: list[str] = _scan_environ()  # Snapshot at import (process start, still single-threaded)


def refresh_env_secrets() -> None:
    """Explicitly re-read the env snapshot (e.g. after deliberately setting a secret in os.environ).

    Do not call in the hot path: iterating over os.environ is not thread-safe against
    parallel putenv/setenv calls of other threads.
    """
    global _env_secrets
    snapshot = _scan_environ()
    with _env_lock:
        _env_secrets = snapshot


def _secrets() -> list[str]:
    """Secret values for log/record masking (env snapshot + env files + spec env, cached).

    Deliberately does NOT iterate os.environ (segfault hazard with parallel setenv, issue #13).
    """
    global _extra_secrets_cache
    vals = list(_env_secrets)
    if _extra_secrets_cache is None:
        _extra_secrets_cache = []
        for f in _EXTRA_SECRET_FILES:
            try:
                for line in f.read_text().splitlines():
                    m = re.match(r'^\s*([A-Z0-9_]+)=["\']?([^"\']+)', line)
                    if m and _SECRET_NAME_RE.search(m.group(1)) and len(m.group(2).strip()) >= MIN_SECRET_LEN:
                        _extra_secrets_cache.append(m.group(2).strip())
            except Exception:
                pass
    vals.extend(_extra_secrets_cache)
    vals.extend(_runtime_secrets)
    for source in _sources:
        vals.extend(source())
    return vals


def _redact(s: str) -> str:
    for secret in _secrets():
        if secret in s:
            s = s.replace(secret, "[REDACTED]")
    return s


def _redact_record(record: dict) -> dict:
    """Mask secrets in record fields BEFORE the record is written as JSON
    (run JSON is readable in the UI via /api/run/{id} — must carry no token)."""
    for s in record.get("steps", []):
        for k in ("summary", "output"):
            if s.get(k):
                s[k] = _redact(s[k])
        # Nested verify checks: per-check output is its own data path
        # (e.g. shell output with secrets) — must be masked too.
        for v in s.get("verify") or []:
            for k in ("output", "summary"):
                if v.get(k):
                    v[k] = _redact(v[k])
    for k in ("error", "note"):
        if record.get(k):
            record[k] = _redact(record[k])
    # Approval context (what/permissions/issue): mask string values so no
    # secret (e.g. from step.permissions or a cmd) ends up in the UI-readable
    # JSON.
    appr = record.get("approval")
    if isinstance(appr, dict):
        for k, v in appr.items():
            if isinstance(v, str):
                appr[k] = _redact(v)
            elif isinstance(v, list):
                appr[k] = [_redact(x) if isinstance(x, str) else x for x in v]
    # Run variables (record['vars']): mask string values — variables can
    # carry secrets (e.g. spec env values), the record is readable in the UI via
    # /api/run/{id}.
    vars_ = record.get("vars")
    if isinstance(vars_, dict):
        for k, v in vars_.items():
            if isinstance(v, str):
                vars_[k] = _redact(v)
    return record


REDACTED = "[REDACTED]"
# Pattern-based redaction for free text (e.g. alert `log_tail`); complements the
# value-based `_redact`. Order matters: private-key blocks first.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Private-key block (also truncated: without END line until end of text)
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        REDACTED,
    ),
    # Bearer tokens (Authorization header or similar)
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
    # AWS-Access-Key-IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Known API-key prefixes (OpenAI/Anthropic sk-, GitHub, Slack)
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"
        ),
        REDACTED,
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.I), r"\1" + REDACTED + r"\2"),
    # Generische Assignments: key=…, "token": "…", password: …
    (
        re.compile(
            r"(?i)(\b[\w.-]*(?:api[_-]?key|key|secret|token|passw(?:or)?d|pwd|credential|access[_-]?key|auth)[\w.-]*"
            r"[\"']?\s*[=:]\s*)(?:[\"'][^\"']*[\"']|[^\s\"',;&]+)"
        ),
        r"\1" + REDACTED,
    ),
)


def redact_text(text: str) -> str:
    """Masks typical secret patterns (bearer, API/AWS keys, private-key blocks,
    key=/token= assignments) in free text with `[REDACTED]`. May produce
    false positives (better to mask too much than too little)."""
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_log_tail(text: str) -> str:
    """`redact_text` controlled via `config.ALERT_LOG_REDACTION`. Fail-safe: on
    a redaction error the text is returned unredacted (+ warning), so
    an alert is never lost."""
    if not config.ALERT_LOG_REDACTION:
        return text
    try:
        return redact_text(text)
    except Exception:
        log.warning(t("secrets.log_tail_redaction_failed_alert"), exc_info=True)
        return text


# Key-based masking of structured data (specs shown to agents/UI): values of secret-looking keys.
MASKED_VALUE = "***redacted***"
_SECRET_KEY_RE = re.compile(r"token|secret|passw|api[_-]?key|credential|authorization|private[_-]?key", re.I)
# Short names that are secrets only as a whole key (a substring match would hit ``monkey``, ``author``).
_SECRET_EXACT_KEY_RE = re.compile(r"(?:key|auth|bearer|pat|dsn|pwd)", re.I)
# Token *counts and limits* (max_tokens, token_budget, ...) are not secrets; masking them would
# hide ordinary settings from agents and, on a round trip, overwrite them with the mask. The
# optional prefix may not itself contain a secret word (``secret_max_tokens`` stays masked), and
# the bare singular ``input_token`` / ``output_token`` forms can be credentials, so only plural counts.
_NOT_SECRET_KEY_RE = re.compile(
    r"^(?!.*(?:secret|passw|credential|api[_-]?key|authorization|private[_-]?key))"
    r"(?:\w*[_-])?(?:(?:max|min|num|n|total)[_-]?tokens?|(?:prompt|completion|input|output)[_-]?tokens)$"
    r"|^tokens?[_-](?:budget|limit|count|used|usage|ttl|max|min)$",
    re.I,
)


def is_secret_key(key: object) -> bool:
    """Whether the value stored under ``key`` should be masked."""
    name = str(key)
    if _SECRET_EXACT_KEY_RE.fullmatch(name):
        return True
    return bool(_SECRET_KEY_RE.search(name)) and not _NOT_SECRET_KEY_RE.match(name)


def redact_secret_keys(obj: Any) -> Any:
    """Recursively mask values of secret-looking keys (token, password, api key, ...).

    A matching key masks its *entire* value (any type), so a secret held as a list or
    nested dict (e.g. ``api_keys: [...]``, ``credentials: {user, pass}``) is not returned
    verbatim.
    """
    if isinstance(obj, dict):
        return {k: MASKED_VALUE if is_secret_key(k) else redact_secret_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_secret_keys(v) for v in obj]
    return obj


def redact_for_agent(obj: Any) -> Any:
    """Like :func:`redact_secret_keys`, and additionally scrub secret patterns and known secret
    values out of every string (``DB_URL=postgres://u:p@h``, ``--token abc`` in free text).

    For anything handed to an MCP client: key names alone miss secrets under innocuous keys.
    """
    if isinstance(obj, dict):
        return {k: MASKED_VALUE if is_secret_key(k) else redact_for_agent(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_for_agent(v) for v in obj]
    if isinstance(obj, str):
        return _redact(redact_text(obj))
    return obj


def contains_masked_value(obj: Any) -> bool:
    """Whether ``obj`` holds the mask anywhere (a redacted value echoed back by an agent)."""
    if isinstance(obj, dict):
        return any(contains_masked_value(v) for v in obj.values())
    if isinstance(obj, list):
        return any(contains_masked_value(v) for v in obj)
    return obj == MASKED_VALUE
