"""KPI metrics: minimal counter registry + Prometheus text export.

Factory KPIs (framework-v1-draft.md §5) need an export point.
Deliberately minimal (stdlib, no prometheus_client dependency): counters with
label dimensions; `render()` returns Prometheus text format, scrapable via
`GET /api/metrics` (control plane).
"""

from __future__ import annotations

import math
import re
import threading

from .i18n import t

# Prometheus metric name: [a-zA-Z_:][a-zA-Z0-9_:]* (OpenMetrics exposition).
# Invalid names would make the entire text export invalid.
# `\Z` (not `$`): in Python `$` also matches before a trailing
# newline — "valid\n" would otherwise pass the check.
_METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")

# Prometheus label name: [a-zA-Z_][a-zA-Z0-9_]* (no ':' unlike the metric name).
# Invalid or duplicate label names produce invalid or ambiguous
# samples in the export.
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


def _escape_label_value(value: str) -> str:
    """Escape a label value for the Prometheus text format.

    The exposition format allows only backslash,
    quotes and newlines in label values in escaped form; anything else
    produces invalid samples.
    Order matters: first backslash, then quotes,
    then newline.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Counter:
    """Monotonically increasing counter with label dimensions.

    `labels` are the allowed label names (order = output order).
    Thread-safe (one lock per counter). Unknown/missing labels are treated
    as empty strings (stable time-series keys).
    """

    def __init__(self, name: str, help: str, labels: list[str]):
        # `register()` is public: invalid names, label schemas or
        # HELP texts with newlines would make the entire text export
        # invalid. All inputs are therefore validated here (at the only
        # construction point), not only when rendering.
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(t("metrics.counter_name_is_not_a", name=name))
        if "\n" in help:
            raise ValueError(t("metrics.help_text_of_must_not", name=name))
        seen: set[str] = set()
        for label in labels:
            if not _LABEL_NAME_RE.match(label):
                raise ValueError(t("metrics.counter_label_name_is_not", name=name, label=label))
            if label in seen:
                raise ValueError(t("metrics.counter_label_name_is_duplicated", name=name, label=label))
            seen.add(label)
        self.name = name
        self.help = help
        self.labels = list(labels)
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increases the counter by `amount` for the label combination.

        Prometheus counters are monotonic and finite: negative, NaN or
        infinite `amount` values are rejected with `ValueError`.
        """
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(t("metrics.counter_amount_must_be_finite", v1=self.name, amount=amount))
        key = tuple(str(labels.get(name, "")) for name in self.labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        """Current value of a label combination (0.0 if never incremented)."""
        key = tuple(str(labels.get(name, "")) for name in self.labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> str:
        """This counter as Prometheus text format (HELP/TYPE + samples).

        The HELP text is escaped per the exposition format (backslash →
        `\\`); raw backslashes would otherwise syntactically
        damage the line.
        """
        with self._lock:
            items = sorted(self._values.items())
        help_text = self.help.replace("\\", "\\\\")
        lines = [f"# HELP {self.name} {help_text}", f"# TYPE {self.name} counter"]
        for key, val in items:
            label_str = ",".join(
                f'{name}="{_escape_label_value(value)}"' for name, value in zip(self.labels, key, strict=True)
            )
            lines.append(f"{self.name}{{{label_str}}} {val}")
        return "\n".join(lines)


_counters: list[Counter] = []
_counters_guard = threading.Lock()


def register(counter: Counter) -> Counter:
    """Registers a counter for the global export (`render()`).

    Duplicate registration of the same name is rejected with `ValueError`:
    it would produce duplicate HELP/TYPE blocks and samples in the export.
    """
    with _counters_guard:
        if any(c.name == counter.name for c in _counters):
            raise ValueError(t("metrics.counter_is_already_registered", v1=counter.name))
        _counters.append(counter)
    return counter


def render() -> str:
    """All registered counters as Prometheus text format (one file)."""
    with _counters_guard:
        cs = list(_counters)
    return "\n".join(c.render() for c in cs) + "\n"


# KPIs (framework-v1-draft.md §5). Each counter is registered where the
# feature lives and incremented at the actual event — the export is
# thereby never empty and reflects the production state.
#
# Reaped runs: the scheduler dead-man's switch (scheduler._check_stale_runs)
# marks overdue 'running' runs as error. This is the current
# production reaping; the full reaper KPI `fabrik_reaped_jobs_total`
# (labels: tier, reason) follows with the reaper implementation (issue 21).
FABRIK_REAPED_RUNS_TOTAL = register(
    Counter("fabrik_reaped_runs_total", "Runs reaped by the Dead-Man's-Switch (stale runs marked as error)", ["reason"])
)
