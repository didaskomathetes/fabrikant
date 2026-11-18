"""Filter einer Run-Suche (Issue #163, ADR 0034) — gemeinsam für JSON- und SQL-Store.

:class:`RunQuery` ist die geprüfte, backend-neutrale Form der Suchparameter aus
:func:`fabrik.runsearch.search_runs`. Der JSON-Store filtert damit im Speicher
(:meth:`RunQuery.matches`), der SQL-Store übersetzt sie in ein indiziertes ``SELECT``.
Beide sortieren nach :func:`sort_key` absteigend (neueste zuerst).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .runmodel import Run

SortKey = tuple[str, str, str]

# Normalisierte UTC-Zeit mit fester Breite: lexikalische = chronologische Ordnung (SQL-Spalte ``started_utc``).
UTC_SORT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def to_utc(value: str) -> datetime:
    """ISO-Datum/-Zeit → aware UTC-datetime; ohne Offset gilt UTC. ValueError bei ungültigem Wert."""
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
    """Strikte ISO-Prüfung: Datum allein oder Datum+Zeit (inkl. optionalem Offset)."""
    try:
        if not _DATE_RE.match(value):
            raise ValueError(value)
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"ungültiges Datum für '{name}': {value!r} (erwartet YYYY-MM-DD[THH:MM:SS])") from e
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
    """Sortier- und Cursor-Schlüssel eines Runs: (started, project, id)."""
    return (run.started, run.project, run.id)


@dataclass(frozen=True)
class RunQuery:
    """Geprüfte Suchfilter. Leere Felder filtern nicht.

    words: kleingeschriebene Suchwörter; jedes muss als Teilstring in Id, Loop, Trigger,
    Status, Notiz oder einer Step-Summary vorkommen. lower/upper: UTC-Grenzen auf ``started``
    (``upper_exclusive``: ``<`` statt ``<=``); ein Run ohne parsebares ``started`` fällt dann raus.
    failure_kinds trifft nur gescheiterte Runs. after: Cursor — nur Runs mit kleinerem :func:`sort_key`.
    limit: Seitengröße; Stores liefern bis zu ``limit + 1`` Runs (der letzte zeigt eine Folgeseite an)."""

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
        """True, wenn ``run`` alle Filter erfüllt (Referenz-Semantik für jedes Backend)."""
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
