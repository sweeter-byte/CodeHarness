"""Final Fix 2 — graceful Runtime shutdown and agent quiescence."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


WAIT_TIMEOUT = 2.0


def _runtime(tmp_path):
    from codeharness.app import CodeHarness
    from codeharness.config import RuntimeConfig

    harness = CodeHarness(
        RuntimeConfig(
            api_key="key",
            base_url="https://example.invalid",
            model="test-model",
            model_context_window=4096,
            workspace=Path(tmp_path),
            mcp_config_path=Path(tmp_path) / "mcp.json",
            agent_home=Path(tmp_path) / "agent-home",
        )
    )
    harness._started = True
    harness.history = []
    return harness


class _ContextManager:
    def get_observability_report(self, _history):
        return "report"

    def compact_history(self, history):
        return list(history)

    def set_active_request(self, _request):
        pass


class _ImmediateAgent:
    approval_handler = None
    status_handler = None
    context_manager = _ContextManager()

    def agent_loop(self, _history):
        return "done"


def test_closing_gate_rejects_new_operations_without_mutating_history(tmp_path):
    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent()
    harness.history.append({"role": "assistant", "content": "existing"})
    before = list(harness.history)
    harness._closing.set()

    with pytest.raises(RuntimeError, match="closing"):
        harness.run("new user turn")
    assert harness._try_deliver_async("background") is False
    for operation in (harness.context_info, harness.compact, harness.clear):
        with pytest.raises(RuntimeError, match="closing"):
            operation()

    assert harness.history == before


def test_start_rejects_an_incompletely_closing_runtime(tmp_path):
    harness = _runtime(tmp_path)
    harness._started = False
    harness._closing.set()

    with pytest.raises(RuntimeError, match="closing"):
        harness.start()


def _install_close_spies(monkeypatch, app_module, harness, team_results=(True,)):
    teardown = []
    producer_stops = []
    team_results = iter(team_results)

    harness._cron_started = True
    harness._team_wakeup_started = True
    harness.mcp_manager = SimpleNamespace(
        close_all=lambda: teardown.append("mcp_manager")
    )
    harness.set_approval_handler(lambda _reason: True)
    harness.set_status_handler(lambda _message: None)
    harness.set_async_result_handler(lambda _result: None)

    monkeypatch.setattr(
        app_module.cron,
        "stop",
        lambda: producer_stops.append("cron") or True,
    )
    monkeypatch.setattr(
        app_module.team_wakeup,
        "stop",
        lambda: producer_stops.append("wakeup") or True,
    )
    monkeypatch.setattr(
        app_module.TEAM,
        "shutdown_all",
        lambda timeout: next(team_results),
    )
    monkeypatch.setattr(
        app_module.TEAM,
        "set_agent_factory",
        lambda factory: teardown.append(("team", factory)),
    )
    monkeypatch.setattr(
        app_module.hooks,
        "clear_mcp_permissions",
        lambda: teardown.append("mcp_permissions"),
    )
    monkeypatch.setattr(
        app_module,
        "shutdown_runtime",
        lambda: teardown.append("mcp_runtime"),
    )
    monkeypatch.setattr(app_module, "trigger_hooks", lambda *_args: None)
    return teardown, producer_stops


def test_close_times_out_on_active_user_turn_then_retries(monkeypatch, tmp_path):
    from codeharness import app as app_module

    harness = _runtime(tmp_path)
    monkeypatch.setattr(app_module, "CLOSE_TIMEOUT", 0.05)
    entered = threading.Event()
    release = threading.Event()

    class BlockingAgent(_ImmediateAgent):
        def agent_loop(self, _history):
            entered.set()
            assert release.wait(timeout=WAIT_TIMEOUT)
            return "done"

    harness.agent = BlockingAgent()
    teardown, _ = _install_close_spies(monkeypatch, app_module, harness)
    turn = threading.Thread(target=lambda: harness.run("hold"))
    turn.start()
    assert entered.wait(timeout=WAIT_TIMEOUT)

    assert harness.close() is False
    assert harness._closing.is_set()
    assert harness._closed is False
    assert teardown == []
    assert harness.approval_handler is not None
    assert harness.status_handler is not None
    assert harness.async_result_handler is not None

    release.set()
    turn.join(timeout=WAIT_TIMEOUT)
    assert not turn.is_alive()
    assert harness.close() is True
    assert harness._closed is True
    assert harness._started is False
    assert teardown == [
        ("team", None),
        "mcp_permissions",
        "mcp_manager",
        "mcp_runtime",
    ]
    assert harness.close() is True


def test_close_times_out_on_active_async_turn_then_retries(monkeypatch, tmp_path):
    from codeharness import app as app_module

    harness = _runtime(tmp_path)
    monkeypatch.setattr(app_module, "CLOSE_TIMEOUT", 0.05)
    entered = threading.Event()
    release = threading.Event()

    class BlockingAgent(_ImmediateAgent):
        def agent_loop(self, _history):
            entered.set()
            assert release.wait(timeout=WAIT_TIMEOUT)
            return "done"

    harness.agent = BlockingAgent()
    teardown, _ = _install_close_spies(monkeypatch, app_module, harness)
    turn = threading.Thread(
        target=lambda: harness._try_deliver_async("background")
    )
    turn.start()
    assert entered.wait(timeout=WAIT_TIMEOUT)

    assert harness.close() is False
    assert teardown == []

    release.set()
    turn.join(timeout=WAIT_TIMEOUT)
    assert harness.close() is True
    assert teardown


def test_close_preserves_dependencies_until_teammates_stop(monkeypatch, tmp_path):
    from codeharness import app as app_module

    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent()
    teardown, _ = _install_close_spies(
        monkeypatch, app_module, harness, team_results=(False, True)
    )

    assert harness.close() is False
    assert teardown == []
    assert harness._closing.is_set()
    assert harness._closed is False

    assert harness.close() is True
    assert teardown


def test_close_preserves_dependencies_until_async_producers_stop(
    monkeypatch, tmp_path
):
    from codeharness import app as app_module

    harness = _runtime(tmp_path)
    harness.agent = _ImmediateAgent()
    teardown, _ = _install_close_spies(
        monkeypatch, app_module, harness, team_results=(True, True)
    )
    cron_results = iter((False, False, True))
    wakeup_results = iter((False, False, True))
    monkeypatch.setattr(app_module.cron, "stop", lambda: next(cron_results))
    monkeypatch.setattr(
        app_module.team_wakeup, "stop", lambda: next(wakeup_results)
    )

    assert harness.close() is False
    assert teardown == []
    assert harness._cron_started is True
    assert harness._team_wakeup_started is True
    assert harness._closed is False

    assert harness.close() is True
    assert teardown


@pytest.fixture
def isolated_team(monkeypatch, tmp_path):
    from codeharness.team import manager as manager_module
    from codeharness.team import protocol
    from codeharness.team.bus import BUS, LEADER

    monkeypatch.setattr(BUS, "_mailbox_dir", BUS._mailbox_dir)
    BUS.configure(tmp_path / ".codeharness" / "runtime" / "team" / "mailboxes")
    monkeypatch.chdir(tmp_path)
    protocol.clear()
    BUS.read_inbox(LEADER)
    manager = manager_module.TeamManager()
    yield manager
    for state in manager.list_states():
        BUS.send(LEADER, state.name, "cleanup", "shutdown_request")
        if state.thread is not None:
            state.thread.join(timeout=WAIT_TIMEOUT)
    protocol.clear()
    BUS.read_inbox(LEADER)


def _start_teammate_state(manager, name, before_idle=None):
    from codeharness.team.teammate import TeammateState, _idle_loop

    state = TeammateState(name=name, require_plan=False)

    def target():
        try:
            if before_idle is not None:
                before_idle()
            _idle_loop(state)
        finally:
            manager._remove(name)

    state.thread = threading.Thread(target=target, name=f"teammate-{name}")
    with manager._lock:
        manager._states[name] = state
    state.thread.start()
    return state


def test_shutdown_all_stops_idle_teammate_and_cleans_transient_state(isolated_team):
    from codeharness.team import protocol
    from codeharness.team.bus import BUS, LEADER

    manager = isolated_team
    state = _start_teammate_state(manager, "Alice")
    protocol.create_request("plan_approval_request", "Alice", LEADER, "plan")
    BUS.send("Alice", LEADER, "stale event", "message")

    assert manager.shutdown_all(timeout=WAIT_TIMEOUT) is True
    assert not state.thread.is_alive()
    assert manager.list_states() == []
    assert protocol.PENDING_REQUESTS == {}
    assert BUS.read_inbox(LEADER) == []


def test_shutdown_all_times_out_during_work_then_succeeds_on_retry(isolated_team):
    manager = isolated_team
    entered = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        assert release.wait(timeout=WAIT_TIMEOUT)

    state = _start_teammate_state(manager, "Bob", before_idle=work)
    assert entered.wait(timeout=WAIT_TIMEOUT)

    assert manager.shutdown_all(timeout=0.05) is False
    assert state.thread.is_alive()
    assert manager.get_state("Bob") is state

    release.set()
    state.thread.join(timeout=WAIT_TIMEOUT)
    assert not state.thread.is_alive()
    assert manager.shutdown_all(timeout=WAIT_TIMEOUT) is True
    assert manager.list_states() == []


def test_idle_loop_prioritizes_shutdown_over_earlier_normal_message(
    monkeypatch, tmp_path
):
    from codeharness.team.bus import BUS, LEADER
    from codeharness.team.teammate import TeammateState, _idle_loop

    monkeypatch.setattr(BUS, "_mailbox_dir", BUS._mailbox_dir)
    BUS.configure(tmp_path / ".codeharness" / "runtime" / "team" / "mailboxes")
    monkeypatch.chdir(tmp_path)
    state = TeammateState(name="Carol", require_plan=False)
    BUS.send(LEADER, "Carol", "ordinary work", "message")
    BUS.send(LEADER, "Carol", "stop", "shutdown_request")

    assert _idle_loop(state) is None
    assert state.messages == []
