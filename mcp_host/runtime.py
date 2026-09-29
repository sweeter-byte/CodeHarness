"""Shared async→sync runtime built on a single background event loop.

The MCP SDK v2 is fully async while the CodeHarness agent loop is fully
sync. One :class:`SyncMCPRuntime` (backed by a single ``anyio``
``BlockingPortal`` thread) is shared by every adapter — an adapter *uses*
the runtime but never owns its thread lifecycle.

Two primitives:

* :meth:`SyncMCPRuntime.run_sync` — run a coroutine to completion and
  return its result (blocking the caller).
* :meth:`SyncMCPRuntime.spawn_persistent` — enter an async context manager
  (e.g. ``Client(...)``) inside a background task that stays alive until
  :meth:`PersistentHandle.close` is called. This is what keeps an MCP
  connection (and its subprocess) alive between sync calls.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, TypeVar

import anyio
import anyio.from_thread

_T = TypeVar("_T")


class PersistentHandle:
    """Sync handle to an async context manager living on the runtime loop."""

    def __init__(self) -> None:
        self._future: Any = None       # concurrent.futures.Future for the task
        self._cancel: Any = None       # portal cancel function (last resort)
        self._portal: Any = None
        self._value: Any = None
        self._error: BaseException | None = None
        self._ready: Any = None
        self._stop: Any = None

    async def _run(
        self,
        factory: Callable[[], AbstractAsyncContextManager],
        *,
        task_status: Any,
    ) -> None:
        # Events are created before started(), so a sync caller can never
        # race close() against a half-initialized handle.
        self._ready = anyio.Event()
        self._stop = anyio.Event()
        task_status.started()
        try:
            async with factory() as value:
                self._value = value
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # surfaced to the waiting sync caller
            self._error = exc
            self._ready.set()
            raise

    def _start(self, portal: Any, factory: Callable[[], AbstractAsyncContextManager]) -> None:
        self._portal = portal
        # start_task blocks until task_status.started() — events exist on return.
        # It yields (future, task_status_value); the portal separately hands
        # back a cancel function used as the last-resort teardown path.
        self._future, cancel = portal.start_task(self._run, factory)
        self._cancel = cancel

    def result(self, timeout: float | None = None) -> Any:
        """Block until the context is entered; return its value or raise its error."""
        async def _await_ready() -> None:
            if timeout is None:
                await self._ready.wait()
            else:
                with anyio.fail_after(timeout):
                    await self._ready.wait()

        self._portal.call(_await_ready)
        if self._error is not None:
            self._consume_future_exception()
            raise self._error
        return self._value

    def _consume_future_exception(self) -> None:
        """Retrieve (and discard) the task exception so GC logs no warning.

        Enter-time errors are surfaced by result(); teardown errors must not
        break caller cleanup paths.
        """
        if self._future is not None and self._future.done() and not self._future.cancelled():
            self._future.exception()

    def close(self, timeout: float = 10.0) -> None:
        """Signal the context to exit and wait for graceful teardown.

        The background task owns the ``async with`` exit, so transport
        shutdown (for stdio: close stdin → wait → kill process tree) always
        runs to completion. If teardown overruns ``timeout`` the task is
        cancelled through the portal as a last resort.
        """
        if self._portal is None or self._future is None:
            return

        async def _signal_stop() -> None:
            if self._stop is not None:
                self._stop.set()

        try:
            self._portal.call(_signal_stop)
            done, _ = concurrent.futures.wait([self._future], timeout=timeout)
            if not done:
                self._cancel(timeout)
        except RuntimeError:
            pass  # portal already stopped; nothing we can do
        self._consume_future_exception()

    @property
    def alive(self) -> bool:
        return self._future is not None and not self._future.done()


class SyncMCPRuntime:
    """One shared BlockingPortal serving all adapters."""

    def __init__(self) -> None:
        self._portal: Any | None = None
        self._portal_cm: Any | None = None
        self._lock = threading.Lock()
        self._shutdown = False

    def _ensure_portal(self) -> Any:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("SyncMCPRuntime has been shut down")
            if self._portal is None:
                # start_blocking_portal() must be used as a context manager;
                # we enter it here and exit it in shutdown().
                self._portal_cm = anyio.from_thread.start_blocking_portal()
                self._portal = self._portal_cm.__enter__()
            return self._portal

    @property
    def running(self) -> bool:
        return self._portal is not None and not self._shutdown

    def run_sync(self, fn: Callable[..., Awaitable[_T]], *args: Any, **kwargs: Any) -> _T:
        """Run an async callable on the shared loop and block for its result."""
        portal = self._ensure_portal()
        return portal.call(fn, *args, **kwargs)

    def spawn_persistent(
        self, factory: Callable[[], AbstractAsyncContextManager]
    ) -> PersistentHandle:
        """Enter ``factory()``'s context manager on the shared loop."""
        portal = self._ensure_portal()
        handle = PersistentHandle()
        handle._start(portal, factory)
        return handle

    def shutdown(self) -> None:
        """Stop the background loop and join its thread. Idempotent.

        Adapters must be closed before calling this; any task still running
        is cancelled when the portal's task group exits.
        """
        with self._lock:
            self._shutdown = True
            cm, self._portal_cm = self._portal_cm, None
            self._portal = None
        if cm is not None:
            # The CM exit stops the portal and joins its thread.
            cm.__exit__(None, None, None)


_RUNTIME: SyncMCPRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> SyncMCPRuntime:
    """Process-wide shared runtime (lazy singleton)."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None or _RUNTIME._shutdown:
            _RUNTIME = SyncMCPRuntime()
        return _RUNTIME


def shutdown_runtime() -> None:
    """Shut down the shared runtime (call at process exit / test teardown)."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            _RUNTIME.shutdown()
            _RUNTIME = None
