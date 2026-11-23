"""Step: notify (slack/webhook) + _notify (Alerts laut Spec-Konfiguration)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error

from .. import config
from ..context import _substitute
from ..failures import parse_kind
from ..gitops import _http_post
from ..hosting import HostingError, Provider, get_provider
from ..i18n import t as _t
from ..runmodel import FAILED_RUN_STATUSES, FAILED_STEP_STATUSES, FailureKind
from ..secrets import redact_log_tail

log = logging.getLogger("fabrik.notify")

# Alert-Dedup: Fingerprint-Marker im Issue-Body (HTML-Kommentar, unsichtbar).
_ALERT_MARKER = "<!-- fabrik-alert:fp={fp} -->"
# Lauf-spezifische Tokens, die den Fingerprint nicht beeinflussen dürfen —
# normalisiert werden: UUIDs (8-4-4-4-12) und Run-/Session-IDs/Hashes
# (Hex-Tokens ≥ 8 Zeichen mit mindestens einem Hex-Buchstaben a-f, auch
# eingebettet, z. B. `copilot-8a1f1b454c95`). Dezimale Zahlen — auch lange
# (Timestamps, Epoch-ms) — bleiben erhalten, damit sich Fehlerbilder nicht
# wild zusammenziehen. (Trade-off: eine rein dezimale 12-Char-Session-ID
# [(10/16)^12 ≈ 0,04 %] würde nicht normalisiert → Duplikat-Issue.)
_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID (8-4-4-4-12)
    r"|(?=[0-9a-f]*[a-f])[0-9a-f]{8,}",  # Hex-Token (mind. ein a-f, sonst Dezimalzahl)
    re.IGNORECASE,
)
# Ersatz-Token für normalisierte IDs im Fingerprint (eindeutiger als "#",
# damit die Normalisierung im Code/Doku eindeutig nachvollziehbar ist).
_VOLATILE_REPLACEMENT = "<id>"
# Fehler, die der Hosting-Provider im Alert-Flow werfen kann und die wir
# gezielt fangen (Fail-safe: ein Alert-Fehler darf den Run nicht killen):
# HTTP 4xx/5xx, Netzwerk-Flakes, Timeouts, kaputte JSON-Antworten,
# HostingError (Token nicht verfügbar, ADR 0062) sowie RuntimeError aus
# _gh_token (GitHub, Token nicht verfügbar). Programmierfehler
# (TypeError, AttributeError, …) fallen NICHT hier rein — sie propagieren
# nach oben und werden dort mit Stacktrace geloggt (kein stilles Verschlucken).
_GH_API_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
    json.JSONDecodeError,
    RuntimeError,
    HostingError,
)


# Kette wiederholt noch (pending) oder ist bereits beendet (dismissed,
# resolved): kein run_failed-Alert — der Fehlschlag ist nicht endgültig bzw.
# die Kette wurde gelöst (z. B. ein pass desselben Schlüssels).
_SILENT_RETRY_STATES = frozenset({"pending", "dismissed", "resolved"})


def _notify(spec: dict, record: dict, retry_state: str | None = None) -> list[str]:
    """Feuert Alerts laut Spec-Konfiguration. Liefert Liste der gesendeten Kanäle.

    ``retry_state`` (ADR 0036, Entscheidung 6): Zustand der Retry-Kette nach dem
    Run. ``"pending"`` → ``run_failed`` wird unterdrückt (die Kette wiederholt
    noch); ``"dismissed"``/``"resolved"`` (Versuch einer verworfenen bzw.
    gelösten Kette) → kein Alert; ``"exhausted"`` → stattdessen genau ein
    ``run_exhausted``-Event (Dedup über den Fingerprint, ADR 0025).
    ``None`` = keine Kette (klassisches ``run_failed``). ``run_exhausted`` ist ein Spezialfall von ``run_failed``:
    wer nur ``run_failed`` abonniert, bekommt es ebenfalls — sonst ginge mit
    Retry der Alert für den endgültigen Fehlschlag verloren.

    Spec-Format:
      notify:
        slack: https://hooks.slack.com/...        # oder {url: ...}
        webhooks:                                  # generisch (Datadog, CloudWatch, …)
          - {name: datadog, url: https://http-intake.logs.datadoghq.com/v1/input?api_key=…}
        github_issue: true                         # bei run_failed/run_exhausted Issue öffnen
        issue_labels: [fabrik]
      notify_events: [run_failed, run_exhausted]   # default; auch [run_failed, run_passed]
    """
    cfg = spec.get("notify") or {}
    if not cfg:
        return []
    events = cfg.get("events") or spec.get("notify_events") or ["run_failed", "run_exhausted"]
    status = record["status"]
    if status in FAILED_RUN_STATUSES:
        if retry_state in _SILENT_RETRY_STATES:
            return []  # Kette wiederholt noch bzw. wurde verworfen (ADR 0036, Entscheidung 6)
        ev = "run_exhausted" if retry_state == "exhausted" else "run_failed"
    elif status == "pass":
        ev = "run_passed"
    else:
        ev = None
    if not ev or not (ev in events or (ev == "run_exhausted" and "run_failed" in events)):
        return []
    # log_tail: bekannte Secrets maskiert bereits emit() beim Log-Write; zusätzlich
    # pattern-basierte Redaction (ADR 0026) — einziger Aufrufpunkt für alle Kanäle.
    tail = ""
    logf = config.RUNS_DIR / record["project"] / f"{record['id']}.log"
    if logf.is_file():
        tail = "\n".join(logf.read_text(encoding="utf-8").splitlines()[-15:])
        tail = redact_log_tail(tail)
    payload = {
        "event": ev,
        "projekt": record["project"],
        "loop": record["loop"],
        "status": status,
        "lauf": record["id"],
        "trigger": record.get("trigger", ""),
        "message": f"fabrik: {record['project']}/{record['loop']} → {status}",
        "log_tail": tail,
    }
    if ev in ("run_failed", "run_exhausted"):
        kind = parse_kind(record.get("failure_kind"))
        payload["failure_kind"] = kind.value
        payload["message"] += f" [{_kind_label(kind)}]"
    sent = []
    slack = cfg.get("slack")
    if slack:
        url = slack["url"] if isinstance(slack, dict) else slack
        code, _ = _http_post(str(url), {"text": f"*{payload['message']}*\n```\n{tail}```"})
        sent.append(f"slack:{code}")
    for wh in cfg.get("webhooks") or []:
        code, _ = _http_post(str(wh["url"]), {**payload, "channel": wh.get("name", "webhook")})
        sent.append(f"{wh.get('name', 'webhook')}:{code}")
    if ev in ("run_failed", "run_exhausted") and cfg.get("github_issue"):
        gh = spec.get("github")
        if gh:
            try:
                labels = [str(x) for x in (cfg.get("issue_labels") or ["fabrik"])]
                sent.append(_alert_issue(get_provider(str(gh), spec=spec), str(gh), labels, record, tail))
            except _GH_API_ERRORS as e:
                # Fail-safe-Grenze: nur API-/Token-/Netzwerkfehler (der
                # _GH_API_ERRORS-Vertrag) werden in einen Kanal-Eintrag
                # übersetzt — ein Alert-Fehler darf den Run nicht killen.
                # Programmierfehler (KeyError, TypeError, AttributeError, …)
                # fallen NICHT hier rein: sie propagieren und werden mit
                # Stacktrace sichtbar, statt als scheinbar normaler
                # Kanalstatus verschluckt zu werden.
                log.exception("Alert-Issue fehlgeschlagen (Run %s)", record["id"])
                sent.append(f"issue:error({e})")
    return sent


# Umgebungsfehler löst nur ein Mensch — im Alert sofort erkennbar machen (ADR 0035 Nr. 5).


def _kind_label(kind: FailureKind) -> str:
    """Fehlerklasse für den Alert-Text; ``environment`` hervorgehoben. Der Issue-Titel
    nutzt den kurzen ``kind.value`` (dort wäre der lange Label zu plump)."""
    return _t("notify.needs_human") if kind is FailureKind.ENVIRONMENT else kind.value


def _failure_reason(record: dict) -> tuple[str, str]:
    """→ (Step-Name, Fehlertext) des ersten fehlgeschlagenen Steps; ohne
    fehlgeschlagenen Step (z. B. Stale-Run) der Run-Fehler bzw. -Status."""
    for st in record.get("steps") or []:
        if st.get("status") in FAILED_STEP_STATUSES:
            return str(st.get("name", "?")), str(st.get("summary", ""))
    return "run", str(record.get("error") or record.get("status", ""))


def _alert_fingerprint(record: dict) -> str:
    """Stabiler Fingerprint eines Fehlerbilds: Projekt/Loop + fehlgeschlagener
    Step + normalisierter Fehlertext. Normalisiert werden nur lauf-spezifische
    IDs (UUIDs, Hex-Tokens ≥ 8 Zeichen mit mindestens einem Hex-Buchstaben);
    dezimale Zahlen (Fehleranzahl, Ports, Timestamps) bleiben erhalten, damit
    sich Fehlerbilder nicht zusammenziehen. Gleicher Fehler in verschiedenen
    Runs → gleicher Fingerprint."""
    step, reason = _failure_reason(record)
    norm = _VOLATILE.sub(_VOLATILE_REPLACEMENT, reason)[: config.ALERT_REASON_CAP]
    key = f"{record['project']}/{record['loop']}/{step}/{norm}"
    return hashlib.sha256(key.encode()).hexdigest()[: config.ALERT_FP_LEN]


def _find_open_alert(provider: Provider, gh: str, labels: list[str], marker: str) -> int | None:
    """Nummer des offenen Alert-Issues mit `marker` im Body (oder None).

    Paginiert über ALLE offenen Alert-Issues (bis eine Seite unvollständig
    ist), damit auch bei mehr offenen Issues als `ALERT_OPEN_ISSUES_PAGE`
    kein relevantes Issue übersehen wird. Nach `ALERT_LOOKUP_MAX_PAGES`
    Seiten wird abgebrochen (Rate-Limit-Schutz; dann mögliches Duplikat).
    Die Seitengröße wird auf `GH_MAX_PER_PAGE` gekappt — GitHub liefert nie
    mehr, und eine größere Soll-Größe würde jede Seite als „letzte" werten.

    Labels sind UND-verknüpft: nach einer Änderung von `issue_labels` matchen
    alte Issues nicht mehr. Der Provider normalisiert die Antwort (nicht-Liste
    → leere Liste, kaputte Elemente werden übersprungen, ADR 0062).
    """
    per_page = min(config.ALERT_OPEN_ISSUES_PAGE, config.GH_MAX_PER_PAGE)
    page = 1
    while True:
        issues = provider.list_issues(gh, state="open", labels=labels, page=page, per_page=per_page)
        for it in issues:
            if it.is_pr:  # GitHub-Liste enthält auch PRs → ausschließen
                continue
            if marker in it.body:
                return it.number
        if len(issues) < per_page:  # letzte Seite → kein Match
            return None
        if page >= config.ALERT_LOOKUP_MAX_PAGES:  # Rate-Limit-Schutz: Lookup abbrechen
            log.warning(
                _t("steps.notify.alert_lookup_d_pages_d"),
                page,
                page * per_page,
            )
            return None
        page += 1


def _sanitize_title(text: str, cap: int = 80) -> str:
    """Title-sicher: Whitespace kollabieren, Markdown-/Klammer-Zeichen ersetzen,
    truncate (Issue-Titles dürfen keine Newlines/Markdown enthalten)."""
    t = re.sub(r"\s+", " ", text).strip()
    t = t.replace("`", "'").replace("[", "(").replace("]", ")")
    return t[:cap].rstrip()


def _alert_issue(provider: Provider, gh: str, labels: list[str], record: dict, tail: str) -> str:
    """Alert-Issue mit Dedup: Ist für denselben Fingerprint bereits ein Issue
    offen, wird der Run dort kommentiert statt ein neues Issue anzulegen
    (sonst: ein Issue pro fehlgeschlagenem Cron-Run). Geschlossene Issues
    zählen nicht — tritt der Fehler nach dem Schließen erneut auf, gibt es ein
    neues Issue. → Kanal-Eintrag `issue#N` bzw. `issue#N(+)` (kommentiert)."""
    step, reason = _failure_reason(record)
    marker = _ALERT_MARKER.format(fp=_alert_fingerprint(record))
    kind = parse_kind(record.get("failure_kind"))
    run_info = (
        f"Run `{record['id']}` · Trigger: `{record.get('trigger', '')}` · "
        f"Fehlerklasse: **{_kind_label(kind)}**\n\n```\n{tail}\n```"
    )
    try:
        existing = _find_open_alert(provider, gh, labels, marker)
    except _GH_API_ERRORS:
        # Fail-safe: Lookup kaputt (API-Fehler) → lieber Duplikat als
        # verlorener Alert (Stacktrace wird geloggt, Exception nicht
        # verschluckt).
        log.exception(_t("steps.notify.alert_lookup_failed_repo_s"), gh)
        existing = None
    if existing is not None:
        try:
            provider.create_comment(gh, existing, f"Erneut fehlgeschlagen — {run_info}")
        except _GH_API_ERRORS as e:
            log.exception("Alert-Kommentar fehlgeschlagen (Issue #%s, Run %s)", existing, record["id"])
            return f"issue:{existing}(+):error({e})"
        return f"issue#{existing}(+)"
    try:
        issue = provider.create_issue(
            gh,
            title=(
                f"[fabrik] {record['project']}/{record['loop']} FAILED [{kind.value}] — "
                f"{step}: {_sanitize_title(reason)}"
            ),
            body=_t("steps.notify.further_runs_with_the_same", run_info=run_info, v2=record["project"], marker=marker),
            labels=labels,
        )
    except _GH_API_ERRORS as e:
        log.exception("Alert-Issue-Anlage fehlgeschlagen (Repo %s, Run %s)", gh, record["id"])
        return f"issue:error({e})"
    if not issue.number:  # API-Antwort ohne Issue-Nummer (Shape-Change, Fehler-Body)
        return _t("steps.notify.issue_error_no_issue_number")

    return f"issue#{issue.number}"


def _step_notify(ctx: dict, step: dict, emit) -> dict:
    """Benachrichtigung als Step (slack/webhook). Ohne Ziel → nur Log (ok)."""
    msg = _substitute(step.get("message", "fabrik: {project}/{loop} → {run_id}"), ctx)
    slack = step.get("slack")
    webhooks = step.get("webhooks") or []
    if not slack and not webhooks:
        emit(_t("steps.notify.notify_no_channels_configured_only", msg=msg))
        return {"ok": True, "summary": _t("steps.notify.log_only_no_channels")}
    codes = []
    if slack:
        url = slack["url"] if isinstance(slack, dict) else str(slack)
        code, _ = _http_post(url, {"text": msg})
        codes.append(code)
    for wh in webhooks:
        code, _ = _http_post(
            str(wh["url"]),
            {
                "channel": wh.get("name", "webhook"),
                "message": msg,
                "projekt": ctx["project"],
                "loop": ctx["loop"],
                "lauf": ctx["run_id"],
            },
        )
        codes.append(code)
    ok = all(c in (200, 201, 204) for c in codes)
    summary = ", ".join(str(c) for c in codes)
    emit(f"  notify: {summary}")
    return {"ok": ok, "summary": f"codes {summary}"}
