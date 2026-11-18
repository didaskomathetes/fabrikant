"""Secret-Masking für Logs & Run-Records (env + Env-Dateien + Spec-env)."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

from . import config

log = logging.getLogger("fabrik.secrets")

_SECRET_NAME_RE = re.compile(r"(TOKEN|KEY|SECRET|PASS|CREDENTIAL)", re.I)
_EXTRA_SECRET_FILES = [
    config.FABRIK_HOME / "fabrik.env",
    Path(os.environ.get("FABRIK_ENV_FILE", "/root/.hermes/.env")),
]
# Kürzere Werte werden nicht maskiert (sonst träfe das Masking beliebige kurze Wörter im Log).
MIN_SECRET_LEN = 8
_extra_secrets_cache: list[str] | None = None
_env_lock = threading.Lock()
_runtime_secrets: list[str] = []  # Spec-env-Values, pro Run registriert
_sources: list[Callable[[], list[str]]] = []  # weitere Secret-Quellen (z. B. UI-Connections)


def register_source(source: Callable[[], list[str]]) -> None:
    """Zusätzliche Quelle für Secret-Werte registrieren (idempotent)."""
    if source not in _sources:
        _sources.append(source)


def _scan_environ() -> list[str]:
    """Secret-Werte aus os.environ lesen (iteriert die Umgebung — nur zu ruhigen Zeitpunkten aufrufen)."""
    return [v for k, v in os.environ.items() if v and len(v) >= MIN_SECRET_LEN and _SECRET_NAME_RE.search(k)]


_env_secrets: list[str] = _scan_environ()  # Snapshot beim Import (Prozessstart, noch single-threaded)


def refresh_env_secrets() -> None:
    """Env-Snapshot explizit neu einlesen (z. B. nach bewusstem Setzen eines Secrets in os.environ).

    Nicht im Hotpath aufrufen: die Iteration über os.environ ist nicht thread-sicher gegenüber
    parallelen putenv/setenv-Aufrufen anderer Threads.
    """
    global _env_secrets
    snapshot = _scan_environ()
    with _env_lock:
        _env_secrets = snapshot


def _secrets() -> list[str]:
    """Secret-Werte für Log-/Record-Masking (Env-Snapshot + Env-Dateien + Spec-env, gecacht).

    Iteriert os.environ bewusst NICHT (Segfault-Hazard bei parallelem setenv, Issue #13).
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
    """Secrets in Record-Feldern maskieren, BEVOR der Record als JSON geschrieben
    wird (Run-JSON ist per /api/run/{id} im UI lesbar — darf kein Token tragen)."""
    for s in record.get("steps", []):
        for k in ("summary", "output"):
            if s.get(k):
                s[k] = _redact(s[k])
        # Nested verify-Checks: Pro-Check-Output ist ein eigener Datapfad
        # (z. B. Shell-Output mit Secrets) — muss mitgemasckt werden.
        for v in s.get("verify") or []:
            for k in ("output", "summary"):
                if v.get(k):
                    v[k] = _redact(v[k])
    for k in ("error", "note"):
        if record.get(k):
            record[k] = _redact(record[k])
    # Approval-Kontext (was/Rechte/Issue): String-Werte maskieren, damit kein
    # Secret (z. B. aus step.permissions oder einem cmd) in die UI-lesbare JSON
    # gelangt.
    appr = record.get("approval")
    if isinstance(appr, dict):
        for k, v in appr.items():
            if isinstance(v, str):
                appr[k] = _redact(v)
            elif isinstance(v, list):
                appr[k] = [_redact(x) if isinstance(x, str) else x for x in v]
    # Run-Variablen (record['vars']): String-Werte maskieren — Variablen können
    # Secrets tragen (z. B. Spec-env-Werte), der Record ist per /api/run/{id}
    # im UI lesbar.
    vars_ = record.get("vars")
    if isinstance(vars_, dict):
        for k, v in vars_.items():
            if isinstance(v, str):
                vars_[k] = _redact(v)
    return record


REDACTED = "[REDACTED]"
# Pattern-basierte Redaction für Freitext (z. B. Alert-`log_tail`); ergänzt das
# Wert-basierte `_redact`. Reihenfolge relevant: Private-Key-Blöcke zuerst.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Private-Key-Block (auch abgeschnitten: ohne END-Zeile bis Textende)
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        REDACTED,
    ),
    # Bearer-Tokens (Authorization-Header o. ä.)
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
    # AWS-Access-Key-IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Bekannte API-Key-Präfixe (OpenAI/Anthropic sk-, GitHub, Slack)
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[A-Za-z0-9-]{10,})"
        ),
        REDACTED,
    ),
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
    """Maskiert typische Secret-Muster (Bearer, API-/AWS-Keys, Private-Key-Blöcke,
    key=/token=-Assignments) in Freitext durch `[REDACTED]`. Kann Fehlalarme
    liefern (lieber zu viel als zu wenig maskieren)."""
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_log_tail(text: str) -> str:
    """`redact_text` gesteuert über `config.ALERT_LOG_REDACTION`. Fail-safe: bei
    einem Redaction-Fehler wird unredigiert zurückgegeben (+ Warning), damit
    ein Alert nie verloren geht."""
    if not config.ALERT_LOG_REDACTION:
        return text
    try:
        return redact_text(text)
    except Exception:
        log.warning("log_tail-Redaction fehlgeschlagen — Alert wird unredigiert gesendet", exc_info=True)
        return text
