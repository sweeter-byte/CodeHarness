"""Workflow Runtime concurrency, usage, cancellation, and shutdown regression tests."""

import json
import threading
import time
from pathlib import Path

import pytest

from codeharness.hooks import new_session_stats
from codeharness.workflow.definition import (
    AgentStep,
    ParallelStep,
    Phase,
    WorkflowConfig,
    WorkflowDefinition,
)
from codeharness.workflow.events import WorkflowEventBus
from codeharness.workflow.registry import WorkflowRegistry
from codeharness.workflow.runtime import (
    RunHandle,
    WorkflowBudgetExceeded,
    WorkflowContext,
    WorkflowRuntime,
)
from codeharness.workflow.state import RunSnapshot, RunStatus, WorkflowStateStore


WAIT_TIMEOUT = 3.0


class _SlowInt(int):
    """Make an unlocked read/modify/write race deterministic."""

    def __add__(self, other):
        time.sleep(0.03)
        return _SlowInt(int(self) + int(other))


class _FakeAgent:
    def __init__(
        self,
        *,
        local_tokens=0,
        aggregate_tokens=None,
        barrier=None,
        entered=None,
        release=None,
        error=None,
    ):
        self.local_stats = new_session_stats()
        self.local_stats["total_tokens"] = local_tokens
        self.session_stats = new_session_stats()
        self.session_stats["total_tokens"] = (
            local_tokens if aggregate_tokens is None else aggregate_tokens
        )
        self._barrier = barrier
        self._entered = entered
        self._release = release
        self._error = error

    def agent_loop(self, _messages):
        if self._entered is not None:
            self._entered.set()
        if self._barrier is not None:
            self._barrier.wait(timeout=WAIT_TIMEOUT)
        if self._release is not None:
            assert self._release.wait(timeout=WAIT_TIMEOUT)
        if self._error is not None:
            raise self._error
        return "done"


def _agent_step(label):
    return AgentStep(
        label=label,
        prompt_template=label,
        output_key=f"{label}_output",
    )


def _parallel_definition(name, branch_count, *, max_agent_calls=50):
    branches = [_agent_step(f"agent-{index}") for index in range(branch_count)]
    return WorkflowDefinition(
        name=name,
        description=name,
        phases=[
            Phase(
                name="phase",
                steps=[ParallelStep(label="parallel", branches=branches, output_key="out")],
            )
        ],
        config=WorkflowConfig(
            max_concurrency=branch_count,
            max_agent_calls=max_agent_calls,
        ),
    )


def _runtime(tmp_path, definition=None):
    registry = WorkflowRegistry()
    if definition is not None:
        registry.register(definition)
    return WorkflowRuntime(
        registry=registry,
        state_store=WorkflowStateStore(tmp_path / "state"),
        event_bus=WorkflowEventBus(),
    )


def _direct_parallel_runtime(tmp_path, branch_count, factory, *, max_agent_calls=50):
    definition = _parallel_definition(
        "parallel-test", branch_count, max_agent_calls=max_agent_calls
    )
    runtime = _runtime(tmp_path, definition)
    runtime.set_agent_factory(factory)
    snapshot = RunSnapshot(
        run_id="run-direct",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.RUNNING.value,
    )
    runtime._store.save_snapshot(snapshot)
    handle = RunHandle("run-direct", threading.current_thread(), snapshot)
    with runtime._lock:
        runtime._runs[handle.run_id] = handle
    parallel = definition.phases[0].steps[0]
    return runtime, definition, parallel, snapshot


def _execute_direct_parallel(runtime, definition, parallel, snapshot):
    return runtime._execute_parallel_step(
        parallel,
        WorkflowContext({}),
        snapshot,
        {},
        definition.config,
        "phase",
        snapshot.run_id,
        definition,
        0,
        time.time() + WAIT_TIMEOUT,
    )


def test_parallel_agent_call_counter_has_no_lost_update(tmp_path):
    branch_count = 4
    barrier = threading.Barrier(branch_count)
    runtime, definition, parallel, snapshot = _direct_parallel_runtime(
        tmp_path,
        branch_count,
        lambda **_kwargs: _FakeAgent(barrier=barrier),
    )
    snapshot.agent_calls_used = _SlowInt(0)

    _execute_direct_parallel(runtime, definition, parallel, snapshot)

    assert snapshot.agent_calls_used == branch_count


def test_max_agent_calls_reservation_is_atomic_under_parallelism(tmp_path):
    starts = []
    starts_lock = threading.Lock()
    runtime, definition, parallel, snapshot = _direct_parallel_runtime(
        tmp_path, 4, lambda **_kwargs: _FakeAgent(), max_agent_calls=2
    )

    def factory(**_kwargs):
        class Agent(_FakeAgent):
            def agent_loop(self, messages):
                with starts_lock:
                    starts.append(self)
                time.sleep(0.08)
                return "done"

        return Agent()

    runtime.set_agent_factory(factory)

    with pytest.raises(WorkflowBudgetExceeded):
        _execute_direct_parallel(runtime, definition, parallel, snapshot)

    assert len(starts) == 2
    assert snapshot.agent_calls_used == 2


def test_parallel_token_accounting_has_no_lost_update(tmp_path):
    totals = iter([100, 200, 300])
    factory_lock = threading.Lock()
    barrier = threading.Barrier(3)

    def factory(**_kwargs):
        with factory_lock:
            tokens = next(totals)
        return _FakeAgent(local_tokens=tokens, barrier=barrier)

    runtime, definition, parallel, snapshot = _direct_parallel_runtime(
        tmp_path, 3, factory
    )
    snapshot.tokens_used = _SlowInt(0)

    _execute_direct_parallel(runtime, definition, parallel, snapshot)

    assert snapshot.tokens_used == 600


def test_workflow_accounts_agent_local_usage_not_runtime_aggregate(tmp_path):
    runtime, definition, parallel, snapshot = _direct_parallel_runtime(
        tmp_path,
        1,
        lambda **_kwargs: _FakeAgent(local_tokens=200, aggregate_tokens=10_200),
    )

    _execute_direct_parallel(runtime, definition, parallel, snapshot)

    assert snapshot.tokens_used == 200


def test_same_run_concurrent_snapshot_saves_do_not_race_on_tmp_file(
    monkeypatch, tmp_path
):
    store = WorkflowStateStore(tmp_path / "state")
    snapshot = RunSnapshot(run_id="run-save", workflow_name="save", inputs={})
    original_write_text = Path.write_text
    first_write = threading.Event()
    second_write = threading.Event()
    count_lock = threading.Lock()
    write_count = 0

    def coordinated_write(path, *args, **kwargs):
        nonlocal write_count
        result = original_write_text(path, *args, **kwargs)
        if path.name == "snapshot.json.tmp":
            with count_lock:
                write_count += 1
                current = write_count
            if current == 1:
                first_write.set()
                second_write.wait(timeout=0.2)
            elif current == 2:
                second_write.set()
        return result

    monkeypatch.setattr(Path, "write_text", coordinated_write)
    errors = []

    def save(*, wait_for_first=False):
        try:
            if wait_for_first:
                assert first_write.wait(timeout=WAIT_TIMEOUT)
            store.save_snapshot(snapshot)
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=save)
    second = threading.Thread(target=lambda: save(wait_for_first=True))
    first.start()
    second.start()
    first.join(timeout=WAIT_TIMEOUT)
    second.join(timeout=WAIT_TIMEOUT)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    data = json.loads((tmp_path / "state/run-save/snapshot.json").read_text())
    assert data["run_id"] == "run-save"


def test_shutdown_timeout_retains_live_handle_for_retry(tmp_path):
    definition = WorkflowDefinition(
        name="blocking-shutdown",
        description="blocking shutdown",
        phases=[Phase(name="phase", steps=[_agent_step("block")])],
    )
    runtime = _runtime(tmp_path, definition)
    entered = threading.Event()
    release = threading.Event()
    runtime.set_agent_factory(
        lambda **_kwargs: _FakeAgent(entered=entered, release=release)
    )
    run_id, error = runtime.start(definition.name, {})
    assert error is None
    assert entered.wait(timeout=WAIT_TIMEOUT)

    assert runtime.shutdown_all(timeout=0.01) is False
    with runtime._lock:
        handle = runtime._runs.get(run_id)
    assert handle is not None
    assert handle.thread.is_alive()

    release.set()
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime.shutdown_all(timeout=WAIT_TIMEOUT) is True
    with runtime._lock:
        assert run_id not in runtime._runs


def test_cancelled_worker_cannot_overwrite_terminal_state(tmp_path):
    definition = WorkflowDefinition(
        name="blocking-cancel",
        description="blocking cancel",
        phases=[Phase(name="phase", steps=[_agent_step("block")])],
    )
    runtime = _runtime(tmp_path, definition)
    entered = threading.Event()
    release = threading.Event()
    runtime.set_agent_factory(
        lambda **_kwargs: _FakeAgent(entered=entered, release=release)
    )
    run_id, error = runtime.start(definition.name, {})
    assert error is None
    assert entered.wait(timeout=WAIT_TIMEOUT)
    with runtime._lock:
        handle = runtime._runs[run_id]

    runtime.cancel(run_id)
    release.set()
    handle.thread.join(timeout=WAIT_TIMEOUT)

    snapshot = runtime._store.load_snapshot(run_id)
    assert snapshot.status == RunStatus.CANCELLED.value


def test_real_execution_error_remains_failed(tmp_path):
    definition = WorkflowDefinition(
        name="real-failure",
        description="real failure",
        phases=[Phase(name="phase", steps=[_agent_step("fail")])],
    )
    runtime = _runtime(tmp_path, definition)
    runtime.set_agent_factory(
        lambda **_kwargs: _FakeAgent(error=RuntimeError("real boom"))
    )
    run_id, error = runtime.start(definition.name, {})
    assert error is None
    with runtime._lock:
        handle = runtime._runs[run_id]
    handle.thread.join(timeout=WAIT_TIMEOUT)

    snapshot = runtime._store.load_snapshot(run_id)
    assert snapshot.status == RunStatus.FAILED.value
    assert "real boom" in snapshot.error
