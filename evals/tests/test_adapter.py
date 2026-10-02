from pathlib import Path

from evals.adapters.codeharness import CodeHarnessAdapter
from evals.runner.case import EvalCase, Verification


class FakeGoalController:
    def __init__(self, calls):
        self.calls = calls

    def set_goal(self, goal):
        self.calls.append(("goal", goal))


class FakeHarness:
    instances = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.goal_controller = FakeGoalController(self.calls)
        self.session_stats = {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "tool_calls": 2,
        }
        self.__class__.instances.append(self)

    def start(self):
        self.calls.append("start")
        return self

    def run(self, task):
        self.calls.append(("run", task))
        return "finished"

    def close(self):
        self.calls.append("close")
        return True


def _case(goal=None):
    return EvalCase(
        case_id="case",
        category="bugfix",
        description="case",
        task="Fix it.",
        fixture_dir=Path("/fixture"),
        timeout_seconds=10,
        verification=Verification(("python", "-c", "pass")),
        goal=goal,
    )


def test_adapter_builds_case_scoped_runtime_and_runs_optional_goal(tmp_path):
    FakeHarness.instances.clear()
    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    mcp_config = tmp_path / "mcp.json"
    environment = {
        "DEEPSEEK_API_KEY": "secret",
        "DEEPSEEK_BASE_URL": "https://models.example/v1",
        "DEEPSEEK_MODEL_ID": "deepseek-test",
        "MODEL_CONTEXT_WINDOW": "8192",
        "GOAL_EVALUATOR_MODEL": "judge-test",
        "MCP_CONFIG_PATH": str(mcp_config),
    }
    adapter = CodeHarnessAdapter(
        environment=environment,
        harness_factory=FakeHarness,
    )

    result = adapter.run(_case("Tests pass"), workspace, agent_home)

    harness = FakeHarness.instances[-1]
    assert harness.config.workspace == workspace.resolve()
    assert harness.config.agent_home == agent_home.resolve()
    assert harness.config.mcp_config_path == mcp_config.resolve()
    assert harness.config.model == "deepseek-test"
    assert harness.config.model_context_window == 8192
    assert harness.config.evaluator_model == "judge-test"
    assert harness.calls == [
        "start",
        ("goal", "Tests pass"),
        ("run", "Fix it."),
        "close",
    ]
    assert result.final_answer == "finished"
    assert result.session_stats["total_tokens"] == 14


def test_adapter_skips_goal_when_case_has_none(tmp_path):
    FakeHarness.instances.clear()
    environment = {
        "DEEPSEEK_API_KEY": "secret",
        "DEEPSEEK_BASE_URL": "https://models.example/v1",
        "DEEPSEEK_MODEL_ID": "deepseek-test",
    }
    adapter = CodeHarnessAdapter(environment, harness_factory=FakeHarness)

    adapter.run(_case(), tmp_path / "workspace", tmp_path / "home")

    calls = FakeHarness.instances[-1].calls
    assert not any(isinstance(call, tuple) and call[0] == "goal" for call in calls)
