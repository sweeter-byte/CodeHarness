import inspect
import threading
from pathlib import Path

import pytest


def _runtime(tmp_path):
    from codeharness.app import CodeHarness
    from codeharness.config import RuntimeConfig

    config = RuntimeConfig(
        api_key="key",
        base_url="https://example.invalid",
        model="test-model",
        model_context_window=4096,
        workspace=Path(tmp_path),
        mcp_config_path=Path(tmp_path) / "mcp.json",
        agent_home=Path(tmp_path) / "agent-home",
    )
    harness = CodeHarness(config)
    harness._started = True
    harness.history = []
    return harness


class _ImmediateAgent:
    def __init__(self, result="done"):
        self.result = result
        self.calls = []

    def agent_loop(self, history):
        self.calls.append(history)
        return self.result


def test_runtime_owns_turn_serialization_state(tmp_path):
    harness = _runtime(tmp_path)

    assert harness._turn_lock.acquire(blocking=False) is True
    harness._turn_lock.release()
    assert harness._user_turn_pending.is_set() is False


def test_user_turn_waits_for_async_turn_instead_of_running_concurrently(
    monkeypatch, tmp_path
):
    from codeharness import app as app_module

    monkeypatch.setattr(app_module, "trigger_hooks", lambda *_args: None)
    harness = _runtime(tmp_path)
    async_entered = threading.Event()
    release_async = threading.Event()
    user_entered = threading.Event()
    active = 0
    max_active = 0
    guard = threading.Lock()

    class SerializedAgent:
        def agent_loop(self, history):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                if history[-1]["content"] == "background":
                    async_entered.set()
                    assert release_async.wait(timeout=2)
                else:
                    user_entered.set()
                return "answer"
            finally:
                with guard:
                    active -= 1

    harness.agent = SerializedAgent()
    async_thread = threading.Thread(
        target=lambda: harness._try_deliver_async("background")
    )
    async_thread.start()
    assert async_entered.wait(timeout=2)

    user_thread = threading.Thread(target=lambda: harness.run("user"))
    user_thread.start()
    assert harness._user_turn_pending.wait(timeout=2)
    assert user_entered.is_set() is False

    release_async.set()
    async_thread.join(timeout=2)
    user_thread.join(timeout=2)

    assert user_entered.is_set() is True
    assert max_active == 1


def test_async_delivery_refuses_when_user_turn_is_pending(monkeypatch, tmp_path):
    from codeharness import app as app_module

    monkeypatch.setattr(app_module, "trigger_hooks", lambda *_args: None)
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent()
    harness._turn_lock.acquire()
    user_thread = threading.Thread(target=lambda: harness.run("user"))
    user_thread.start()
    assert harness._user_turn_pending.wait(timeout=2)

    try:
        assert harness._try_deliver_async("background") is False
    finally:
        harness._turn_lock.release()
        user_thread.join(timeout=2)


def test_async_delivery_refuses_when_agent_is_busy(tmp_path):
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent()
    harness._turn_lock.acquire()
    try:
        assert harness._try_deliver_async("background") is False
    finally:
        harness._turn_lock.release()


def test_async_delivery_uses_leader_history_agent_and_result_handler(tmp_path):
    harness = _runtime(tmp_path)
    agent = _ImmediateAgent(result="background answer")
    results = []
    harness.agent = agent
    harness.set_async_result_handler(results.append)

    assert harness._try_deliver_async("background event") is True

    assert harness.history[0] == {"role": "user", "content": "background event"}
    assert agent.calls == [harness.history]
    assert results == ["background answer"]


def test_async_delivery_exception_rolls_back_message_and_releases_lock(tmp_path):
    harness = _runtime(tmp_path)

    class FailingAgent:
        def agent_loop(self, _history):
            raise RuntimeError("boom")

    harness.agent = FailingAgent()

    for _ in range(2):
        with pytest.raises(RuntimeError, match="boom"):
            harness._try_deliver_async("retry me")
        assert [m for m in harness.history if m.get("content") == "retry me"] == []
        assert harness._turn_lock.acquire(blocking=False) is True
        harness._turn_lock.release()


def test_context_operations_share_runtime_turn_lock(tmp_path):
    harness = _runtime(tmp_path)
    entered = threading.Event()

    class ContextManager:
        def get_observability_report(self, _history):
            entered.set()
            return "report"

    harness.agent = type("Agent", (), {"context_manager": ContextManager()})()
    harness._turn_lock.acquire()
    thread = threading.Thread(target=harness.context_info)
    thread.start()
    assert entered.wait(timeout=0.05) is False
    harness._turn_lock.release()
    thread.join(timeout=2)
    assert entered.is_set() is True


def test_cron_has_only_callback_delivery_dependencies():
    from codeharness.scheduler import cron

    source = inspect.getsource(cron)

    for removed_name in (
        "_agent_ref",
        "_history_ref",
        "agent_lock",
        "UI_BUSY",
        "CRON_TURN",
    ):
        assert removed_name not in source
    assert "print(" not in source


class _OneIterationStop:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _timeout):
        self.stopped = True


@pytest.fixture
def isolated_cron(monkeypatch, tmp_path):
    from codeharness.scheduler import cron

    store = cron.CronStore(tmp_path / "cron.json")
    monkeypatch.setattr(cron, "_cron_store", store)
    monkeypatch.setattr(cron, "_delivery_queue", [])
    monkeypatch.setattr(cron, "_status_handler", None, raising=False)
    return cron, store


def test_cron_keeps_job_queued_when_runtime_is_busy(monkeypatch, isolated_cron):
    cron, store = isolated_cron
    job = cron.CronJob("one", "* * * * *", "work", False, False, True)
    store.jobs[job.id] = job
    cron._delivery_queue.append(job)
    calls = []
    monkeypatch.setattr(
        cron,
        "_delivery_handler",
        lambda content: calls.append(content) or False,
        raising=False,
    )

    cron._queue_processor_loop(_OneIterationStop())

    assert calls == ["[Scheduled] work"]
    assert cron._delivery_queue == [job]
    assert job.pending_delivery is True


def test_cron_success_preserves_one_shot_and_recurring_semantics(
    monkeypatch, isolated_cron
):
    cron, store = isolated_cron
    one_shot = cron.CronJob(
        "one", "* * * * *", "once", False, False, True
    )
    recurring = cron.CronJob(
        "repeat", "* * * * *", "again", True, False, True
    )
    store.jobs = {one_shot.id: one_shot, recurring.id: recurring}
    cron._delivery_queue.extend([one_shot, recurring])
    delivered = []
    monkeypatch.setattr(
        cron,
        "_delivery_handler",
        lambda content: delivered.append(content) or True,
        raising=False,
    )

    cron._queue_processor_loop(_OneIterationStop())

    assert delivered == ["[Scheduled] once\n[Scheduled] again"]
    assert one_shot.id not in store.jobs
    assert recurring.pending_delivery is False
    assert cron._delivery_queue == []


def _team_message():
    return {
        "type": "result",
        "from": "Alice",
        "to": "lead",
        "content": "finished",
        "metadata": {},
    }


def test_team_wakeup_has_no_cron_agent_history_or_terminal_dependency():
    from codeharness.team import wakeup

    source = inspect.getsource(wakeup)

    assert "from codeharness.scheduler import cron" not in source
    assert "agent_loop" not in source
    assert "history" not in source
    assert "print(" not in source


def test_team_wakeup_retries_buffered_event_after_runtime_busy(monkeypatch):
    from codeharness.team import wakeup

    waits = 0

    def wait_for_messages(_agent, timeout):
        nonlocal waits
        waits += 1
        return [_team_message()] if waits == 1 else []

    monkeypatch.setattr(wakeup.BUS, "wait_for_messages", wait_for_messages)
    monkeypatch.setattr(wakeup._stop_event, "is_set", lambda: waits >= 2)
    accepted = iter([False, True])
    delivered = []

    wakeup._wakeup_loop(
        lambda content: delivered.append(content) or next(accepted)
    )

    assert len(delivered) == 2
    assert delivered[0] == delivered[1]
    assert "[result from Alice]\nfinished" in delivered[0]


def test_team_wakeup_clears_buffer_only_after_delivery_success(monkeypatch):
    from codeharness.team import wakeup

    waits = 0

    def wait_for_messages(_agent, timeout):
        nonlocal waits
        waits += 1
        return [_team_message()] if waits == 1 else []

    monkeypatch.setattr(wakeup.BUS, "wait_for_messages", wait_for_messages)
    monkeypatch.setattr(wakeup._stop_event, "is_set", lambda: waits >= 2)
    delivered = []

    wakeup._wakeup_loop(lambda content: delivered.append(content) or True)

    assert len(delivered) == 1


def test_permission_hook_blocks_background_approval_without_cron_dependency(
    monkeypatch,
):
    from codeharness import hooks

    monkeypatch.setattr(
        hooks._perm_manager,
        "check",
        lambda _tool_name, _args: ("ask", "approval needed"),
    )
    hooks.INTERACTIVE_APPROVAL_ALLOWED.value = False
    hooks.PENDING_USER_ASK.value = None
    try:
        result = hooks.permission_hook("bash", {"command": "danger"})
    finally:
        hooks.INTERACTIVE_APPROVAL_ALLOWED.value = True

    assert "Interactive approval not available during background execution" in result
    assert hooks.PENDING_USER_ASK.value is None
    assert "scheduler" not in inspect.getsource(hooks.permission_hook)


def test_background_turn_subagent_inherits_non_interactive_approval_context(
    monkeypatch, tmp_path
):
    from codeharness import hooks, subagent
    from codeharness.skills import SkillRegistry

    monkeypatch.setattr(
        hooks._perm_manager,
        "check",
        lambda _tool_name, _args: ("ask", "approval needed"),
    )
    approvals = []
    blocked = []

    class NestedAgent:
        def agent_loop(self, _messages):
            result = hooks.permission_hook("bash", {"command": "danger"})
            if result is None and hooks.PENDING_USER_ASK.value is not None:
                approvals.append(hooks.PENDING_USER_ASK.value)
            blocked.append(result)
            return "nested done"

    skills = SkillRegistry(
        workspace=tmp_path,
        agent_home=tmp_path / "agent-home",
    )
    task_handler = subagent.make_task_handler(
        lambda **_kwargs: NestedAgent(),
        workspace=str(tmp_path),
        skill_registry=skills,
    )
    harness = _runtime(tmp_path)

    class LeaderAgent:
        def agent_loop(self, _history):
            return task_handler("nested task")

    harness.agent = LeaderAgent()

    assert harness._try_deliver_async("background") is True
    assert approvals == []
    assert "Interactive approval not available during background execution" in blocked[0]


def test_cli_registers_and_displays_async_results():
    from interfaces.cli import CLI

    class FakeHarness:
        def set_approval_handler(self, handler):
            self.approval_handler = handler

        def set_status_handler(self, handler):
            self.status_handler = handler

        def set_async_result_handler(self, handler):
            self.async_result_handler = handler

    output = []
    harness = FakeHarness()
    CLI(harness, input_fn=lambda _prompt: "q", output_fn=output.append)

    harness.async_result_handler("async answer")

    assert any("async answer" in line for line in output)
    assert any(">> " in line for line in output)


def test_async_result_handler_runs_after_turn_lock_is_released(tmp_path):
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent(result="answer")
    lock_was_free = []

    def presenting_handler(_result):
        lock_was_free.append(harness._turn_lock.acquire(blocking=False))
        if lock_was_free[-1]:
            harness._turn_lock.release()

    harness.set_async_result_handler(presenting_handler)

    assert harness._try_deliver_async("background") is True
    assert lock_was_free == [True]


def test_presentation_failure_still_counts_as_successful_delivery(
    monkeypatch, tmp_path
):
    statuses = []
    harness = _runtime(tmp_path)
    agent = _ImmediateAgent(result="answer")
    harness.agent = agent

    def failing_handler(_result):
        raise RuntimeError("ui broke")

    harness.set_async_result_handler(failing_handler)
    harness.set_status_handler(statuses.append)

    assert harness._try_deliver_async("background") is True

    # The successful agent turn is not rolled back.
    assert [m for m in harness.history if m.get("content") == "background"] == [
        {"role": "user", "content": "background"}
    ]
    assert len(agent.calls) == 1
    assert any("Failed to present async result" in line for line in statuses)
    assert harness._turn_lock.acquire(blocking=False) is True
    harness._turn_lock.release()


def test_presentation_failure_with_failing_status_handler_still_succeeds(tmp_path):
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent(result="answer")

    def failing_handler(_result):
        raise RuntimeError("ui broke")

    def failing_status(_message):
        raise RuntimeError("status broke")

    harness.set_async_result_handler(failing_handler)
    harness.set_status_handler(failing_status)

    assert harness._try_deliver_async("background") is True


def test_cron_does_not_requeue_job_after_interface_presentation_failure(
    monkeypatch, isolated_cron, tmp_path
):
    cron, store = isolated_cron
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent(result="answer")

    def failing_presentation(_result):
        raise RuntimeError("ui broke")

    harness.set_async_result_handler(failing_presentation)
    job = cron.CronJob("one", "* * * * *", "work", False, False, True)
    store.jobs[job.id] = job
    cron._delivery_queue.append(job)
    monkeypatch.setattr(
        cron,
        "_delivery_handler",
        harness._try_deliver_async,
        raising=False,
    )

    cron._queue_processor_loop(_OneIterationStop())

    # The event reached the agent exactly once and is not queued again.
    assert [m for m in harness.history if "[Scheduled] work" in m["content"]]
    assert job.id not in store.jobs
    assert cron._delivery_queue == []


def test_team_wakeup_clears_buffer_after_presentation_failure(monkeypatch, tmp_path):
    from codeharness.team import wakeup

    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent(result="answer")

    def failing_presentation(_result):
        raise RuntimeError("ui broke")

    harness.set_async_result_handler(failing_presentation)

    waits = 0

    def wait_for_messages(_agent, timeout):
        nonlocal waits
        waits += 1
        return [_team_message()] if waits == 1 else []

    monkeypatch.setattr(wakeup.BUS, "wait_for_messages", wait_for_messages)
    monkeypatch.setattr(wakeup._stop_event, "is_set", lambda: waits >= 2)

    wakeup._wakeup_loop(harness._try_deliver_async)

    # The model-facing event was delivered exactly once.
    assert (
        sum(1 for m in harness.history if "[Team events]" in m["content"]) == 1
    )


def test_backend_files_do_not_contain_cli_prompt():
    from codeharness import app as app_module
    from codeharness.core import agent as agent_module
    from codeharness.scheduler import cron
    from codeharness.team import wakeup

    for module in (cron, wakeup, app_module, agent_module):
        assert '">> "' not in inspect.getsource(module)
