"""Goal Loop readiness tests; no test calls a real LLM."""

from types import SimpleNamespace

from codeharness.core.agent import Agent
from codeharness.goal import EvalResult, GoalController, GoalStatus, StopDecision


class FakeEvaluator:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def evaluate(self, condition, messages):
        self.calls.append((condition, list(messages)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _controller(*outcomes, **kwargs):
    return GoalController(
        evaluator=FakeEvaluator(*outcomes),
        status_handler=lambda _message: None,
        **kwargs,
    )


def test_no_active_goal_allows_exit():
    controller = _controller(EvalResult(ok=False, reason="unused"))
    assert controller.evaluate_after_turn([]) is StopDecision.ALLOW
    assert controller._evaluator.calls == []


def test_evaluator_success_marks_goal_achieved():
    controller = _controller(EvalResult(ok=True, reason="tests passed"))
    controller.set_goal("tests pass")
    decision = controller.evaluate_after_turn([{"role": "tool", "content": "ok"}])
    assert decision is StopDecision.ACHIEVED
    assert controller._goal.status is GoalStatus.ACHIEVED
    assert controller.has_active_goal is False


def test_evaluator_not_ok_blocks_exit():
    controller = _controller(EvalResult(ok=False, reason="one test failing"))
    controller.set_goal("tests pass")
    assert controller.evaluate_after_turn([]) is StopDecision.BLOCK
    assert controller._goal.consecutive_blocks == 1
    assert "one test failing" in controller.build_feedback_message()


def test_evaluator_impossible_marks_goal_failed():
    controller = _controller(
        EvalResult(ok=False, impossible=True, reason="dependency unavailable")
    )
    controller.set_goal("deploy")
    assert controller.evaluate_after_turn([]) is StopDecision.FAILED
    assert controller._goal.status is GoalStatus.FAILED


def test_evaluator_exception_returns_error():
    controller = _controller(RuntimeError("judge unavailable"))
    controller.set_goal("tests pass")
    assert controller.evaluate_after_turn([]) is StopDecision.ERROR
    assert "judge unavailable" in controller._goal.last_reason


def test_consecutive_block_limit_stops_auto_continuation():
    controller = _controller(
        EvalResult(ok=False, reason="first"),
        EvalResult(ok=False, reason="second"),
        max_blocks=2,
    )
    controller.set_goal("tests pass")
    assert controller.evaluate_after_turn([]) is StopDecision.BLOCK
    assert controller.evaluate_after_turn([]) is StopDecision.BLOCK
    assert controller.evaluate_after_turn([]) is StopDecision.LIMIT
    assert len(controller._evaluator.calls) == 2


def test_running_background_manager_defers_evaluation():
    controller = _controller(EvalResult(ok=True, reason="unused"))
    controller.set_goal("tests pass")
    background = SimpleNamespace(tasks={"bg_001": {"status": "running"}})
    assert (
        controller.evaluate_after_turn([], background_manager=background)
        is StopDecision.DEFER
    )
    assert controller._evaluator.calls == []


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None

    def model_dump(self):
        return {"role": "assistant", "content": self.content}


def test_block_feedback_is_appended_and_agent_loop_continues(monkeypatch):
    evaluator = FakeEvaluator(
        EvalResult(ok=False, reason="missing verification"),
        EvalResult(ok=True, reason="verified"),
    )
    controller = GoalController(evaluator=evaluator, status_handler=lambda _: None)
    controller.set_goal("tests pass")
    agent = Agent(
        client=object(),
        model="test",
        model_context_window=4096,
        tools=[],
        handlers={},
        memory_manager=False,
        goal_controller=controller,
    )
    responses = iter(
        [
            SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage("first"))]),
            SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage("done"))]),
        ]
    )
    monkeypatch.setattr(agent, "_call_llm", lambda _messages: next(responses))
    messages = [{"role": "user", "content": "fix it"}]
    final_answer = agent.agent_loop(messages)
    assert final_answer == "done"
    feedback = [
        message["content"]
        for message in messages
        if message.get("role") == "user"
        and "<goal_gate>" in str(message.get("content"))
    ]
    assert len(feedback) == 1
    assert "missing verification" in feedback[0]
    assert len(evaluator.calls) == 2


def test_secondary_agents_use_null_controller_and_do_not_activate_gate():
    agent = Agent(
        client=object(),
        model="test",
        model_context_window=4096,
        tools=[],
        handlers={},
        memory_manager=False,
    )
    controller = agent.goal_controller
    controller.set_goal("this must not gate subagents")
    assert getattr(controller, "_null_mode") is True
    assert controller.evaluate_after_turn([]) is StopDecision.ALLOW
