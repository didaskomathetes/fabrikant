"""KPI-Metriken: minimale Counter-Registry + Prometheus-Text-Export.

Factory-KPIs (framework-v1-draft.md §5) brauchen einen Export-Punkt.
Bewusst minimal (Stdlib, keine prometheus_client-Dependency): Counter mit
Label-Dimensionen; `render()` liefert Prometheus-Textformat, scrapebar via
`GET /api/metrics` (Control Plane).
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable

from .i18n import t

# Prometheus-Metrikname: [a-zA-Z_:][a-zA-Z0-9_:]* (OpenMetrics-Exposition).
# Ungültige Namen würden den gesamten Text-Export ungültig machen.
# `\Z` (nicht `$`): `$` matcht in Python auch vor einem abschließenden
# Newline — "valid\n" würde die Prüfung sonst passieren.
_METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")

# Prometheus-Labelname: [a-zA-Z_][a-zA-Z0-9_]* (kein ':' wie beim Metriknamen).
# Ungültige oder doppelte Label-Namen erzeugen ungültige bzw. mehrdeutige
# Samples im Export.
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


def _escape_label_value(value: str) -> str:
    """Escapen ein Label-Wert für das Prometheus-Textformat.

    Das Exposition-Format erlaubt in Label-Werten nur Backslash,
    Anführungszeichen und Zeilenumbrüche in escaped Form; alles andere
    erzeugt ungültige Samples.
    Reihenfolge ist wichtig: erst Backslash, dann Anführungszeichen,
    dann Zeilenumbruch.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_samples(
    name: str, help: str, kind: str, label_names: list[str], items: Iterable[tuple[tuple[str, ...], float]]
) -> str:
    """Eine Metrik-Familie (HELP/TYPE + Samples) als Prometheus-Textformat.

    HELP wird gemäß Expositionsformat escaped (Backslash → ``\\\\``); rohe Backslashes würden die
    Zeile syntaktisch beschädigen. Label-Werte werden ebenfalls escaped."""
    help_text = help.replace("\\", "\\\\")
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for key, val in items:
        label_str = ",".join(
            f'{label}="{_escape_label_value(value)}"' for label, value in zip(label_names, key, strict=True)
        )
        lines.append(f"{name}{{{label_str}}} {val}")
    return "\n".join(lines)


class Counter:
    """Monoton wachsender Counter mit Label-Dimensionen.

    `labels` sind die erlaubten Label-Namen (Reihenfolge = Output-Reihenfolge).
    Thread-sicher (ein Lock pro Counter). Unbekannte/fehlende Labels werden
    als leere Strings behandelt (stabile Zeitreihen-Keys).
    """

    def __init__(self, name: str, help: str, labels: list[str]):
        # `register()` ist öffentlich: ungültige Namen, Label-Schemas oder
        # HELP-Texte mit Zeilenumbruch würden den gesamten Text-Export
        # ungültig machen. Alle Eingaben werden daher hier (am einzigen
        # Konstruktionspunkt) validiert, nicht erst beim Rendern.
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
        """Erhöht den Counter um `amount` für die Label-Kombination.

        Prometheus-Counter sind monoton und endlich: negative, NaN- oder
        unendliche `amount`-Werte werden mit `ValueError` abgelehnt.
        """
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(t("metrics.counter_amount_must_be_finite", v1=self.name, amount=amount))
        key = tuple(str(labels.get(name, "")) for name in self.labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        """Aktueller Wert einer Label-Kombination (0.0, wenn nie inkrementiert)."""
        key = tuple(str(labels.get(name, "")) for name in self.labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> str:
        """Dieser Counter als Prometheus-Textformat (HELP/TYPE + Samples).

        Der HELP-Text wird gemäß Expositionsformat escaped (Backslash →
        `\\`); rohe Backslashes würden die Zeile sonst syntaktisch
        beschädigen.
        """
        with self._lock:
            items = sorted(self._values.items())
        return render_samples(self.name, self.help, "counter", self.labels, items)


_counters: list[Counter] = []
_counters_guard = threading.Lock()


def register(counter: Counter) -> Counter:
    """Registriert einen Counter für den globalen Export (`render()`).

    Doppelte Registrierung desselben Namens wird mit `ValueError` abgelehnt:
    Sie würde doppelte HELP/TYPE-Blöcke und Samples im Export erzeugen.
    """
    with _counters_guard:
        if any(c.name == counter.name for c in _counters):
            raise ValueError(t("metrics.counter_is_already_registered", v1=counter.name))
        _counters.append(counter)
    return counter


def render() -> str:
    """Alle registrierten Counter als Prometheus-Textformat (eine Datei)."""
    with _counters_guard:
        cs = list(_counters)
    return "\n".join(c.render() for c in cs) + "\n"


# KPIs (framework-v1-draft.md §5). Jeder Counter wird dort registriert, wo das
# Feature lebt, und beim tatsächlichen Ereignis inkrementiert — der Export ist
# dadurch nie leer und spiegelt den Produktionsstand wider.
#
# Reaped Runs: der Scheduler-Dead-Man's-Switch (scheduler._check_stale_runs)
# markiert überfällige 'running'-Runs als error. Das ist das derzeitige
# Produktions-Reaping; die volle Reaper-KPI `fabrik_reaped_jobs_total`
# (Labels: tier, reason) folgt mit der Reaper-Implementierung (Issue 21).
FABRIK_REAPED_RUNS_TOTAL = register(
    Counter("fabrik_reaped_runs_total", "Runs reaped by the Dead-Man's-Switch (stale runs marked as error)", ["reason"])
)
