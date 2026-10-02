"""Workflow Resume ownership and successful-step Journal regressions."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codeharness.tools.executor import ToolExecutionResult
from codeharness.workflow import runtime as workflow_runtime
from codeharness.workflow import state as workflow_state
from codeharness.workflow.definition import (
    AgentStep,
    Phase,
    PipelineStep,
    ToolStep,
    WorkflowDefinition,
    WorkflowStep,
)
from codeharness.workflow.events import WorkflowEventBus
from codeharness.workflow.registry import WorkflowRegistry
from codeharness.workflow.runtime import (
    RunHandle,
    WorkflowCancelled,
    WorkflowContext,
    WorkflowError,
    WorkflowRuntime,
)
from codeharness.workflow.state import (
    JournalEntry,
    RunSnapshot,
    RunStatus,
    WorkflowStateStore,
)

WAIT_TIMEOUT = 3.0


class _SuccessfulAgent:
    def __init__(self):
        self.local_stats = {"total_tokens": 0}

    def agent_loop(self, _messages):
        return "done"


def _agent_definition(name: str = "resume-lock") -> WorkflowDefinition:
    return WorkflowDefinition(
        name=name,
        description=name,
        phases=[
            Phase(
                name="phase",
                steps=[
                    AgentStep(
                        label="agent",
                        prompt_template="run",
                        output_key="result",
                    )
                ],
            )
        ],
    )


def _runtime(base_dir: Path, definition: WorkflowDefinition) -> WorkflowRuntime:
    registry = WorkflowRegistry()
    registry.register(definition)
    runtime = WorkflowRuntime(
        registry=registry,
        state_store=WorkflowStateStore(base_dir),
        event_bus=WorkflowEventBus(),
    )
    runtime.set_agent_factory(lambda **_kwargs: _SuccessfulAgent())
    return runtime


def _hold_run_lock(base_dir: str, run_id: str, connection) -> None:
    """Acquire in another process and hold until the parent asks to release."""
    store = WorkflowStateStore(Path(base_dir))
    run_lock = store.acquire_run_lock(run_id)
    connection.send(run_lock is not None)
    connection.recv()
    assert run_lock is not None
    run_lock.release()
    connection.close()


def test_run_lock_is_cross_process_owned_and_release_is_idempotent(tmp_path):
    base_dir = tmp_path / "state"
    parent_connection, child_connection = multiprocessing.Pipe()
    owner = multiprocessing.Process(
        target=_hold_run_lock,
        args=(str(base_dir), "run-lock", child_connection),
    )
    owner.start()
    assert parent_connection.recv() is True

    contender_store = WorkflowStateStore(base_dir)
    assert contender_store.acquire_run_lock("run-lock") is None

    parent_connection.send("release")
    owner.join(timeout=WAIT_TIMEOUT)
    assert not owner.is_alive()
    assert owner.exitcode == 0

    run_lock = contender_store.acquire_run_lock("run-lock")
    assert run_lock is not None
    assert hasattr(run_lock, "release")
    run_lock.release()
    run_lock.release()

    next_owner = WorkflowStateStore(base_dir).acquire_run_lock("run-lock")
    assert next_owner is not None
    next_owner.release()
    assert (base_dir / "run-lock" / ".lock").exists()
    assert not hasattr(contender_store, "release_run_lock")


def test_process_exit_releases_run_lock(tmp_path):
    base_dir = tmp_path / "state"
    parent_connection, child_connection = multiprocessing.Pipe()
    owner = multiprocessing.Process(
        target=_hold_run_lock,
        args=(str(base_dir), "run-exit", child_connection),
    )
    owner.start()
    assert parent_connection.recv() is True

    owner.terminate()
    owner.join(timeout=WAIT_TIMEOUT)
    assert not owner.is_alive()

    run_lock = WorkflowStateStore(base_dir).acquire_run_lock("run-exit")
    assert run_lock is not None
    assert hasattr(run_lock, "release")
    run_lock.release()


def test_second_runtime_cannot_resume_owned_run_or_mutate_snapshot(tmp_path):
    base_dir = tmp_path / "state"
    definition = _agent_definition()
    owner_runtime = _runtime(base_dir, definition)
    contender_runtime = _runtime(base_dir, definition)
    snapshot = RunSnapshot(
        run_id="run-owned",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.FAILED.value,
        error="original failure",
    )
    owner_runtime._store.save_snapshot(snapshot)
    owner_runtime._store.update_index(snapshot)
    owner_lock = owner_runtime._store.acquire_run_lock(snapshot.run_id)
    assert owner_lock is not None
    snapshot_path = base_dir / snapshot.run_id / "snapshot.json"
    before = snapshot_path.read_bytes()

    resumed_id, error = contender_runtime.resume(snapshot.run_id)

    assert resumed_id is None
    assert "cannot acquire lock" in error
    assert snapshot_path.read_bytes() == before
    owner_lock.release()


def test_run_can_resume_after_previous_owner_releases(tmp_path):
    base_dir = tmp_path / "state"
    definition = _agent_definition()
    owner_runtime = _runtime(base_dir, definition)
    contender_runtime = _runtime(base_dir, definition)
    snapshot = RunSnapshot(
        run_id="run-released",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.FAILED.value,
        error="interrupted",
    )
    owner_runtime._store.save_snapshot(snapshot)
    owner_runtime._store.update_index(snapshot)
    owner_lock = owner_runtime._store.acquire_run_lock(snapshot.run_id)
    owner_lock.release()

    resumed_id, error = contender_runtime.resume(snapshot.run_id)

    assert error is None
    assert resumed_id == snapshot.run_id
    handle = contender_runtime._get_handle(snapshot.run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert not handle.thread.is_alive()
    assert contender_runtime._store.load_snapshot(snapshot.run_id).status == "completed"


def test_start_failure_before_worker_launch_releases_owned_lock(
    monkeypatch, tmp_path
):
    definition = _agent_definition("start-failure")
    runtime = _runtime(tmp_path / "state", definition)
    monkeypatch.setattr(runtime._store, "generate_run_id", lambda: "run-start-fail")

    def fail_start(_thread):
        raise RuntimeError("thread launch failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    run_id, error = runtime.start(definition.name, {})

    assert run_id is None
    assert "thread launch failed" in error
    assert runtime._get_handle("run-start-fail") is None
    snapshot = runtime._store.load_snapshot("run-start-fail")
    assert snapshot.status == RunStatus.FAILED.value
    replacement = runtime._store.acquire_run_lock("run-start-fail")
    assert replacement is not None
    replacement.release()


def test_resume_failure_before_worker_launch_restores_snapshot_and_lock(
    monkeypatch, tmp_path
):
    definition = _agent_definition("resume-failure")
    runtime = _runtime(tmp_path / "state", definition)
    original = RunSnapshot(
        run_id="run-resume-fail",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.FAILED.value,
        error="original error",
    )
    runtime._store.save_snapshot(original)
    runtime._store.update_index(original)

    def fail_start(_thread):
        raise RuntimeError("thread launch failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    run_id, error = runtime.resume(original.run_id)

    assert run_id is None
    assert "thread launch failed" in error
    assert runtime._get_handle(original.run_id) is None
    restored = runtime._store.load_snapshot(original.run_id)
    assert restored.status == RunStatus.FAILED.value
    assert restored.error == "original error"
    replacement = runtime._store.acquire_run_lock(original.run_id)
    assert replacement is not None
    replacement.release()


def test_worker_initialization_failure_marks_failed_and_releases_lock(
    monkeypatch, tmp_path
):
    definition = _agent_definition("worker-init-failure")
    runtime = _runtime(tmp_path / "state", definition)

    def fail_load_journal(_run_id):
        raise OSError("journal read failed")

    monkeypatch.setattr(runtime._store, "load_journal", fail_load_journal)

    run_id, error = runtime.start(definition.name, {})

    assert error is None
    handle = runtime._get_handle(run_id)
    assert handle is not None
    handle.thread.join(timeout=WAIT_TIMEOUT)
    failed = runtime._store.load_snapshot(run_id)
    assert failed.status == RunStatus.FAILED.value
    assert "journal read failed" in failed.error
    replacement = WorkflowStateStore(tmp_path / "state").acquire_run_lock(run_id)
    assert replacement is not None
    replacement.release()


def test_starting_handle_cannot_be_pruned_before_thread_start(monkeypatch, tmp_path):
    definition = _agent_definition("start-prune-race")
    runtime = _runtime(tmp_path / "state", definition)
    real_start = threading.Thread.start

    def start_after_prune(thread):
        runtime.status()
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_after_prune)

    run_id, error = runtime.start(definition.name, {})

    assert error is None
    handle = runtime._get_handle(run_id)
    assert handle is not None
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value


def test_resume_reloads_snapshot_after_acquiring_ownership(monkeypatch, tmp_path):
    definition = _agent_definition("resume-revalidate")
    runtime = _runtime(tmp_path / "state", definition)
    original = RunSnapshot(
        run_id="run-resume-revalidate",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.FAILED.value,
        error="old failure",
    )
    runtime._store.save_snapshot(original)
    runtime._store.update_index(original)
    real_acquire = runtime._store.acquire_run_lock

    def complete_then_acquire(run_id):
        latest = runtime._store.load_snapshot(run_id)
        latest.status = RunStatus.COMPLETED.value
        latest.error = None
        latest.tokens_used = 99
        runtime._store.save_snapshot(latest)
        runtime._store.update_index(latest)
        return real_acquire(run_id)

    monkeypatch.setattr(runtime._store, "acquire_run_lock", complete_then_acquire)

    resumed_id, error = runtime.resume(original.run_id)

    assert resumed_id is None
    assert "already completed" in error
    persisted = runtime._store.load_snapshot(original.run_id)
    assert persisted.status == RunStatus.COMPLETED.value
    assert persisted.tokens_used == 99
    replacement = real_acquire(original.run_id)
    assert replacement is not None
    replacement.release()


class _FailOnceAgentFactory:
    def __init__(self):
        self.attempts = 0
        self.failures_remaining = 1
        self.prompts = []

    def __call__(self, **_kwargs):
        owner = self

        class Agent:
            def __init__(self):
                self.local_stats = {"total_tokens": 0}

            def agent_loop(self, messages):
                owner.attempts += 1
                owner.prompts.append(messages[-1]["content"])
                if owner.failures_remaining:
                    owner.failures_remaining -= 1
                    raise RuntimeError("fail once")
                return "done"

        return Agent()


def _tool_then_agent_definition(
    name: str, args_template: dict | None = None
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name=name,
        description=name,
        phases=[
            Phase(
                name="phase",
                steps=[
                    ToolStep(
                        label="fetch",
                        tool_name="counter",
                        args_template=args_template or {"value": "payload"},
                        output_key="raw_data",
                    ),
                    AgentStep(
                        label="consume",
                        prompt_template="consume {raw_data}",
                        output_key="done",
                    ),
                ],
            )
        ],
    )


def test_canonical_json_is_stable_and_rejects_non_json_values():
    assert workflow_state.canonical_json({"b": 1, "a": "雪"}) == '{"a":"雪","b":1}'

    with pytest.raises(TypeError):
        workflow_state.canonical_json({"bad": {1, 2}})

    with pytest.raises(ValueError):
        workflow_state.canonical_json({"bad": float("nan")})

    with pytest.raises(TypeError):
        workflow_state.canonical_json({1: "non-string key"})


def test_stable_key_fields_are_framed_without_delimiter_collisions():
    first = workflow_state.compute_stable_key(
        "tool", "a|b", "prompt", item_id="c"
    )
    second = workflow_state.compute_stable_key(
        "tool", "a", "prompt", item_id="b|c"
    )

    assert first != second


def test_tool_step_resume_reuses_journal_and_restores_output_key(tmp_path):
    definition = _tool_then_agent_definition("tool-resume")
    runtime = _runtime(tmp_path / "state", definition)
    agent_factory = _FailOnceAgentFactory()
    runtime.set_agent_factory(agent_factory)
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return '{"value":"cached"}'

    runtime.set_tool_resolver(lambda _name: handler)

    run_id, error = runtime.start(definition.name, {})
    assert error is None
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.FAILED.value
    assert calls == [{"value": "payload"}]
    journal = runtime._store.load_journal(run_id)
    assert [entry.step_kind for entry in journal.values()] == ["tool"]

    resumed_id, error = runtime.resume(run_id)
    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert calls == [{"value": "payload"}]


def test_tool_step_changed_args_use_a_different_stable_key(tmp_path):
    base_dir = tmp_path / "state"
    first_definition = _tool_then_agent_definition("tool-args", {"value": "A"})
    first_runtime = _runtime(base_dir, first_definition)
    fail_once = _FailOnceAgentFactory()
    first_runtime.set_agent_factory(fail_once)
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return "ok"

    first_runtime.set_tool_resolver(lambda _name: handler)
    run_id, error = first_runtime.start(first_definition.name, {})
    handle = first_runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert calls == [{"value": "A"}]

    second_definition = _tool_then_agent_definition("tool-args", {"value": "B"})
    second_runtime = _runtime(base_dir, second_definition)
    second_runtime.set_tool_resolver(lambda _name: handler)
    resumed_id, error = second_runtime.resume(run_id)
    assert error is None
    handle = second_runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert second_runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert calls == [{"value": "A"}, {"value": "B"}]
    journal = second_runtime._store.load_journal(run_id)
    assert len([entry for entry in journal.values() if entry.step_kind == "tool"]) == 2


def test_pipeline_tool_journal_isolated_by_position_and_business_id(tmp_path):
    definition = WorkflowDefinition(
        name="pipeline-tools",
        description="pipeline tools",
        phases=[
            Phase(
                name="phase",
                steps=[
                    PipelineStep(
                        label="pipeline",
                        items_key="items",
                        stages=[
                            ToolStep(
                                label="per-item",
                                tool_name="counter",
                                args_template={"same": "args"},
                                output_key="tool_result",
                            )
                        ],
                        output_key="pipeline_result",
                        max_item_concurrency=2,
                    ),
                    AgentStep(
                        label="after-pipeline",
                        prompt_template="finish",
                        output_key="done",
                    ),
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)
    fail_once = _FailOnceAgentFactory()
    runtime.set_agent_factory(fail_once)
    calls = []
    calls_lock = threading.Lock()

    def handler(**kwargs):
        with calls_lock:
            calls.append(kwargs)
        return "ok"

    runtime.set_tool_resolver(lambda _name: handler)
    run_id, error = runtime.start(
        definition.name, {"items": [{"id": "duplicate"}, {"id": "duplicate"}]}
    )
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert len(calls) == 2
    journal = runtime._store.load_journal(run_id)
    assert len([e for e in journal.values() if e.step_kind == "tool"]) == 2

    resumed_id, error = runtime.resume(run_id)
    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert len(calls) == 2



def test_pipeline_journal_identity_keeps_falsy_business_ids(monkeypatch, tmp_path):
    definition = WorkflowDefinition(
        name="pipeline-falsy-business-id",
        description="pipeline falsy business ids",
        phases=[
            Phase(
                name="phase",
                steps=[
                    PipelineStep(
                        label="pipeline",
                        items_key="items",
                        stages=[
                            ToolStep(
                                label="per-item",
                                tool_name="noop",
                                args_template={},
                                output_key="result",
                            )
                        ],
                        output_key="pipeline_result",
                    )
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)
    runtime.set_tool_resolver(lambda _name: lambda: "ok")
    real_compute = workflow_runtime.compute_stable_key
    identities = []

    def record_key(step_kind, label, prompt, schema_json="", item_id=""):
        if step_kind == "tool":
            identities.append(item_id)
        return real_compute(step_kind, label, prompt, schema_json, item_id)

    monkeypatch.setattr(workflow_runtime, "compute_stable_key", record_key)

    run_id, error = runtime.start(
        definition.name,
        {"items": [{}, {"id": 0}, {"id": False}, {"id": ""}]},
    )

    assert error is None
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert set(identities) == {"0:0", "1:0", "2:False", "3:"}

def test_same_tool_stable_key_executes_once_under_concurrency(tmp_path):
    definition = _agent_definition("claim")
    runtime = _runtime(tmp_path / "state", definition)
    snapshot = RunSnapshot(
        run_id="run-claim",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.RUNNING.value,
    )
    runtime._store.save_snapshot(snapshot)
    handle = RunHandle("run-claim", threading.current_thread(), snapshot)
    with runtime._lock:
        runtime._runs[handle.run_id] = handle
    calls = []
    calls_lock = threading.Lock()

    def handler(**kwargs):
        with calls_lock:
            calls.append(kwargs)
        time.sleep(0.1)
        return "ok"

    runtime.set_tool_resolver(lambda _name: handler)
    step = ToolStep(
        label="same",
        tool_name="counter",
        args_template={"value": "same"},
        output_key="result",
    )
    journal = {}

    def execute():
        return runtime._execute_tool_step(
            step,
            WorkflowContext({}),
            snapshot,
            "phase",
            "run-claim",
            journal=journal,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: execute(), range(2)))

    assert results == [{"output": "ok"}, {"output": "ok"}]
    assert calls == [{"value": "same"}]


@pytest.mark.parametrize(
    "status",
    ["blocked", "rejected", "approval_unavailable"],
)
def test_tool_step_not_executed_is_never_journaled(tmp_path, status):
    definition = WorkflowDefinition(
        name=f"not-executed-{status}",
        description=status,
        phases=[
            Phase(
                name="phase",
                steps=[
                    ToolStep(
                        label="mutate",
                        tool_name="counter",
                        args_template={},
                        output_key="result",
                        allow_failure=True,
                    )
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)

    class NeverExecuted:
        def execute(self, **_kwargs):
            return ToolExecutionResult(False, "not executed", status)

    runtime._tool_executor = NeverExecuted()
    runtime.set_tool_resolver(lambda _name: lambda: "should not run")
    run_id, error = runtime.start(definition.name, {})
    assert error is None
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert runtime._store.load_journal(run_id) == {}


def test_allow_failure_handler_exception_is_not_journaled(tmp_path):
    definition = WorkflowDefinition(
        name="handler-exception",
        description="handler exception",
        phases=[
            Phase(
                name="phase",
                steps=[
                    ToolStep(
                        label="mutate",
                        tool_name="counter",
                        args_template={},
                        output_key="result",
                        allow_failure=True,
                    )
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)
    calls = []

    def handler():
        calls.append("called")
        raise RuntimeError("handler failed")

    runtime.set_tool_resolver(lambda _name: handler)
    run_id, error = runtime.start(definition.name, {})
    assert error is None
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert calls == ["called"]
    assert runtime._store.load_journal(run_id) == {}
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value


class _NestedAgentFactory:
    def __init__(self):
        self.child_calls = []
        self.parent_attempts = 0
        self.parent_failures_remaining = 1
        self.parent_prompts = []

    def __call__(self, **_kwargs):
        owner = self

        class Agent:
            def __init__(self):
                self.local_stats = {"total_tokens": 0}

            def agent_loop(self, messages):
                prompt = messages[-1]["content"]
                if prompt.startswith("child "):
                    owner.child_calls.append(prompt)
                    return prompt
                owner.parent_attempts += 1
                owner.parent_prompts.append(prompt)
                if owner.parent_failures_remaining:
                    owner.parent_failures_remaining -= 1
                    raise RuntimeError("parent fails once")
                return "parent done"

        return Agent()


def _nested_definitions(parent_input: str):
    child = WorkflowDefinition(
        name="child",
        description="child",
        phases=[
            Phase(
                name="child-phase",
                steps=[
                    AgentStep(
                        label="child-agent",
                        prompt_template="child {value}",
                        output_key="child_output",
                    )
                ],
            )
        ],
    )
    parent = WorkflowDefinition(
        name="parent",
        description="parent",
        phases=[
            Phase(
                name="parent-phase",
                steps=[
                    WorkflowStep(
                        label="invoke-child",
                        workflow_name="child",
                        inputs_mapping={"value": parent_input},
                        output_key="child_result",
                    ),
                    AgentStep(
                        label="after-child",
                        prompt_template="parent sees {child_result}",
                        output_key="parent_output",
                    ),
                ],
            )
        ],
    )
    return child, parent


def test_completed_workflow_step_is_reused_on_parent_resume(tmp_path):
    child, parent = _nested_definitions("A")
    registry = WorkflowRegistry()
    registry.register(child)
    registry.register(parent)
    runtime = WorkflowRuntime(
        registry=registry,
        state_store=WorkflowStateStore(tmp_path / "state"),
        event_bus=WorkflowEventBus(),
    )
    factory = _NestedAgentFactory()
    runtime.set_agent_factory(factory)

    run_id, error = runtime.start(parent.name, {})
    assert error is None
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.FAILED.value
    assert factory.child_calls == ["child A"]
    journal = runtime._store.load_journal(run_id)
    assert len([e for e in journal.values() if e.step_kind == "workflow"]) == 1

    resumed_id, error = runtime.resume(run_id)
    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert factory.child_calls == ["child A"]
    assert any("child A" in prompt for prompt in factory.parent_prompts)


def test_workflow_step_changed_inputs_use_a_different_stable_key(tmp_path):
    base_dir = tmp_path / "state"
    child, parent = _nested_definitions("A")
    registry = WorkflowRegistry()
    registry.register(child)
    registry.register(parent)
    first_runtime = WorkflowRuntime(
        registry=registry,
        state_store=WorkflowStateStore(base_dir),
        event_bus=WorkflowEventBus(),
    )
    factory = _NestedAgentFactory()
    first_runtime.set_agent_factory(factory)
    run_id, error = first_runtime.start(parent.name, {})
    handle = first_runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert factory.child_calls == ["child A"]

    changed_child, changed_parent = _nested_definitions("B")
    changed_registry = WorkflowRegistry()
    changed_registry.register(changed_child)
    changed_registry.register(changed_parent)
    second_runtime = WorkflowRuntime(
        registry=changed_registry,
        state_store=WorkflowStateStore(base_dir),
        event_bus=WorkflowEventBus(),
    )
    second_runtime.set_agent_factory(factory)
    resumed_id, error = second_runtime.resume(run_id)
    assert error is None
    handle = second_runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert second_runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert factory.child_calls == ["child A", "child B"]
    journal = second_runtime._store.load_journal(run_id)
    assert len([e for e in journal.values() if e.step_kind == "workflow"]) == 2


class _AgentResumeFactory:
    def __init__(self):
        self.first_calls = 0
        self.second_calls = 0
        self.second_failures_remaining = 1
        self.second_prompts = []

    def __call__(self, **_kwargs):
        owner = self

        class Agent:
            def __init__(self):
                self.local_stats = {"total_tokens": 0}

            def agent_loop(self, messages):
                prompt = messages[-1]["content"]
                if prompt == "first":
                    owner.first_calls += 1
                    return "first output"
                owner.second_calls += 1
                owner.second_prompts.append(prompt)
                if owner.second_failures_remaining:
                    owner.second_failures_remaining -= 1
                    raise RuntimeError("second fails once")
                return "done"

        return Agent()


def test_existing_agent_step_resume_still_uses_journal(tmp_path):
    definition = WorkflowDefinition(
        name="agent-resume",
        description="agent resume",
        phases=[
            Phase(
                name="phase",
                steps=[
                    AgentStep(
                        label="first-label",
                        prompt_template="first",
                        output_key="first_result",
                    ),
                    AgentStep(
                        label="second-label",
                        prompt_template="second {first_result}",
                        output_key="second_result",
                    ),
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)
    factory = _AgentResumeFactory()
    runtime.set_agent_factory(factory)
    run_id, error = runtime.start(definition.name, {})
    handle = runtime._get_handle(run_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.FAILED.value
    assert factory.first_calls == 1
    assert len(runtime._store.load_journal(run_id)) == 1

    resumed_id, error = runtime.resume(run_id)
    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    assert runtime._store.load_snapshot(run_id).status == RunStatus.COMPLETED.value
    assert factory.first_calls == 1



def test_existing_agent_journal_key_remains_resume_compatible(tmp_path):
    definition = _agent_definition("agent-legacy-key")
    runtime = _runtime(tmp_path / "state", definition)
    snapshot = RunSnapshot(
        run_id="run-agent-legacy-key",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.FAILED.value,
        error="interrupted",
    )
    runtime._store.save_snapshot(snapshot)
    runtime._store.update_index(snapshot)
    legacy_raw = "agent|agent||run|"
    legacy_key = hashlib.sha256(legacy_raw.encode()).hexdigest()[:16]
    runtime._store.append_journal(
        snapshot.run_id,
        JournalEntry(
            stable_key=legacy_key,
            label="agent",
            phase="phase",
            step_kind="agent",
            result="legacy result",
        ),
    )

    def fail_factory(**_kwargs):
        raise AssertionError("legacy AgentStep journal entry was not reused")

    runtime.set_agent_factory(fail_factory)

    resumed_id, error = runtime.resume(snapshot.run_id)

    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    completed = runtime._store.load_snapshot(snapshot.run_id)
    assert completed.status == RunStatus.COMPLETED.value
    assert runtime._store.load_output(snapshot.run_id) == "legacy result"


def test_pipeline_agent_legacy_key_with_schema_remains_compatible(tmp_path):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    definition = WorkflowDefinition(
        name="pipeline-agent-legacy-key",
        description="pipeline legacy key",
        phases=[
            Phase(
                name="phase",
                steps=[
                    PipelineStep(
                        label="pipeline",
                        items_key="items",
                        stages=[
                            AgentStep(
                                label="per-item",
                                prompt_template="process {item.id}",
                                output_key="analysis",
                                output_schema=schema,
                            )
                        ],
                        output_key="pipeline_result",
                    )
                ],
            )
        ],
    )
    runtime = _runtime(tmp_path / "state", definition)
    snapshot = RunSnapshot(
        run_id="run-pipeline-agent-legacy-key",
        workflow_name=definition.name,
        inputs={"items": [{"id": "biz"}]},
        status=RunStatus.FAILED.value,
        error="interrupted",
    )
    runtime._store.save_snapshot(snapshot)
    runtime._store.update_index(snapshot)
    legacy_schema = json.dumps(schema, sort_keys=True)
    legacy_raw = f"agent|per-item|biz|process biz|{legacy_schema}"
    legacy_key = hashlib.sha256(legacy_raw.encode()).hexdigest()[:16]
    runtime._store.append_journal(
        snapshot.run_id,
        JournalEntry(
            stable_key=legacy_key,
            label="per-item",
            phase="phase",
            step_kind="agent",
            result={"answer": "cached"},
        ),
    )

    def fail_factory(**_kwargs):
        raise AssertionError("legacy Pipeline Agent journal entry was not reused")

    runtime.set_agent_factory(fail_factory)

    resumed_id, error = runtime.resume(snapshot.run_id)

    assert error is None
    handle = runtime._get_handle(resumed_id)
    handle.thread.join(timeout=WAIT_TIMEOUT)
    completed = runtime._store.load_snapshot(snapshot.run_id)
    assert completed.status == RunStatus.COMPLETED.value
    output = runtime._store.load_output(snapshot.run_id)
    assert output[0]["analysis"] == {"answer": "cached"}

def test_failed_claim_owner_releases_and_waiter_rechecks_journal(tmp_path):
    definition = _agent_definition("claim-failure")
    runtime = _runtime(tmp_path / "state", definition)
    snapshot = RunSnapshot(
        run_id="run-claim-failure",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.RUNNING.value,
    )
    runtime._store.save_snapshot(snapshot)
    handle = RunHandle(snapshot.run_id, threading.current_thread(), snapshot)
    with runtime._lock:
        runtime._runs[handle.run_id] = handle
    attempts = []
    attempts_lock = threading.Lock()
    first_entered = threading.Event()

    def handler(**_kwargs):
        with attempts_lock:
            attempts.append(len(attempts) + 1)
            attempt = attempts[-1]
        if attempt == 1:
            first_entered.set()
            time.sleep(0.1)
            raise RuntimeError("first owner failed")
        return "ok"

    runtime.set_tool_resolver(lambda _name: handler)
    step = ToolStep(
        label="same-failure",
        tool_name="counter",
        args_template={},
        output_key="result",
    )
    journal = {}

    def execute():
        return runtime._execute_tool_step(
            step,
            WorkflowContext({}),
            snapshot,
            "phase",
            snapshot.run_id,
            journal=journal,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert first_entered.wait(timeout=WAIT_TIMEOUT)
        second = pool.submit(execute)
        with pytest.raises(WorkflowError, match="first owner failed"):
            first.result(timeout=WAIT_TIMEOUT)
        assert second.result(timeout=WAIT_TIMEOUT) == {"output": "ok"}

    assert attempts == [1, 2]
    assert len(journal) == 1
    assert handle.journal_claims == set()


def test_cancelled_claim_owner_releases_and_notifies_waiter(tmp_path):
    definition = _agent_definition("claim-cancel")
    runtime = _runtime(tmp_path / "state", definition)
    snapshot = RunSnapshot(
        run_id="run-claim-cancel",
        workflow_name=definition.name,
        inputs={},
        status=RunStatus.RUNNING.value,
    )
    runtime._store.save_snapshot(snapshot)
    handle = RunHandle(snapshot.run_id, threading.current_thread(), snapshot)
    with runtime._lock:
        runtime._runs[handle.run_id] = handle
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def handler(**_kwargs):
        calls.append("called")
        entered.set()
        assert release.wait(timeout=WAIT_TIMEOUT)
        return "ok"

    runtime.set_tool_resolver(lambda _name: handler)
    step = ToolStep(
        label="same-cancel",
        tool_name="counter",
        args_template={},
        output_key="result",
    )
    journal = {}

    def execute():
        return runtime._execute_tool_step(
            step,
            WorkflowContext({}),
            snapshot,
            "phase",
            snapshot.run_id,
            journal=journal,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        assert entered.wait(timeout=WAIT_TIMEOUT)
        second = pool.submit(execute)
        handle.cancelled.set()
        release.set()
        with pytest.raises(WorkflowCancelled):
            first.result(timeout=WAIT_TIMEOUT)
        with pytest.raises(WorkflowCancelled):
            second.result(timeout=WAIT_TIMEOUT)

    assert journal == {}
    assert handle.journal_claims == set()
    assert calls == ["called"]
