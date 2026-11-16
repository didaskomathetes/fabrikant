"""Kindprozesse in eigener Prozessgruppe — Timeout und Server-Ende beenden den ganzen Baum.

`subprocess.run(..., timeout=…)` tötet bei Ablauf nur das direkte Kind (`sh`); was der
Befehl gestartet hat (Python-CLI, `git push`, Testläufe), läuft verwaist weiter. Hier
startet jeder Befehl mit `start_new_session=True` und wird per `killpg` als Gruppe beendet.

Eigene Session heißt auch: ein Ctrl+C im Terminal erreicht die Gruppe nicht mehr. Deshalb
merkt sich das Modul laufende Gruppen und `kill_all()` beendet sie beim Prozess-Ende
(`atexit`; `fabrik serve` übersetzt SIGTERM/SIGHUP in ein reguläres Ende).

Nebenwirkung des Imports: `atexit.register(kill_all)` läuft bei `import fabrik.procs` —
jeder Prozess (auch CLI-Kommandos, Tests) bekommt damit den Safety-Net-Handler. Er ist
idempotent und No-Op, solange keine Gruppe vorgemerkt ist; `fabrik serve` ruft `kill_all()`
zusätzlich explizit im `finally` auf.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Sequence

_live: set[subprocess.Popen] = set()
_lock = threading.Lock()


def track(proc: subprocess.Popen) -> None:
    """Gruppe von `proc` (mit `start_new_session=True` gestartet) für `kill_all` vormerken."""
    with _lock:
        _live.add(proc)


def untrack(proc: subprocess.Popen) -> None:
    with _lock:
        _live.discard(proc)


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL an die Prozessgruppe von `proc`; No-Op, wenn sie schon weg ist.

    Bewusst SIGKILL (nicht SIGTERM): die Gruppen sind Testläufe, `git`- und Agent-CLIs —
    ein geordnetes Herunterfahren bringt nichts, und ein ignorierendes Kind würde das
    Ende des Servers/Steps sonst blockieren."""
    with contextlib.suppress(OSError):  # ProcessLookupError: Gruppe schon beendet
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def kill_all() -> int:
    """Alle vorgemerkten, noch laufenden Gruppen beenden. Gibt die Anzahl zurück."""
    with _lock:
        procs = [p for p in _live if p.poll() is None]
        _live.clear()
    for proc in procs:
        kill_group(proc)
    return len(procs)


def run_group(argv: Sequence[str], *, timeout: float, **kwargs) -> subprocess.CompletedProcess[str]:
    """Wie `subprocess.run(argv, capture_output=True, text=True, timeout=timeout)`, aber in
    eigener Prozessgruppe: bei Timeout (oder Abbruch) stirbt die ganze Gruppe, danach wird
    `subprocess.TimeoutExpired` (mit dem bis dahin gelesenen Output) weitergereicht."""
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **kwargs,
    ) as proc:
        track(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:  # Timeout, KeyboardInterrupt, …: keine Waisen zurücklassen
            kill_group(proc)
            proc.wait()
            raise
        finally:
            untrack(proc)
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


atexit.register(kill_all)
