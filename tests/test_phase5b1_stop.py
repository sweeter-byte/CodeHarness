"""Phase 5B.1 — honest stop()/start() lifecycle for cron and team wakeup.

join(timeout) only bounds the wait; it does not prove the thread exited.
These tests verify that when a delivery_handler is artificially blocked:

  - stop() timeout keeps thread references and Runtime state (cron store,
    handlers) instead of pretending the runtime is gone;
  - start() refuses to create a next generation while old threads live;
  - once the blocked handler is released and the thread really exits, a
    second stop() completes the cleanup (idempotent);
  - a fresh start() then works normally.

No test here calls a real LLM API. STOP_JOIN_TIMEOUT is shortened via
monkeypatch so no test waits the production 2 seconds.
"""

import threading
import time

import pytest

WAIT_TIMEOUT = 3.0  # generous bound for event waits; joins are patched short


def _wait_until(predicate, timeout=WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── Cron ───────────────────────────────────────────────────────


@pytest.fixture
def cron_env(monkeypatch, tmp_path):
    """Isolated cwd + short join timeout + clean cron module state."""
    from codeharness.scheduler import cron

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cron, "STOP_JOIN_TIMEOUT", 0.05)
    monkeypatch.setattr(
        cron, "_test_store_path", tmp_path / "jobs.json", raising=False
    )
    with cron._delivery_lock:
        cron._delivery_queue.clear()
    yield cron
    # Belt-and-braces: never leak threads/state into other tests.
    cron.RUNTIME_STOP.set()
    for thread in (cron._scheduler_thread, cron._processor_thread):
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=WAIT_TIMEOUT)
    cron._scheduler_thread = None
    cron._processor_thread = None
    cron._delivery_handler = None
    cron._status_handler = None
    cron._cron_store = None
    with cron._delivery_lock:
        cron._delivery_queue.clear()


def _start_cron_with_blocked_delivery(cron):
    """Start cron whose delivery_handler blocks; return (gate, entered)."""
    gate = threading.Event()
    entered = threading.Event()

    def blocking_handler(content):
        entered.set()
        gate.wait(timeout=WAIT_TIMEOUT)
        return True

    cron.start(
        delivery_handler=blocking_handler,
        store_path=cron._test_store_path,
        status_handler=lambda m: None,
    )
    # Every-minute job so the scheduler enqueues it on the next poll.
    cron.get_store().add("* * * * *", "blocked delivery", recurring=True, durable=False)
    assert _wait_until(entered.is_set), "delivery handler was never reached"
    return gate, entered


def test_cron_stop_timeout_retains_threads_and_state(cron_env):
    cron = cron_env
    gate, _ = _start_cron_with_blocked_delivery(cron)
    scheduler, processor = cron._scheduler_thread, cron._processor_thread
    store = cron.get_store()

    assert cron.stop() is False  # processor is still inside the handler

    # 1: processor thread reference is retained, not set to None.
    assert cron._processor_thread is processor
    assert processor.is_alive()
    assert cron._scheduler_thread is scheduler or scheduler.is_alive() is False
    # 2: runtime state the running thread still needs is NOT cleared.
    assert cron._cron_store is store
    assert cron._delivery_handler is not None
    assert cron.RUNTIME_STOP.is_set()

    gate.set()


def test_cron_start_refuses_while_previous_thread_alive(cron_env):
    cron = cron_env
    gate, _ = _start_cron_with_blocked_delivery(cron)
    processor = cron._processor_thread

    cron.stop()  # times out; processor still alive inside the handler
    assert processor.is_alive()

    # 3: no next generation while the old processor is still alive.
    with pytest.raises(RuntimeError, match="still stopping"):
        cron.start(
            delivery_handler=lambda c: True,
            store_path=cron._test_store_path,
            status_handler=None,
        )

    assert cron.RUNTIME_STOP.is_set()  # start() must not clear the stop signal
    assert cron._processor_thread is processor

    gate.set()


def test_cron_second_stop_after_thread_exits_completes_cleanup(cron_env):
    cron = cron_env
    gate, _ = _start_cron_with_blocked_delivery(cron)

    cron.stop()  # times out; state retained
    processor = cron._processor_thread
    assert processor is not None

    # 4: release the handler; the processor finishes its batch and exits.
    gate.set()
    assert _wait_until(lambda: not processor.is_alive())

    assert cron.stop() is True  # idempotent second call completes cleanup
    assert cron._scheduler_thread is None
    assert cron._processor_thread is None
    assert cron._delivery_handler is None
    assert cron._status_handler is None
    assert cron.get_store() is None

    # 5: a fresh runtime starts normally after the cleanup.
    cron.start(
        delivery_handler=lambda c: True,
        store_path=cron._test_store_path,
        status_handler=lambda m: None,
    )
    assert cron._scheduler_thread is not None
    assert cron._processor_thread is not None
    assert cron._scheduler_thread.is_alive()
    assert cron._processor_thread.is_alive()
    assert not cron.RUNTIME_STOP.is_set()
    assert cron.get_store() is not None

    cron.stop()
    assert cron._scheduler_thread is None
    assert cron._processor_thread is None


def test_cron_start_cleans_stale_dead_thread_references(cron_env):
    cron = cron_env
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join(timeout=WAIT_TIMEOUT)
    assert not dead.is_alive()

    cron._scheduler_thread = dead
    cron._processor_thread = dead

    cron.start(
        delivery_handler=lambda c: True,
        store_path=cron._test_store_path,
        status_handler=lambda m: None,
    )

    assert cron._scheduler_thread is not dead
    assert cron._processor_thread is not dead
    assert cron._scheduler_thread.is_alive()

    cron.stop()


# ── Team Wakeup ────────────────────────────────────────────────


@pytest.fixture
def wakeup_env(monkeypatch, tmp_path):
    """Isolated cwd (BUS mailbox dir) + short join timeout."""
    from codeharness.team import wakeup
    from codeharness.team.bus import BUS

    monkeypatch.setattr(BUS, "_mailbox_dir", BUS._mailbox_dir)
    BUS.configure(tmp_path / ".codeharness/runtime/team/mailboxes")

    monkeypatch.chdir(tmp_path)
    # Join timeout must exceed the poll interval (wait_for_messages blocks for
    # a full interval before noticing the stop event) so a clean stop joins
    # reliably, while still being far below the 3s gate block used by the
    # timeout-path tests.
    monkeypatch.setattr(wakeup, "STOP_JOIN_TIMEOUT", 0.3)
    monkeypatch.setattr(wakeup, "WAKEUP_POLL_INTERVAL", 0.05)
    yield wakeup
    wakeup._stop_event.set()
    thread = wakeup._thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=WAIT_TIMEOUT)
    wakeup._thread = None
    wakeup._status_handler = None


def _start_wakeup_with_blocked_delivery(wakeup):
    """Start wakeup whose delivery_handler blocks on a team event."""
    from codeharness.team.bus import BUS, LEADER

    gate = threading.Event()
    entered = threading.Event()

    def blocking_handler(content):
        entered.set()
        gate.wait(timeout=WAIT_TIMEOUT)
        return True

    wakeup.start(delivery_handler=blocking_handler, status_handler=lambda m: None)
    BUS.send("worker", LEADER, "wake the leader", msg_type="message")
    assert _wait_until(entered.is_set), "delivery handler was never reached"
    return gate


def test_wakeup_stop_timeout_retains_thread_reference(wakeup_env):
    wakeup = wakeup_env
    gate = _start_wakeup_with_blocked_delivery(wakeup)
    thread = wakeup._thread

    assert wakeup.stop() is False  # blocked inside the delivery_handler

    # 6: the thread reference survives; stop does not pretend it exited.
    assert wakeup._thread is thread
    assert thread.is_alive()
    assert wakeup._stop_event.is_set()
    assert wakeup._status_handler is not None

    gate.set()


def test_wakeup_start_refuses_while_previous_thread_alive(wakeup_env):
    wakeup = wakeup_env
    gate = _start_wakeup_with_blocked_delivery(wakeup)
    thread = wakeup._thread

    wakeup.stop()  # times out; thread still alive inside the handler
    assert thread.is_alive()

    # 7: no next generation; the stop event must NOT be cleared.
    with pytest.raises(RuntimeError, match="still stopping"):
        wakeup.start(delivery_handler=lambda c: True, status_handler=None)

    assert wakeup._stop_event.is_set()
    assert wakeup._thread is thread

    gate.set()


def test_wakeup_second_stop_after_thread_exits_completes_cleanup(wakeup_env):
    wakeup = wakeup_env
    gate = _start_wakeup_with_blocked_delivery(wakeup)

    wakeup.stop()  # times out; reference retained
    thread = wakeup._thread
    assert thread is not None

    # 8: release the handler; the thread exits and a second stop cleans up.
    gate.set()
    assert _wait_until(lambda: not thread.is_alive())

    assert wakeup.stop() is True
    assert wakeup._thread is None
    assert wakeup._status_handler is None

    # 9: a fresh wakeup starts successfully afterwards.
    wakeup.start(delivery_handler=lambda c: True, status_handler=lambda m: None)
    assert wakeup._thread is not None
    assert wakeup._thread.is_alive()
    assert not wakeup._stop_event.is_set()

    wakeup.stop()
    assert wakeup._thread is None


def test_wakeup_start_cleans_stale_dead_thread_reference(wakeup_env):
    wakeup = wakeup_env
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join(timeout=WAIT_TIMEOUT)
    assert not dead.is_alive()

    wakeup._thread = dead

    wakeup.start(delivery_handler=lambda c: True, status_handler=lambda m: None)

    assert wakeup._thread is not dead
    assert wakeup._thread.is_alive()

    wakeup.stop()
