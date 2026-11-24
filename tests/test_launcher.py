"""Run-Launcher (Ports & Adapters): Port, In-Prozess-Adapter, Registry, Config.

Testet die Substrat-Schicht: dass der In-Prozess-Adapter das Port-Protokoll
erfüllt, Work korrekt ausführt (Pool vs. Vorrang-Thread), status/shutdown
verhalten, und dass die Registry + Config das Substrat auswählen. Die
Scheduling-Policy (Claim/Dedup/Vorrang) wird in den Engine-Tests abgedeckt.
"""

from __future__ import annotations

import threading

import pytest

from fabrikant import config, engine
from fabrikant.launcher import InProcessLauncher, RunLauncher, create_launcher, known_launchers
from fabrikant.launcher.registry import ADAPTERS


def _wait(flag: threading.Event, timeout: float = 2.0) -> bool:
    return flag.wait(timeout)


class TestPort:
    def test_inprocess_satisfies_protocol(self):
        launcher = InProcessLauncher(pool_factory=lambda: engine._WorkerPool(1), pool_or_none=lambda: None)
        assert isinstance(launcher, RunLauncher)
        assert launcher.name == "inprocess"

    def test_protocol_is_runtime_checkable(self):
        # Ein Objekt ohne die Port-Methoden erfüllt das Protokoll nicht.
        class _NotALauncher:
            name = "x"

        assert not isinstance(_NotALauncher(), RunLauncher)


class TestInProcess:
    def test_submit_runs_work_on_pool(self):
        pool = engine._WorkerPool(2)
        launcher = InProcessLauncher(pool_factory=lambda: pool, pool_or_none=lambda: pool)
        try:
            done = threading.Event()
            launcher.submit(done.set)
            assert _wait(done), "submit-Work wurde nicht ausgeführt"
        finally:
            pool.shutdown()

    def test_submit_priority_runs_work_on_thread(self):
        pool = engine._WorkerPool(1)
        launcher = InProcessLauncher(pool_factory=lambda: pool, pool_or_none=lambda: pool)
        try:
            done = threading.Event()
            launcher.submit_priority(done.set)
            assert _wait(done), "Vorrang-Work wurde nicht ausgeführt"
        finally:
            pool.shutdown()

    def test_status_without_pool_reports_configured_size(self, monkeypatch):
        monkeypatch.setenv("FABRIKANT_WORKERS", "7")
        launcher = InProcessLauncher(
            pool_factory=lambda: (_ for _ in ()).throw(AssertionError("sollte nicht laufen")),
            pool_or_none=lambda: None,
        )
        assert launcher.status() == {"workers": 7, "busy": 0, "queue": 0}

    def test_status_with_pool_reports_pool_stats(self):
        pool = engine._WorkerPool(3)
        launcher = InProcessLauncher(pool_factory=lambda: pool, pool_or_none=lambda: pool)
        try:
            assert launcher.status() == {"workers": 3, "busy": 0, "queue": 0}
        finally:
            pool.shutdown()

    def test_shutdown_drains_pool_idempotently(self):
        pool = engine._WorkerPool(2)
        launcher = InProcessLauncher(pool_factory=lambda: pool, pool_or_none=lambda: pool)
        launcher.shutdown()
        launcher.shutdown()  # idempotent
        # Nach dem Shutdown läuft eingereihtes Work nicht mehr.
        done = threading.Event()
        launcher.submit(done.set)
        assert not _wait(done, timeout=0.3)

    def test_shutdown_noop_without_pool(self):
        launcher = InProcessLauncher(pool_factory=lambda: None, pool_or_none=lambda: None)
        launcher.shutdown()  # darf nicht werfen


class TestRegistry:
    def test_inprocess_is_registered(self):
        assert "inprocess" in ADAPTERS
        assert "inprocess" in known_launchers()

    def test_create_inprocess(self):
        launcher = create_launcher("inprocess", pool_factory=lambda: None, pool_or_none=lambda: None)
        assert isinstance(launcher, InProcessLauncher)
        assert isinstance(launcher, RunLauncher)

    def test_create_unknown_lists_known(self):
        with pytest.raises(ValueError, match=r"unbekanntes Substrat 'nope'.*inprocess"):
            create_launcher("nope")


class TestConfig:
    def test_default_is_inprocess(self, monkeypatch):
        monkeypatch.delenv(config.LAUNCHER_ENV, raising=False)
        assert config.configured_launcher() == "inprocess"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(config.LAUNCHER_ENV, "k8s")
        assert config.configured_launcher() == "k8s"

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(config.LAUNCHER_ENV, "   ")
        assert config.configured_launcher() == "inprocess"


class TestEngineLauncher:
    def test_get_launcher_is_singleton(self, monkeypatch):
        monkeypatch.setattr(engine, "_launcher", None)
        monkeypatch.delenv(config.LAUNCHER_ENV, raising=False)
        a = engine._get_launcher()
        b = engine._get_launcher()
        assert a is b
        assert isinstance(a, InProcessLauncher)

    def test_get_launcher_respects_env(self, monkeypatch):
        monkeypatch.setattr(engine, "_launcher", None)
        monkeypatch.setenv(config.LAUNCHER_ENV, "inprocess")
        assert isinstance(engine._get_launcher(), InProcessLauncher)

    def test_get_launcher_unknown_substrate_raises(self, monkeypatch):
        monkeypatch.setattr(engine, "_launcher", None)
        monkeypatch.setenv(config.LAUNCHER_ENV, "does-not-exist")
        with pytest.raises(ValueError, match="unbekanntes Substrat"):
            engine._get_launcher()

    def test_submit_routes_through_pool(self, monkeypatch):
        """Engine-Submit nutzt den (ggf. injizierten) Pool — späte Bindung."""
        submitted: list = []

        class _FakePool:
            def submit(self, work):
                submitted.append(work)

        monkeypatch.setattr(engine, "_run_pool", _FakePool())
        engine._get_launcher().submit(lambda: None)
        assert len(submitted) == 1

    def test_pool_status_delegates_to_launcher_without_pool(self, monkeypatch):
        """pool_status() und launcher.status() stimmen überein (eine Quelle)."""
        monkeypatch.setattr(engine, "_run_pool", None)
        monkeypatch.setenv("FABRIKANT_WORKERS", "7")
        assert engine.pool_status() == engine._get_launcher().status()
        assert engine.pool_status() == {"workers": 7, "busy": 0, "queue": 0}

    def test_pool_status_delegates_to_launcher_with_pool(self, monkeypatch):
        pool = engine._WorkerPool(3)
        monkeypatch.setattr(engine, "_run_pool", pool)
        try:
            assert engine.pool_status() == engine._get_launcher().status()
            assert engine.pool_status() == {"workers": 3, "busy": 0, "queue": 0}
        finally:
            pool.shutdown()

    def test_submit_failure_does_not_leak_queued_trigger(self, monkeypatch):
        """Submit-Fehler (ungültige FABRIKANT_WORKERS) unterdrückt den Trigger nicht dauerhaft."""
        monkeypatch.setattr(engine, "_run_pool", None)
        monkeypatch.setattr(engine, "_queued_triggers", {})
        monkeypatch.setenv("FABRIKANT_WORKERS", "not-a-number")
        with pytest.raises(ValueError):
            engine._safe_run("p", "l", "cron")
        assert engine._queued_triggers == {}
        # Nach dem Fehler kann derselbe Trigger erneut eingereiht werden.
        monkeypatch.setenv("FABRIKANT_WORKERS", "2")
        submitted: list = []

        class _FakePool:
            def submit(self, work):
                submitted.append(work)

        monkeypatch.setattr(engine, "_run_pool", _FakePool())
        engine._safe_run("p", "l", "cron")
        assert len(submitted) == 1

    def test_unknown_substrate_does_not_leak_queued_trigger(self, monkeypatch):
        """Unbekanntes FABRIKANT_LAUNCHER unterdrückt den Trigger nicht dauerhaft."""
        monkeypatch.setattr(engine, "_launcher", None)
        monkeypatch.setattr(engine, "_queued_triggers", {})
        monkeypatch.setenv(config.LAUNCHER_ENV, "does-not-exist")
        with pytest.raises(ValueError, match="unbekanntes Substrat"):
            engine._safe_run("p", "l", "cron")
        assert engine._queued_triggers == {}
