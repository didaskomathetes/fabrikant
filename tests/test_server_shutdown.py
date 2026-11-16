"""serve(): Ende per Ctrl+C/SIGTERM/SIGHUP beendet die Step-Prozessgruppen (procs)."""

from __future__ import annotations

import signal

import pytest

from fabrik import procs, server


def test_exit_on_signals_raises_system_exit(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))
    server._exit_on_signals()
    assert set(installed) == set(server.EXIT_SIGNALS)
    with pytest.raises(SystemExit) as exc:
        installed[signal.SIGTERM](signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM


class _InterruptedServer:
    def __init__(self, *_a, **_k):
        pass

    def serve_forever(self):
        raise KeyboardInterrupt


class _IdleScheduler:
    def start(self):
        pass


def test_serve_kills_process_groups_on_exit(monkeypatch):
    killed = []
    monkeypatch.setattr(server, "ThreadingHTTPServer", _InterruptedServer)
    monkeypatch.setattr(server, "Scheduler", _IdleScheduler)
    monkeypatch.setattr(server, "_exit_on_signals", lambda: None)
    monkeypatch.setattr(procs, "kill_all", lambda: killed.append(True) or 2)
    server.serve(port=0, host="127.0.0.1")
    assert killed == [True]
