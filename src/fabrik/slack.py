"""`on_slack`-Trigger: Slack-Channels pollen, Signatur matchen, deduplizieren (ADR 0040).

Reine Logik hinter einem kleinen Client-Port (`SlackClient`); der Scheduler
startet den Run danach über dieselbe Funktion wie `on_issue` (`_safe_run` mit
Initial-Variablen) — kein eigener Run-Pfad. Das Bot-Token kommt nur aus der
Env-Variable `token_env`; es steht weder in Spec, State noch Logs.

`alert_text` ist untrusted (beliebige Slack-Nutzer): in `llm`-Steps nur über
`untrusted: [alert_text]`, in `shell`-Steps nur über `env:` verwenden (ADR 0008).
`alert_channel` (Whitelist) und `alert_ts` (Format-geprüft) sind unkritisch."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from . import config, httpsafe
from .i18n import t

SLACK_API = "https://slack.com/api"
DEFAULT_POLL_S = 60
HISTORY_LIMIT = 100  # Nachrichten pro conversations.history-Aufruf
HTTP_TIMEOUT_S = 15
SEEN_MAX = 500  # Retention der Dedup-Historie pro Channel
ACCEPTED_SUBTYPES = (None, "bot_message")  # Joins, Edits, Deletes usw. sind keine Alerts
TRIGGER_PREFIX = "on_slack:"
VAR_TEXT, VAR_CHANNEL, VAR_TS = "alert_text", "alert_channel", "alert_ts"
ALERT_VARS = (VAR_TEXT, VAR_CHANNEL, VAR_TS)  # Laufzeit-Variablen eines on_slack-Runs
SLACK_TS_RE = re.compile(r"^\d{1,12}\.\d{1,9}$")  # Slack-`ts`, z. B. "1729650000.000100"
CHANNEL_ID_RE = re.compile(r"^[A-Z0-9]{2,32}$")  # Channel-/DM-ID, z. B. "C0123ALERTS"

# Scheduler-Thread (poll) und Worker-Threads (mark_started) schreiben dieselbe
# State-Datei; ohne Lock gehen Updates verloren (→ Doppel-Run eines pending Alerts).
_state_lock = threading.Lock()
log = logging.getLogger("fabrik.slack")


class SlackError(Exception):
    """Slack-API-Fehler (ohne Token im Text)."""


@dataclass(frozen=True)
class Alert:
    """Eine passende Slack-Nachricht = Initial-Variablen eines Runs."""

    text: str
    channel: str
    ts: str

    @property
    def trigger(self) -> str:
        """Trigger-String des Run-Records (`on_slack:<channel>:<ts>`)."""
        return f"{TRIGGER_PREFIX}{self.channel}:{self.ts}"

    def variables(self) -> dict[str, str]:
        """Initial-Variablen (untrusted, framework §1.4)."""
        return {VAR_TEXT: self.text, VAR_CHANNEL: self.channel, VAR_TS: self.ts}


@dataclass
class History:
    """Ergebnis eines History-Abrufs; `complete=False` = Seitenlimit erreicht, ältere fehlen."""

    messages: list[dict] = field(default_factory=list)
    complete: bool = True


class SlackClient(Protocol):
    """Port für Slack; in Tests durch einen Fake ersetzt."""

    def history(self, channel: str, oldest: str, *, latest: str | None, max_pages: int) -> History:
        """Nachrichten von `channel` mit `oldest` < `ts` < `latest`, neueste zuerst (wie Slack).

        Höchstens `max_pages` Seiten; wurde abgebrochen, ist `complete` False und es
        fehlen die ältesten Nachrichten des Bereichs."""
        ...


class WebApiClient:
    """`conversations.history` per Bot-Token (Scopes: channels:history, groups:history, im:history)."""

    def __init__(self, token: str):
        self._token = token

    def _history_page(self, channel: str, oldest: str, latest: str | None, cursor: str) -> tuple[list[dict], str]:
        params: dict[str, str | int] = {"channel": channel, "oldest": oldest, "limit": HISTORY_LIMIT}
        if latest:
            params["latest"] = latest
        if cursor:
            params["cursor"] = cursor
        url = f"{SLACK_API}/conversations.history?{urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with httpsafe.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            raise SlackError(data.get("error", t("slack.unknown_error")))
        next_cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
        return list(data.get("messages") or []), next_cursor

    def history(self, channel: str, oldest: str, *, latest: str | None, max_pages: int) -> History:
        """Folgt Slacks `next_cursor` (neueste zuerst) bis zur letzten Seite oder `max_pages`."""
        result = History()
        cursor = ""
        for _ in range(max_pages):
            page, cursor = self._history_page(channel, oldest, latest, cursor)
            result.messages.extend(page)
            if not cursor:
                return result
        result.complete = False
        return result


def token_from_env(trig: dict) -> str:
    """Bot-Token aus der in `token_env` benannten Env-Variable (leer = nicht gesetzt)."""
    return os.environ.get(str(trig.get("token_env") or ""), "")


def matches(text: str, pattern: str | None) -> bool:
    """Signatur-Match: Regex-Suche; ohne Pattern passt jede Nachricht."""
    return pattern is None or re.search(pattern, text) is not None


def _valid_ts(value: object) -> bool:
    return isinstance(value, str) and SLACK_TS_RE.match(value) is not None


def _ts_float(value: object) -> float:
    """`ts` als Zahl für Vergleiche; ungültig → 0.0."""
    return float(value) if _valid_ts(value) else 0.0  # type: ignore[arg-type]


def max_pages(trig: dict) -> int:
    """Seitenlimit pro Channel und Poll (`max_pages`, validiert in specs)."""
    return int(float(trig.get("max_pages", config.SLACK_DEFAULT_MAX_PAGES)))  # "20"/"20.0" aus `vars`


def max_alert_chars(trig: dict) -> int:
    """Längenlimit für `alert_text` (`max_alert_chars`, validiert in specs)."""
    return int(float(trig.get("max_alert_chars", config.SLACK_DEFAULT_MAX_ALERT_CHARS)))  # "20"/"20.0" aus `vars`


def cap_text(text: str, limit: int) -> str:
    """`text` auf `limit` Zeichen kürzen, mit Marker samt Originallänge."""
    if len(text) <= limit:
        return text
    return text[:limit] + t("slack.truncation_marker", total=len(text))


def _is_alert(msg: dict) -> bool:
    return msg.get("subtype") in ACCEPTED_SUBTYPES and isinstance(msg.get("text"), str) and bool(msg["text"])


def validate_trigger(trig: dict, where: str) -> list[str]:
    """Spec-Validierung eines `on_slack`-Triggers."""
    errs = []
    if "token" in trig:
        errs.append(t("slack.on_slack_token_does_not", where=where))
    if not isinstance(trig.get("token_env"), str) or not trig["token_env"]:
        errs.append(t("slack.on_slack_needs_token_env", where=where))
    chans = trig.get("channels")
    if (
        not isinstance(chans, list)
        or not chans
        or not all(isinstance(c, str) and CHANNEL_ID_RE.match(c) for c in chans)
    ):
        errs.append(t("slack.on_slack_needs_channels_non", where=where))
    pat = trig.get("pattern")
    if pat is not None:
        try:
            if not isinstance(pat, str):
                raise TypeError
            re.compile(pat)
        except (re.error, TypeError):
            errs.append(t("slack.on_slack_pattern_must_be", where=where))
    return errs


def state_path(project: str, loop: str) -> Path:
    """State-Datei des Triggers (`state/slack/<projekt>__<loop>.json`)."""
    return config.STATE_DIR / "slack" / f"{project}__{loop}.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def poll(
    client: SlackClient,
    trig: dict,
    path: Path,
    now_ts: str,
    *,
    include_pending: bool = False,
    defer_seen: bool = False,
) -> list[Alert]:
    """Neue passende Alerts aller Whitelist-Channels, älteste zuerst.

    State pro Channel: `cursor` (höchster gesehener `ts`), `pending` (noch nicht
    erfolgreich geclaimte Alerts, nur mit `defer_seen`) und `seen` (gestartete
    `ts`). Erster Poll eines Channels setzt nur den Cursor auf `now_ts` (kein
    Backlog-Run). Mit `defer_seen` bleibt ein Treffer in `pending`, bis
    `mark_started` ihn übernimmt; `include_pending` liefert ihn bei jedem Poll
    erneut (verworfene Runs werden so wiederholt). Fehler eines Channels brechen
    die anderen nicht ab (Cursor bleibt dort stehen).

    Seitenlimit (`max_pages`): Slack liefert neueste zuerst. Wird das Limit
    erreicht, verarbeitet der Poll die gelieferten (neuesten) Nachrichten, lässt
    `cursor` stehen und merkt sich die Lücke (`gap_latest` = ältester gelieferter
    `ts`, `gap_high` = neuester). Folge-Polls lesen nur die Lücke
    (`cursor` < ts < `gap_latest`), bis sie vollständig ist; erst dann springt
    `cursor` auf `gap_high`. So geht keine Nachricht verloren. Der Slack-Abruf läuft
    außerhalb des State-Locks; die Ergebnisse werden in den frisch geladenen
    State gemischt, damit parallele `mark_started`-Updates nicht verloren gehen."""
    channels = list(dict.fromkeys(str(c) for c in trig.get("channels") or []))
    with _state_lock:
        state = _load(path)
        ranges: dict[str, tuple[str, str | None]] = {}
        for channel in channels:
            st = _channel_state(state, channel)
            if st["cursor"] is None:
                st["cursor"] = now_ts
            else:
                ranges[channel] = (st["cursor"], st.get("gap_latest"))
        _save(path, state)
    pages = max_pages(trig)
    fetched: dict[str, History] = {}
    for channel, (oldest, latest) in ranges.items():
        try:
            fetched[channel] = client.history(channel, oldest, latest=latest, max_pages=pages)
        except SlackError as e:  # Slack-Fehlercode, nie Request-Details
            print(f"[fabrik] on_slack Poll {channel} fehlgeschlagen: {e}")
        except Exception as e:
            print(f"[fabrik] on_slack Poll {channel} fehlgeschlagen: {type(e).__name__}")
    rules = _MatchRules(pattern=trig.get("pattern"), max_chars=max_alert_chars(trig), defer_seen=defer_seen)
    alerts: list[Alert] = []
    with _state_lock:
        state = _load(path)
        for channel in ranges:
            st = _channel_state(state, channel)
            pending = _pending_alerts(st, channel)
            if include_pending:
                alerts.extend(pending.values())
            if channel not in fetched:
                continue
            alerts.extend(_merge_history(st, channel, fetched[channel], pending, rules))
        _save(path, state)
    return alerts


def _channel_state(state: dict, channel: str) -> dict:
    """Channel-Eintrag im State (fehlende/defekte Felder normalisiert)."""
    st = state.get(channel)
    if not isinstance(st, dict):
        st = state[channel] = {}
    if st.get("cursor") is not None and not _valid_ts(st["cursor"]):
        st["cursor"] = None  # defekter Cursor → wie Erstpoll (kein Backlog-Run)
    st.setdefault("cursor", None)
    if st["cursor"] is None or not (_valid_ts(st.get("gap_latest")) and _valid_ts(st.get("gap_high"))):
        st.pop("gap_latest", None)  # Lücke nur gültig mit Cursor und beiden Grenzen
        st.pop("gap_high", None)
    for key in ("seen", "pending"):
        if not isinstance(st.get(key), list):
            st[key] = []
    return st


def _pending_alerts(st: dict, channel: str) -> dict[str, Alert]:
    return {
        item["ts"]: Alert(text=item["text"], channel=channel, ts=item["ts"])
        for item in st["pending"]
        if isinstance(item, dict) and _valid_ts(item.get("ts")) and isinstance(item.get("text"), str)
    }


@dataclass(frozen=True)
class _MatchRules:
    """Filter/Format eines Polls: Signatur, Textlimit, pending- oder seen-Modus."""

    pattern: str | None
    max_chars: int
    defer_seen: bool


def _merge_history(
    st: dict, channel: str, history: History, pending: dict[str, Alert], rules: _MatchRules
) -> list[Alert]:
    """Abgerufene Nachrichten in den Channel-State übernehmen; liefert die neuen Alerts.

    Vollständiger Abruf → `cursor` auf den neuesten bekannten `ts` (inkl. `gap_high`),
    Lücke erledigt. Seitenlimit erreicht → `cursor` bleibt, Lücke bis zum ältesten
    gelieferten `ts` wird gemerkt (siehe `poll`)."""
    msgs = sorted(
        (m for m in history.messages if isinstance(m, dict) and _valid_ts(m.get("ts"))),
        key=lambda m: _ts_float(m["ts"]),
    )
    alerts = [a for msg in msgs if (a := _accept(st, channel, msg, pending, rules)) is not None]
    st["seen"] = st["seen"][-SEEN_MAX:]
    newest = max([st["cursor"], st.get("gap_high"), *(m["ts"] for m in msgs)], key=_ts_float)
    if history.complete:
        st["cursor"] = newest
        st.pop("gap_latest", None)
        st.pop("gap_high", None)
    elif msgs:
        st["gap_latest"], st["gap_high"] = msgs[0]["ts"], newest
        log.warning(
            t("slack.on_slack_s_page_limit"),
            channel,
            st["cursor"],
            msgs[0]["ts"],
        )
    else:
        log.warning(t("slack.on_slack_s_page_limit_2"), channel)
    return alerts


def _accept(st: dict, channel: str, msg: dict, pending: dict[str, Alert], rules: _MatchRules) -> Alert | None:
    """Eine Nachricht prüfen und als Alert in `pending` bzw. `seen` vermerken (sonst None)."""
    ts = msg["ts"]
    if ts in st["seen"] or ts in pending or not _is_alert(msg) or not matches(msg["text"], rules.pattern):
        return None
    text = cap_text(msg["text"], rules.max_chars)
    if text != msg["text"]:
        log.debug(t("slack.on_slack_s_s_alert"), channel, ts, len(msg["text"]), rules.max_chars)
    alert = Alert(text=text, channel=channel, ts=ts)
    if rules.defer_seen:
        pending[ts] = alert
        st["pending"].append({"ts": ts, "text": text})
    else:
        st["seen"].append(ts)
    return alert


def mark_started(path: Path, alert: Alert) -> None:
    """Geclaimten Alert von `pending` nach `seen` (persistente Dedup) verschieben."""
    with _state_lock:
        state = _load(path)
        st = state.get(alert.channel)
        if not isinstance(st, dict):
            return
        st["pending"] = [
            item for item in st.get("pending", []) if not isinstance(item, dict) or item.get("ts") != alert.ts
        ]
        seen = list(st.get("seen", []))
        if alert.ts not in seen:
            seen.append(alert.ts)
        st["seen"] = seen[-SEEN_MAX:]
        _save(path, state)
