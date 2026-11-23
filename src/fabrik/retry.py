"""Retry-Policy (`retry: {max, backoff, on, max_wait}`) nach ADR 0036 (Run-Retry).

Geteilt, damit Relay-Tasks (ADR 0037) und der Run-Retry (#290) dieselbe Semantik haben:
- `max`: Anzahl **zusätzlicher** Versuche nach dem ersten Run (`max: 3` → höchstens 4 Runs);
  `max: 0` = kein Retry.
- `backoff`: Wartezeit vor Versuch 1..n; der letzte Wert gilt für alle weiteren.
- `on`: Fehlerklassen (ADR 0035), die wiederholt werden dürfen — nur `interrupted`/`transient`.
  `unknown` und Runs ohne Klasse werden nie automatisch wiederholt.
- `max_wait`: Obergrenze für ein gemeldetes `retry_after` (z. B. Reset eines Rate-Limits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .i18n import t
from .runmodel import FailureKind

RETRYABLE_KINDS = frozenset({FailureKind.INTERRUPTED, FailureKind.TRANSIENT})
DEFAULT_MAX = 3
DEFAULT_BACKOFF = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
DEFAULT_MAX_WAIT = timedelta(hours=24)
_KEYS = frozenset({"max", "backoff", "on", "max_wait"})
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: object) -> timedelta:
    """`45s`, `5m`, `2h`, `1d` → timedelta (> 0); ValueError sonst."""
    m = _DURATION_RE.match(str(raw).strip())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(t("retry.duration_like_5m_2h_expected", raw=raw))
    return timedelta(seconds=int(m.group(1)) * _UNIT_S[m.group(2)])


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for unit in ("d", "h", "m"):
        if seconds % _UNIT_S[unit] == 0:
            return f"{seconds // _UNIT_S[unit]}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True)
class RetryPolicy:
    max: int = DEFAULT_MAX
    backoff: tuple[timedelta, ...] = DEFAULT_BACKOFF
    on: frozenset[FailureKind] = RETRYABLE_KINDS
    max_wait: timedelta = DEFAULT_MAX_WAIT

    def allows(self, kind: FailureKind | None) -> bool:
        return (kind or FailureKind.UNKNOWN) in self.on

    def delay(self, attempt: int) -> timedelta:
        """Wartezeit vor Versuch `attempt` (1-basiert)."""
        return self.backoff[min(attempt, len(self.backoff)) - 1]

    def next_attempt_at(self, attempt: int, finished: datetime, retry_after: datetime | None = None) -> datetime:
        """Frühester Start von Versuch `attempt`: Backoff ab Ende des vorherigen Runs; ein gemeldetes
        `retry_after` (auf `max_wait` gekappt) verschiebt ihn nach hinten (ADR 0036, Entscheidung 1)."""
        due = finished + self.delay(attempt)
        if retry_after is None:
            return due
        return max(due, min(retry_after, finished + self.max_wait))

    @classmethod
    def parse(cls, raw: Any, where: str, errs: list[str]) -> RetryPolicy | None:
        """`retry:`-Mapping → Policy; Fehler landen in `errs`. None = kein Retry."""
        if raw is None:
            return None
        if isinstance(raw, dict) and True in raw and "on" not in raw:
            # PyYAML (YAML 1.1) liest den unquotierten Key `on` als bool True.
            raw = {("on" if k is True else k): v for k, v in raw.items()}
        if not isinstance(raw, dict) or set(raw) - _KEYS:
            errs.append(t("retry.mapping_with_expected", where=where, v2="/".join(sorted(_KEYS))))
            return None
        n = raw.get("max", DEFAULT_MAX)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            errs.append(t("retry.max_integer_0_expected_is", where=where, n=n))
            n = DEFAULT_MAX
        backoff: tuple[timedelta, ...] = DEFAULT_BACKOFF
        if "backoff" in raw:
            values = raw["backoff"]
            if not isinstance(values, list) or not values:
                errs.append(t("retry.backoff_non_empty_list_of", where=where))
            else:
                try:
                    backoff = tuple(parse_duration(v) for v in values)
                except ValueError as e:
                    errs.append(f"{where}.backoff: {e}")
        max_wait = DEFAULT_MAX_WAIT
        if "max_wait" in raw:
            try:
                max_wait = parse_duration(raw["max_wait"])
            except ValueError as e:
                errs.append(f"{where}.max_wait: {e}")
        on = raw.get("on", sorted(RETRYABLE_KINDS))
        if not isinstance(on, list) or not on or any(k not in RETRYABLE_KINDS for k in on):
            errs.append(t("retry.on_non_empty_subset_of", where=where, v2=sorted(RETRYABLE_KINDS), on=on))
            on = sorted(RETRYABLE_KINDS)
        if n == 0:
            return None
        return cls(max=n, backoff=backoff, on=frozenset(FailureKind(k) for k in on), max_wait=max_wait)
