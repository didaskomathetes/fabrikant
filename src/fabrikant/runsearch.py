"""Run history: filtered, cursor-paginated search over the runs (issue #163, ADR 0034).

No separate search index: the search runs directly on the run store
(:meth:`~fabrikant.runstore.RunStore.search`) — in the SQL store an indexed ``SELECT`` over the
run tables, in the JSON store a filter over the records. Signature and semantics
(`search_runs` → `RunPage`, cursor, date bounds) are the contract for the UI/API. The
log text is not searchable.
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
    """One row of the search results."""

    id: str
    project: str
    loop: str
    trigger: str
    status: str
    started: str
    finished: str
    note: str
    failure_kind: str  # "" for non-failed runs (ADR 0035)
    verdict: str  # business outcome (runmodel.Run.verdict), "" if none
    cost: CostSummary  # Consumption from the step usages (issue #126)


class RunPage(TypedDict):
    """A result page; `next_cursor` is None on the last page."""

    runs: list[RunRow]
    next_cursor: str | None


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def encode_cursor(started: str, project: str, run_id: str) -> str:
    """(started, project, run_id) → opaque URL-safe cursor; counterpart to `decode_cursor`."""
    return base64.urlsafe_b64encode(json.dumps([started, project, run_id]).encode()).decode()


def decode_cursor(cursor: str) -> SortKey:
    """Cursor → (started, project, run_id); ValueError if it cannot originate from `encode_cursor`."""
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
    """Filtered, cursor-paginated runs (newest first).

    q: every word must occur (case-insensitive, as substring) in run id, loop, trigger, status,
    note or a step summary. date_from/date_to: ISO date (`to` as a pure
    date = whole day inclusive) or time, compared in UTC. status, failure_kind: comma-separated
    (failure_kind hits only failed runs).
    → {"runs": [...], "next_cursor": str | None}. ValueError on an invalid date/cursor.

    Records with an unknown status cannot be represented in the model and are therefore
    excluded from the results (not just a status-filter miss); they are
    visible only via ``STORE.load_report()`` (ADR 0034, tolerant loading)."""
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
