"""Shared pytest fixtures for the Streamrip Web GUI suite.

Tests drive the app through its externally observable seams (HTTP API, the
injectable subprocess runner) and never shell out to the real `rip` binary.
"""
import threading
import time

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def clean_state():
    """Reset server-owned Active/History/queue and restore the real runner
    around every test so tests do not bleed into each other."""
    original_runner = app_module.run_rip
    app_module.store.clear()
    # Drain any leftover queued tasks.
    try:
        while True:
            app_module.download_queue.get_nowait()
            app_module.download_queue.task_done()
    except Exception:
        pass
    yield
    # Keep a harmless fast runner in place while we drain, so any worker that
    # was mid-flight or grabs a leftover task never shells out to real `rip`.
    app_module.run_rip = fake_runner(["teardown"], returncode=0)
    try:
        while True:
            app_module.download_queue.get_nowait()
            app_module.download_queue.task_done()
    except Exception:
        pass
    # Let workers settle so they are idle before the next test reconfigures things.
    time.sleep(0.05)
    app_module.run_rip = original_runner
    app_module.store.clear()


def fake_runner(lines, returncode=0):
    """Build a fake subprocess runner: yields each canned line, then returns the
    canned exit code via StopIteration.value (matching the real runner's shape)."""
    def runner(cmd):
        for line in lines:
            yield line
        return returncode
    return runner


def wait_for(predicate, timeout=5.0, interval=0.01):
    """Poll until predicate() is truthy or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class BlockingRunner:
    """A fake runner that, once a worker begins iterating it, blocks until
    released. Lets a test saturate all workers so it can prove that further
    submissions stay Queued rather than being silently dropped.

    Mirrors the real runner's generator shape: the body runs on first ``next``,
    not on the call, so a worker is held inside the run only after it has
    transitioned the Download to ``downloading``."""

    def __init__(self, returncode=0):
        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self.returncode = returncode

    def __call__(self, cmd):
        def gen():
            self.started.release()
            self.release.wait(timeout=10)
            yield "blocked output"
            return self.returncode
        return gen()

    def wait_started(self, count, timeout=5.0):
        deadline = time.time() + timeout
        acquired = 0
        while acquired < count and time.time() < deadline:
            if self.started.acquire(timeout=deadline - time.time()):
                acquired += 1
        return acquired == count
