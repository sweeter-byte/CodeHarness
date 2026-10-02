import json
from pathlib import Path

from evals.adapters.codeharness import AdapterResult
from evals.worker import run_worker


def _case_file(tmp_path):
    case_dir = tmp_path / "case"
    (case_dir / "fixture").mkdir(parents=True)
    path = case_dir / "case.json"
    path.write_text(
        json.dumps(
            {
                "case_id": "sample",
                "category": "bugfix",
                "description": "sample",
                "task": "Fix it.",
                "timeout_seconds": 10,
                "verification": {"command": ["python", "-c", "pass"]},
            }
        )
    )
    return path


class SuccessfulAdapter:
    def __init__(self, environment):
        self.environment = environment

    def run(self, case, workspace, agent_home):
        assert case.case_id == "sample"
        assert workspace.name == "workspace"
        assert agent_home.name == "agent-home"
        return AdapterResult(
            final_answer="done",
            session_stats={
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "total_tokens": 12,
                "tool_calls": 3,
            },
        )


class FailingAdapter:
    def __init__(self, environment):
        self.environment = environment

    def run(self, case, workspace, agent_home):
        raise RuntimeError(f"request failed using {self.environment['DEEPSEEK_API_KEY']}")


def test_worker_writes_completed_result_without_llm(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_home = tmp_path / "agent-home"
    result_path = tmp_path / "worker.json"
    environment = {"CODEHARNESS_HOME": str(agent_home)}

    exit_code = run_worker(
        _case_file(tmp_path),
        workspace,
        result_path,
        environment=environment,
        adapter_factory=SuccessfulAdapter,
    )

    result = json.loads(result_path.read_text())
    assert exit_code == 0
    assert result == {
        "final_answer": "done",
        "session_stats": {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
            "tool_calls": 3,
        },
        "worker_status": "completed",
        "error": None,
    }


def test_worker_redacts_api_key_from_failure_result(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result_path = tmp_path / "worker.json"
    secret = "super-secret-eval-key"
    environment = {
        "CODEHARNESS_HOME": str(tmp_path / "agent-home"),
        "DEEPSEEK_API_KEY": secret,
    }

    exit_code = run_worker(
        _case_file(tmp_path),
        workspace,
        result_path,
        environment=environment,
        adapter_factory=FailingAdapter,
    )

    raw = result_path.read_text()
    result = json.loads(raw)
    assert exit_code == 1
    assert result["worker_status"] == "error"
    assert result["final_answer"] == ""
    assert secret not in raw
    assert "***REDACTED***" in result["error"]
