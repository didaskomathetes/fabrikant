"""Failure-aware excerpt of a failed step's output for the run log.

The run record keeps the full output; the log only gets an excerpt so it stays readable. A plain
tail is not enough: a test step prints package installations *after* the lines that matter (seen
in practice: the last 10 lines were the `uv sync` package list, the segfault was 300 lines
earlier). :func:`failure_excerpt` therefore keeps a window around every failure marker (pytest
results, tracebacks, crashes, verdict lines of our own scripts) plus the tail, and always says how
much was left out — the excerpt never truncates silently. :func:`cap_output` caps over-long output
for the record the same way: windows first, so the excerpt can still find them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import accumulate

from . import config
from .i18n import t

MAX_LINES = 120  # upper bound of the excerpt in the log (windows + tail)
TAIL_LINES = 15
BEFORE = 2  # context lines before a marker
AFTER = 20  # context lines after a marker (crash dump or assertion follow it)

# Lines that carry the cause. Deliberately narrow: every hit costs log lines.
FAILURE_MARKERS = re.compile(
    r"^(?:FAILED|ERROR)\b"  # pytest short summary
    r"|^E {3}"  # pytest assertion
    r"|^=+ .*(?:FAILURES|ERRORS|short test summary).* =+$"
    r"|Fatal Python error"
    r"|^Traceback \(most recent call last\)"
    r"|^rot: "  # verdict of scripts/ci-matrix-pytest.py
    r"|^error(?:\[\w+\])?:"  # uv, ruff, compilers
    r"|\b\d+ (?:failed|errors?)\b",  # pytest summary with failures
)


@dataclass(frozen=True)
class Excerpt:
    """Excerpt lines (including gap markers ``… (N lines)``) plus total and omitted lines."""

    lines: tuple[str, ...]
    total: int
    omitted: int

    def render(self) -> str:
        """Excerpt as text; when cut, with a note where the full output is."""
        if not self.omitted:
            return "\n".join(self.lines)
        note = t("excerpt.note", shown=self.total - self.omitted, total=self.total)
        return "\n".join((*self.lines, note))


def _windows(lines: list[str]) -> list[tuple[int, int]]:
    """Merged ``[start, end)`` ranges around the failure markers."""
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not FAILURE_MARKERS.search(line):
            continue
        start, end = max(0, i - BEFORE), min(len(lines), i + AFTER + 1)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


def _within_budget(spans: list[tuple[int, int]], budget: int) -> list[tuple[int, int]]:
    """Ranges in order until the budget (lines or characters) is used up; the last one is cut if needed."""
    kept: list[tuple[int, int]] = []
    for start, end in spans:
        if budget <= 0:
            break
        end = min(end, start + budget)
        kept.append((start, end))
        budget -= end - start
    return kept


def failure_excerpt(output: str, max_lines: int = MAX_LINES, tail: int = TAIL_LINES) -> Excerpt:
    """Excerpt of ``output``: windows around failure markers (in order, up to the budget) plus the
    last ``tail`` lines. Short outputs come back whole; gaps are marked ``… (N lines)``."""
    lines = output.strip("\n").splitlines()
    total = len(lines)
    if total <= max_lines:
        return Excerpt(tuple(lines), total, 0)
    tail_start = total - tail
    marked = [(s, min(e, tail_start)) for s, e in _windows(lines) if s < tail_start]
    spans = [*_within_budget(marked, max_lines - tail), (tail_start, total)]
    out: list[str] = []
    pos = shown = 0
    for start, end in spans:
        if start > pos:
            out.append(t("excerpt.gap", n=start - pos))
        out.extend(lines[start:end])
        shown += end - start
        pos = end
    return Excerpt(tuple(out), total, total - shown)


def failure_reason(output: str) -> str:
    """Last line with a failure marker (stripped) — the shortest reason for the summary; empty if none."""
    return next((line.strip() for line in reversed(output.splitlines()) if FAILURE_MARKERS.search(line)), "")


def cap_output(output: str | None, limit: int = config.MAX_STEP_OUTPUT) -> str:
    """Caps a failed step's output to ``limit`` characters for the record: the windows around failure
    markers (in order, at most half of ``limit``) plus the start and the end, with the number of omitted
    characters at each gap. The windows are picked *before* the cut — a marker in the middle would
    otherwise be lost for the record and for :func:`failure_excerpt`."""
    if not output or len(output) <= limit:
        return output or ""
    lines = output.splitlines(keepends=True)
    offsets = [0, *accumulate(map(len, lines))]
    chars = [(offsets[s], offsets[e]) for s, e in _windows(lines)]
    windows = _within_budget(chars, limit // 2)
    rest = limit - sum(e - s for s, e in windows)
    head = rest // 2
    spans: list[tuple[int, int]] = []
    for start, end in sorted([(0, head), *windows, (len(output) - (rest - head), len(output))]):
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            parts.append(f"\n{t('context.omitted_middle', n=start - pos)}\n")
        parts.append(output[start:end])
        pos = end
    return "".join(parts)
