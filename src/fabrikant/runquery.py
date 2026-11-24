"""Filter of a run search (issue #163, ADR 0034) — shared for JSON and SQL store.

:class:`RunQuery` is the validated, backend-neutral form of the search parameters from
:func:`fabrikant.runsearch.search_runs`. The JSON store filters in memory with it
(:meth:`RunQuery.matches`), the SQL store translates it into an indexed ``SELECT``.
Both sort by :func:`sort_key` descending (newest first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .i18n import t
from .runmodel import Run

SortKey = tuple[str, str, str]

# Normalisierte UTC-Zeit mit fester Breite: lexikalische = chronologische Ordnung (SQL-Spalte ``started_utc``).
UTC_SORT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def to_utc(value: str) -> datetime:
    """ISO date/time → aware UTC datetime; without offset UTC applies. ValueError on an invalid value."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def utc_sort_text(value: str) -> str | None:
    """ISO-Zeitpunkt → normalisierter UTC-Text (:data:`UTC_SORT_FORMAT`); None, wenn leer/unparsebar."""
    try:
        return to_utc(value).strftime(UTC_SORT_FORMAT) if value else None
    except ValueError:
        return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]+(Z|[+-]\d{2}:?\d{2})?)?$")


def _check_date(name: str, value: str) -> str:
    """Strict ISO check: date alone or date+time (incl. optional offset)."""
    try:
        if not _DATE_RE.match(value):
            raise ValueError(value)
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(t("runquery.invalid_date_for_expected_yyyy", name=name, value=value)) from e
    return value


def date_bounds(date_from: str, date_to: str) -> tuple[datetime | None, datetime | None, bool]:
    """→ (untere Grenze inkl., obere Grenze, obere exklusiv?). `to` als reines Datum = ganzer Tag."""
    lower = to_utc(_check_date("from", date_from)) if date_from else None
    if not date_to:
        return lower, None, False
    if len(_check_date("to", date_to)) == 10:
        return lower, to_utc(date_to) + timedelta(days=1), True
    return lower, to_utc(date_to), False


def sort_key(run: Run) -> SortKey:
    """Sort and cursor key of a run: (started, project, id)."""
    return (run.started, run.project, run.id)


@dataclass(frozen=True)
class RunQuery:
    """Validated search filters. Empty fields do not filter.

    words: lowercase search words; each must occur as a substring in id, loop, trigger,
    status, note or a step summary. lower/upper: UTC bounds on ``started``
    (``upper_exclusive``: ``<`` instead of ``<=``); a run without parseable ``started`` then drops out.
    failure_kinds hits only failed runs. after: cursor — only runs with smaller :func:`sort_key`.
    limit: page size; stores return up to ``limit + 1`` runs (the last one indicates a next page)."""

    project: str | None = None
    words: tuple[str, ...] = ()
    lower: datetime | None = None
    upper: datetime | None = None
    upper_exclusive: bool = False
    statuses: frozenset[str] = frozenset()  # RunStatus-Werte; unbekannte treffen nichts
    loop: str = ""
    failure_kinds: frozenset[str] = frozenset()  # FailureKind-Werte
    after: SortKey | None = None
    limit: int = 50

    def _in_range(self, run: Run) -> bool:
        if self.lower is None and self.upper is None:
            return True
        try:
            started = to_utc(run.started) if run.started else None
        except ValueError:
            started = None
        if started is None:
            return False
        if self.lower is not None and started < self.lower:
            return False
        if self.upper is None:
            return True
        return started < self.upper if self.upper_exclusive else started <= self.upper

    @staticmethod
    def _haystack(run: Run) -> str:
        parts = [run.id, run.loop, run.trigger, run.status.value, run.note]
        parts += [s.summary for s in run.steps]
        return "\n".join(parts).lower()

    def matches(self, run: Run) -> bool:
        """True if ``run`` satisfies all filters (reference semantics for every backend)."""
        if self.project and run.project != self.project:
            return False
        if self.loop and run.loop != self.loop:
            return False
        if self.statuses and run.status not in self.statuses:
            return False
        if self.failure_kinds and (run.failure_kind is None or run.failure_kind not in self.failure_kinds):
            return False
        if self.after is not None and not sort_key(run) < self.after:
            return False
        if not self._in_range(run):
            return False
        text = self._haystack(run) if self.words else ""
        return all(w in text for w in self.words)
