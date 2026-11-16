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
            raise ValueError(f"Counter-Name {name!r} ist kein gültiger Prometheus-Metrikname")
        if "\n" in help:
            raise ValueError(f"HELP-Text von {name!r} darf keine Zeilenumbrüche enthalten")
        seen: set[str] = set()
        for label in labels:
            if not _LABEL_NAME_RE.match(label):
                raise ValueError(f"Counter {name!r}: Label-Name {label!r} ist kein gültiger Prometheus-Labelname")
            if label in seen:
                raise ValueError(f"Counter {name!r}: Label-Name {label!r} ist doppelt")
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
            raise ValueError(f"Counter {self.name}: amount muss endlich und >= 0 sein, got {amount!r}")
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
    """Registriert einen Counter für den globalen Export (`render()`).

    Doppelte Registrierung desselben Namens wird mit `ValueError` abgelehnt:
    Sie würde doppelte HELP/TYPE-Blöcke und Samples im Export erzeugen.
    """
    with _counters_guard:
        if any(c.name == counter.name for c in _counters):
            raise ValueError(f"Counter {counter.name!r} ist bereits registriert")
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
