"""Lifecycle robustness of the MCP server: startup recovery, worker draining, stdout hygiene."""

from __future__ import annotations

import io
import threading
import time

from fabrikant import mcp_server as mcp


def test_drain_waits_for_new_threads():
    done = threading.Event()
    t = threading.Thread(target=lambda: (time.sleep(0.2), done.set()), daemon=True)
    baseline = set(threading.enumerate())
    t.start()
    mcp._drain_workers(baseline, timeout_s=5)
    assert done.is_set()


def test_drain_gives_up_after_the_timeout():
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    baseline = set(threading.enumerate())
    t.start()
    started = time.monotonic()
    mcp._drain_workers(baseline, timeout_s=0.3)
    stop.set()
    assert time.monotonic() - started < 5


def test_startup_recovery_failure_is_contained(monkeypatch):
    monkeypatch.setattr(mcp, "reap_orphaned_runs", lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    err = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stderr", err)
    mcp._startup_recovery()  # must not raise
    assert "db locked" in err.getvalue()


def test_startup_recovery_reports_reaped_runs(monkeypatch):
    monkeypatch.setattr(mcp, "reap_orphaned_runs", lambda: ["r1", "r2"])
    err = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stderr", err)
    mcp._startup_recovery()
    assert "reaped orphaned run r1" in err.getvalue() and "r2" in err.getvalue()


def test_serve_stdio_recovers_serves_then_drains(monkeypatch):
    order: list[str] = []

    class FakeServer:
        def run(self, transport):
            order.append(f"run:{transport}")

    monkeypatch.setattr(mcp, "_startup_recovery", lambda: order.append("recover"))
    monkeypatch.setattr(mcp, "build_server", lambda options: FakeServer())
    monkeypatch.setattr(mcp, "_drain_workers", lambda baseline: order.append("drain"))
    mcp.serve_stdio(mcp.McpOptions())
    assert order == ["recover", "run:stdio", "drain"]


def test_main_serves_with_env_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp, "serve_stdio", lambda: seen.update(ran=True))
    mcp.main()
    assert seen == {"ran": True}
