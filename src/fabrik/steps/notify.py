"""Step: notify (slack/webhook) + _notify (Alerts laut Spec-Konfiguration)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse

from .. import config
from ..context import _substitute
from ..failures import parse_kind
from ..gitops import _gh_api, _gh_token, _http_post
from ..i18n import t as _t
from ..runmodel import FAILED_RUN_STATUSES, FAILED_STEP_STATUSES, FailureKind
from ..secrets import redact_log_tail

log = logging.getLogger("fabrik.notify")

# Alert-Dedup: Fingerprint-Marker im Issue-Body (HTML-Kommentar, unsichtbar).
_ALERT_MARKER = "<!-- fabrik-alert:fp={fp} -->"
# Run-specific tokens that must not influence the fingerprint —
# normalized are: UUIDs (8-4-4-4-12) and run/session IDs/hashes
# (hex tokens ≥ 8 characters with at least one hex letter a-f, also
# embedded, e.g. `copilot-8a1f1b454c95`). Decimal numbers — even long
# ones (timestamps, epoch ms) — are kept so failure patterns do not
# merge wildly. (Trade-off: a purely decimal 12-char session ID
# [(10/16)^12 ≈ 0.04 %] would not be normalized → duplicate issue.)
_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID (8-4-4-4-12)
    r"|(?=[0-9a-f]*[a-f])[0-9a-f]{8,}",  # Hex token (at least one a-f, otherwise decimal number)
    re.IGNORECASE,
)
# Replacement token for normalized IDs in the fingerprint (more distinctive than "#",
# so the normalization can be traced unambiguously in code/docs).
_VOLATILE_REPLACEMENT = "<id>"
# Errors the GitHub call layer can raise in the alert flow and that we
# catch deliberately (fail-safe: an alert error must not kill the run):
# HTTP 4xx/5xx, network flakes, timeouts, broken JSON answers as well as
# RuntimeError from _gh_token (token not available). Programming errors
# (TypeError, AttributeError, …) do NOT fall in here — they propagate
# upward and are logged there with a stack trace (no silent swallowing).
_GH_API_ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
    json.JSONDecodeError,
    RuntimeError,
)


# Chain is still retrying (pending) or has already ended (dismissed,
# resolved): no run_failed alert — the failure is not final or
# the chain was resolved (e.g. a pass of the same key).
_SILENT_RETRY_STATES = frozenset({"pending", "dismissed", "resolved"})


def _notify(spec: dict, record: dict, retry_state: str | None = None) -> list[str]:
    """Fires alerts per spec configuration. Returns the list of sent channels.

    ``retry_state`` (ADR 0036, decision 6): state of the retry chain after the
    run. ``"pending"`` → ``run_failed`` is suppressed (the chain is still
    retrying); ``"dismissed"``/``"resolved"`` (attempt of a discarded or
    resolved chain) → no alert; ``"exhausted"`` → instead exactly one
    ``run_exhausted`` event (dedup via the fingerprint, ADR 0025).
    ``None`` = no chain (classic ``run_failed``). ``run_exhausted`` is a special case of ``run_failed``:
    whoever subscribes only to ``run_failed`` receives it as well — otherwise with
    retry the alert for the final failure would be lost.

    Spec format:
      notify:
        slack: https://hooks.slack.com/...        # or {url: ...}
        webhooks:                                  # generic (Datadog, CloudWatch, …)
          - {name: datadog, url: https://http-intake.logs.datadoghq.com/v1/input?api_key=…}
        github_issue: true                         # open an issue on run_failed/run_exhausted
        issue_labels: [fabrik]
      notify_events: [run_failed, run_exhausted]   # default; also [run_failed, run_passed]
    """
    cfg = spec.get("notify") or {}
    if not cfg:
        return []
    events = cfg.get("events") or spec.get("notify_events") or ["run_failed", "run_exhausted"]
    status = record["status"]
    if status in FAILED_RUN_STATUSES:
        if retry_state in _SILENT_RETRY_STATES:
            return []  # Chain is still retrying or was discarded (ADR 0036, decision 6)
        ev = "run_exhausted" if retry_state == "exhausted" else "run_failed"
    elif status == "pass":
        ev = "run_passed"
    else:
        ev = None
    if not ev or not (ev in events or (ev == "run_exhausted" and "run_failed" in events)):
        return []
    # log_tail: known secrets are already masked by emit() at log write; additionally
    # pattern-based redaction (ADR 0026) — single call point for all channels.
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
                sent.append(_alert_issue(str(gh), labels, record, tail))
            except _GH_API_ERRORS as e:
                # Fail-safe boundary: only API/token/network errors (the
                # _GH_API_ERRORS contract) are translated into a channel entry
                # — an alert error must not kill the run.
                # Programming errors (KeyError, TypeError, AttributeError, …)
                # do NOT fall in here: they propagate and become visible with a
                # stack trace, instead of being swallowed as an apparently normal
                # channel status.
                log.exception("Alert-Issue fehlgeschlagen (Run %s)", record["id"])
                sent.append(f"issue:error({e})")
    return sent


# An environment error is solved only by a human — make it immediately recognizable in the alert (ADR 0035 no. 5).


def _kind_label(kind: FailureKind) -> str:
    """Failure class for the alert text; ``environment`` highlighted. The issue title
    uses the short ``kind.value`` (the long label would be too clumsy there)."""
    return _t("notify.needs_human") if kind is FailureKind.ENVIRONMENT else kind.value


def _failure_reason(record: dict) -> tuple[str, str]:
    """→ (step name, error text) of the first failed step; without a
    failed step (e.g. stale run) the run error or status."""
    for st in record.get("steps") or []:
        if st.get("status") in FAILED_STEP_STATUSES:
            return str(st.get("name", "?")), str(st.get("summary", ""))
    return "run", str(record.get("error") or record.get("status", ""))


def _alert_fingerprint(record: dict) -> str:
    """Stable fingerprint of a failure pattern: project/loop + failed
    step + normalized error text. Only run-specific
    IDs are normalized (UUIDs, hex tokens ≥ 8 characters with at least one hex letter);
    decimal numbers (error counts, ports, timestamps) are kept so
    failure patterns do not merge. Same error in different
    runs → same fingerprint."""
    step, reason = _failure_reason(record)
    norm = _VOLATILE.sub(_VOLATILE_REPLACEMENT, reason)[: config.ALERT_REASON_CAP]
    key = f"{record['project']}/{record['loop']}/{step}/{norm}"
    return hashlib.sha256(key.encode()).hexdigest()[: config.ALERT_FP_LEN]


def _find_open_alert(gh: str, labels: list[str], marker: str, tok: str) -> int | None:
    """Number of the open alert issue with `marker` in the body (or None).

    Paginates over ALL open alert issues (until a page is incomplete),
    so even with more open issues than `ALERT_OPEN_ISSUES_PAGE`
    no relevant issue is overlooked. After `ALERT_LOOKUP_MAX_PAGES`
    pages it aborts (rate-limit protection; then a possible duplicate).
    The page size is capped at `GH_MAX_PER_PAGE` — GitHub never returns
    more, and a larger target size would treat every page as "last".

    Query via `urlencode`: the comma in `labels` is percent-encoded
    (labels=a%2Cb) — the GitHub API accepts that (test:
    test_alert_dedup_comments_on_open_issue). Labels are AND-combined:
    after a change of `issue_labels` old issues no longer match.
    """
    per_page = min(config.ALERT_OPEN_ISSUES_PAGE, config.GH_MAX_PER_PAGE)
    page = 1
    while True:
        q = urllib.parse.urlencode({"state": "open", "labels": ",".join(labels), "per_page": per_page, "page": page})
        issues: object = _gh_api("GET", f"https://api.github.com/repos/{gh}/issues?{q}", tok)
        if not isinstance(issues, list):  # `_gh_api` is typed to dict, the list API returns list
            log.warning(
                _t("steps.notify.alert_lookup_unexpected_api_response"),
                type(issues).__name__,
            )
            return None
        for it in issues:
            if not isinstance(it, dict):  # unexpected element (shape change) → skip
                log.warning(
                    _t("steps.notify.alert_lookup_unexpected_list_item"),
                    type(it).__name__,
                )
                continue
            if "pull_request" in it:  # GitHub list also contains PRs → exclude
                continue
            if marker in (it.get("body") or ""):
                return it.get("number")
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
    """Title-safe: collapse whitespace, replace markdown/bracket characters,
    truncate (issue titles must not contain newlines/markdown)."""
    t = re.sub(r"\s+", " ", text).strip()
    t = t.replace("`", "'").replace("[", "(").replace("]", ")")
    return t[:cap].rstrip()


def _alert_issue(gh: str, labels: list[str], record: dict, tail: str) -> str:
    """Alert issue with dedup: if an issue is already open for the same fingerprint,
    the run is commented there instead of creating a new issue
    (otherwise: one issue per failed cron run). Closed issues
    do not count — if the error occurs again after closing, there is a
    new issue. → channel entry `issue#N` or `issue#N(+)` (commented)."""
    tok = _gh_token()
    step, reason = _failure_reason(record)
    marker = _ALERT_MARKER.format(fp=_alert_fingerprint(record))
    kind = parse_kind(record.get("failure_kind"))
    run_info = (
        f"Run `{record['id']}` · Trigger: `{record.get('trigger', '')}` · "
        f"Fehlerklasse: **{_kind_label(kind)}**\n\n```\n{tail}\n```"
    )
    try:
        existing = _find_open_alert(gh, labels, marker, tok)
    except _GH_API_ERRORS:
        # Fail-safe: lookup broken (API error) → better a duplicate than a
        # lost alert (stack trace is logged, exception not
        # swallowed).
        log.exception(_t("steps.notify.alert_lookup_failed_repo_s"), gh)
        existing = None
    if existing is not None:
        try:
            _gh_api(
                "POST",
                f"https://api.github.com/repos/{gh}/issues/{existing}/comments",
                tok,
                {"body": f"Erneut fehlgeschlagen — {run_info}"},
            )
        except _GH_API_ERRORS as e:
            log.exception("Alert-Kommentar fehlgeschlagen (Issue #%s, Run %s)", existing, record["id"])
            return f"issue:{existing}(+):error({e})"
        return f"issue#{existing}(+)"
    try:
        res = _gh_api(
            "POST",
            f"https://api.github.com/repos/{gh}/issues",
            tok,
            {
                "title": (
                    f"[fabrik] {record['project']}/{record['loop']} FAILED [{kind.value}] — "
                    f"{step}: {_sanitize_title(reason)}"
                ),
                "body": (
                    _t(
                        "steps.notify.further_runs_with_the_same",
                        run_info=run_info,
                        v2=record["project"],
                        marker=marker,
                    )
                ),
                "labels": labels,
            },
        )
    except _GH_API_ERRORS as e:
        log.exception("Alert-Issue-Anlage fehlgeschlagen (Repo %s, Run %s)", gh, record["id"])
        return f"issue:error({e})"
    num = res.get("number") if isinstance(res, dict) else None
    if num is None:  # API response without issue number (shape change, error body)
        return _t("steps.notify.issue_error_no_issue_number")
    return f"issue#{num}"


def _step_notify(ctx: dict, step: dict, emit) -> dict:
    """Notification as a step (slack/webhook). Without a target → log only (ok)."""
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
