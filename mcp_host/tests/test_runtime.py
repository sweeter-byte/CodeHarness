"""Unit tests for mcp_host.runtime — shared portal & persistent handles."""

import time
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import pytest


def test_run_sync_returns_result(runtime):
    async def add(a, b):
        await anyio.lowlevel.checkpoint()
        return a + b

    assert runtime.run_sync(add, 1, 2) == 3
    assert runtime.running


def test_run_sync_propagates_exception(runtime):
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        runtime.run_sync(boom)


def test_spawn_persistent_lifecycle(runtime):
    events = []

    @asynccontextmanager
    async def cm():
        events.append("enter")
        yield {"ok": True}
        events.append("exit")

    handle = runtime.spawn_persistent(cm)
    try:
        assert handle.result(timeout=5) == {"ok": True}
        assert handle.alive
        assert events == ["enter"]
    finally:
        handle.close(timeout=5)
    assert events == ["enter", "exit"]
    assert not handle.alive


def test_spawn_persistent_surfaces_enter_error(runtime):
    @asynccontextmanager
    async def cm():
        raise OSError("spawn failed")
        yield  # pragma: no cover

    handle = runtime.spawn_persistent(cm)
    with pytest.raises(OSError, match="spawn failed"):
        handle.result(timeout=5)
    handle.close(timeout=5)  # must not hang or raise


def test_close_is_idempotent_and_safe_after_shutdown(runtime):
    @asynccontextmanager
    async def cm():
        yield "v"

    handle = runtime.spawn_persistent(cm)
    handle.result(timeout=5)
    handle.close(timeout=5)
    handle.close(timeout=5)  # idempotent
    runtime.shutdown()
    handle.close(timeout=1)  # portal gone — swallowed


def test_shutdown_rejects_new_work(runtime):
    runtime.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        runtime.run_sync(anyio.sleep, 0)
    assert not runtime.running


def test_close_waits_for_slow_teardown(runtime):
    @asynccontextmanager
    async def cm():
        yield "v"
        await anyio.sleep(0.3)  # graceful teardown takes time

    handle = runtime.spawn_persistent(cm)
    handle.result(timeout=5)
    start = time.monotonic()
    handle.close(timeout=5)
    assert time.monotonic() - start >= 0.25  # waited for teardown


def test_close_timeout_falls_back_to_future_cancel(runtime):
    # Regression: teardown that overruns the close deadline must fall back to
    # future.cancel(). The old code called a None "_cancel" (the misread second
    # element of start_task's return), raising
    # "TypeError: 'NoneType' object is not callable" and masking the real error.
    @asynccontextmanager
    async def slow_cm():
        try:
            yield "v"
        finally:
            await anyio.sleep(1.0)  # __aexit__ overruns the timeout below

    handle = runtime.spawn_persistent(slow_cm)
    handle.result(timeout=5)                 # (a) normal enter
    start = time.monotonic()
    handle.close(timeout=0.2)                # (d)+(e) must NOT raise
    elapsed = time.monotonic() - start
    assert elapsed < 0.9                     # returned via cancel fallback, not full teardown
    handle.close(timeout=0.2)                # (c) still idempotent after the fallback
