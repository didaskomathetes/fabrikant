"""Worker pool: bounded concurrency (FABRIKANT_WORKERS) for local dev."""

import threading
import time

import fabrikant


def _concurrency_probe(n_tasks, sleep_s):
    """Run n_tasks on a pool; return the observed max concurrent executions."""
    max_concurrent = 0
    cur = 0
    lock = threading.Lock()

    def task():
        nonlocal max_concurrent, cur
        with lock:
            cur += 1
            max_concurrent = max(max_concurrent, cur)
        time.sleep(sleep_s)
        with lock:
            cur -= 1

    return task, lambda: max_concurrent


def test_one_worker_runs_serially():
    pool = fabrikant._WorkerPool(1)
    try:
        task, read = _concurrency_probe(5, 0.05)
        for _ in range(5):
            pool.submit(task)
        time.sleep(1.0)
        assert read() == 1
    finally:
        pool.shutdown()


def test_n_workers_bounds_concurrency():
    pool = fabrikant._WorkerPool(3)
    try:
        task, read = _concurrency_probe(6, 0.1)
        for _ in range(6):
            pool.submit(task)
        time.sleep(1.5)
        assert 2 <= read() <= 3
    finally:
        pool.shutdown()


def test_pool_size_floor_is_one():
    assert fabrikant._WorkerPool(0).size == 1
    assert fabrikant._WorkerPool(-5).size == 1


def test_get_run_pool_respects_env(monkeypatch):
    monkeypatch.setattr(fabrikant.engine, "_run_pool", None)
    monkeypatch.setenv("FABRIKANT_WORKERS", "2")
    pool = fabrikant._get_run_pool()
    assert pool.size == 2
    pool.shutdown()
    monkeypatch.setattr(fabrikant.engine, "_run_pool", None)


def test_get_run_pool_is_singleton(monkeypatch):
    monkeypatch.setattr(fabrikant.engine, "_run_pool", None)
    monkeypatch.setenv("FABRIKANT_WORKERS", "1")
    a = fabrikant._get_run_pool()
    b = fabrikant._get_run_pool()
    assert a is b
    a.shutdown()
    monkeypatch.setattr(fabrikant.engine, "_run_pool", None)
