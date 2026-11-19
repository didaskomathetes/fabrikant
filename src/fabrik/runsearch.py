"""Run-Historie: gefilterte, cursor-paginierte Suche über die Runs (Issue #163, ADR 0034).

Kein eigener Suchindex: die Suche läuft direkt auf dem Run-Store
(:meth:`~fabrik.runstore.RunStore.search`) — im SQL-Store ein indiziertes ``SELECT`` über die
Run-Tabellen, im JSON-Store ein Filter über die Records. Signatur und Semantik
(`search_runs` → `RunPage`, Cursor, Datumsgrenzen) sind der Vertrag für die UI/API. Der
Log-Text ist nicht durchsuchbar.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import TypedDict

from .costs import CostSummary, run_cost
from .i18n import t
from .runmodel import Run
from .runquery import RunQuery, SortKey, date_bounds, sort_key
from .runstore import STORE


class RunRow(TypedDict):
    """Eine Zeile der Suchergebnisse."""

    id: str
    project: str
    loop: str
    trigger: str
    status: str
    started: str
    finished: str
    note: str
    failure_kind: str  # "" bei nicht gescheiterten Runs (ADR 0035)
    verdict: str  # fachliches Ergebnis (runmodel.Run.verdict), "" ohne
    cost: CostSummary  # Verbrauch aus den Step-Usages (Issue #126)


class RunPage(TypedDict):
    """Eine Ergebnisseite; `next_cursor` ist None auf der letzten Seite."""

    runs: list[RunRow]
    next_cursor: str | None


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def encode_cursor(started: str, project: str, run_id: str) -> str:
    """(started, project, run_id) → opaker URL-sicherer Cursor; Gegenstück zu `decode_cursor`."""
    return base64.urlsafe_b64encode(json.dumps([started, project, run_id]).encode()).decode()


def decode_cursor(cursor: str) -> SortKey:
    """Cursor → (started, project, run_id); ValueError, wenn er nicht von `encode_cursor` stammen kann."""
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (binascii.Error, ValueError, TypeError) as e:
        raise ValueError(t("runsearch.invalid_cursor")) from e
    if not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(p, str) for p in parts):
        raise ValueError(t("runsearch.invalid_cursor"))
    started, project, run_id = parts
    return started, project, run_id


def _row(run: Run) -> RunRow:
    return {
        "id": run.id,
        "project": run.project,
        "loop": run.loop,
        "trigger": run.trigger,
        "status": run.status.value,
        "started": run.started,
        "finished": run.finished,
        "note": run.note,
        "failure_kind": run.failure_kind.value if run.failure_kind else "",
        "verdict": run.verdict,
        "cost": run_cost(run).totals.as_dict(),
    }


def search_runs(
    project: str | None = None,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    loop: str = "",
    cursor: str = "",
    limit: int = DEFAULT_PAGE_SIZE,
    failure_kind: str = "",
) -> RunPage:
    """Gefilterte, cursor-paginierte Runs (neueste zuerst).

    q: jedes Wort muss (case-insensitiv, als Teilstring) in Run-Id, Loop, Trigger, Status,
    Notiz oder einer Step-Summary vorkommen. date_from/date_to: ISO-Datum (`to` rein als
    Datum = inklusive ganzer Tag) oder -Zeit, Vergleich in UTC. status, failure_kind: kommagetrennt
    (failure_kind trifft nur gescheiterte Runs).
    → {"runs": [...], "next_cursor": str | None}. ValueError bei ungültigem Datum/Cursor.

    Records mit unbekanntem Status sind nicht im Modell darstellbar und werden daher
    aus den Ergebnissen ausgeschlossen (nicht nur Status-Filter-Verfehlung); sie sind
    nur über ``STORE.load_report()`` sichtbar (ADR 0034, tolerant laden)."""
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    lower, upper, upper_exclusive = date_bounds(date_from, date_to)
    query = RunQuery(
        project=project,
        words=tuple(w.lower() for w in re.findall(r"\w+", q)),
        lower=lower,
        upper=upper,
        upper_exclusive=upper_exclusive,
        statuses=frozenset(s for s in status.split(",") if s),
        loop=loop,
        failure_kinds=frozenset(k for k in failure_kind.split(",") if k),
        after=decode_cursor(cursor) if cursor else None,
        limit=limit,
    )
    hits = STORE.search(query)
    page = hits[:limit]
    next_cursor = encode_cursor(*sort_key(page[-1])) if len(hits) > limit else None
    return {"runs": [_row(r) for r in page], "next_cursor": next_cursor}
