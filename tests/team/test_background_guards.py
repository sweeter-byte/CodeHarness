from codeharness.team.protocol import GATE_APPROVED, GATE_REQUIRED
from codeharness.team.teammate import TeammateState, make_teammate_handlers


class RecordingBackgroundManager:
    def __init__(self):
        self.calls = []

    def start(self, command, cwd=None):
        self.calls.append((command, cwd))
        return "bg_001", None


def test_background_bash_is_blocked_without_assignment():
    state = TeammateState(name="Alice", require_plan=False)
    background = RecordingBackgroundManager()
    handler = make_teammate_handlers(state, background)["bash"]

    result = handler(command="pwd", run_in_background=True)

    assert "No task assigned" in result
    assert background.calls == []


def test_background_bash_is_blocked_until_plan_is_approved(tmp_path):
    state = TeammateState(name="Alice", require_plan=True)
    state.assignment = {"task_id": "task_1", "cwd": str(tmp_path)}
    state.plan_gate = GATE_REQUIRED
    background = RecordingBackgroundManager()
    handler = make_teammate_handlers(state, background)["bash"]

    blocked = handler(command="pwd", run_in_background=True)

    assert "Blocked: plan status is required" in blocked
    assert background.calls == []

    state.plan_gate = GATE_APPROVED
    started = handler(command="pwd", run_in_background=True)

    assert started == "[Background task bg_001 started: pwd]"
    assert background.calls == [("pwd", str(tmp_path))]


def test_background_bash_uses_current_assignment_cwd(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    state = TeammateState(name="Alice", require_plan=False)
    state.assignment = {"task_id": "task_a", "cwd": str(first)}
    background = RecordingBackgroundManager()
    handler = make_teammate_handlers(state, background)["bash"]

    handler(command="pwd", run_in_background=True)
    state.assignment = {"task_id": "task_b", "cwd": str(second)}
    handler(command="pwd", run_in_background=True)

    assert background.calls == [("pwd", str(first)), ("pwd", str(second))]
