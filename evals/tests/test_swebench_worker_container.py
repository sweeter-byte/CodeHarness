import inspect
import json
from typing import ClassVar

import pytest

from codeharness.app import CodeHarness
from evals.adapters.codeharness import AdapterResult
from evals.swebench import worker as worker_module


def _environment() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "test-key",
        "DEEPSEEK_BASE_URL": "https://models.example.invalid/v1",
        "DEEPSEEK_MODEL_ID": "test-model",
    }


class _RecordingHarness:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        run_error: Exception | None = None,
    ) -> None:
        self.start_error = start_error
        self.run_error = run_error
        self.started = False
        self.closed = False
        self.tasks: list[str] = []
        self.approval_handler = None
        self.session_stats = {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
            "tool_calls": 1,
        }

    def set_approval_handler(self, handler) -> None:
        self.approval_handler = handler

    def start(self) -> None:
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    def run(self, task: str) -> str:
        self.tasks.append(task)
        if self.run_error is not None:
            raise self.run_error
        return "done"

    def close(self) -> None:
        self.closed = True


class _FakeMCPManager:
    def __init__(self, configs) -> None:
        self.configs = configs

    def connect_all(self) -> None:
        pass

    def status_lines(self) -> list[str]:
        return []

    def assemble(self, names) -> tuple[list, dict]:
        return [], {}

    def resolve(self, prefixed):
        return None

    def annotations_of(self, prefixed):
        return None

    def close_all(self) -> None:
        pass


def test_container_mode_builds_one_runtime_against_one_borrowed_container(
    monkeypatch, tmp_path
):
    backend_ids = []
    worktree_ids = []
    factory_calls = []
    harness = _RecordingHarness()

    class RecordingBackend:
        def __init__(self, container):
            self.container = container
            backend_ids.append(container)

    class RecordingWorktreeEnvironment:
        def __init__(self, container):
            self.container = container
            worktree_ids.append(container)

    def harness_factory(config, **kwargs):
        factory_calls.append((config, kwargs))
        return harness

    monkeypatch.setattr(
        worker_module, "DockerWorkspaceBackend", RecordingBackend, raising=False
    )
    monkeypatch.setattr(
        worker_module,
        "DockerWorktreeEnvironment",
        RecordingWorktreeEnvironment,
        raising=False,
    )
    host_workspace = tmp_path / "host-runtime-state"
    adapter = worker_module.SWEbenchCodeHarnessAdapter(
        _environment(), harness_factory=harness_factory
    )

    result = adapter.run_task(
        "fix it",
        host_workspace,
        tmp_path / "agent-home",
        container_id="borrowed-container",
    )

    config, kwargs = factory_calls[-1]
    assert backend_ids == ["borrowed-container"]
    assert worktree_ids == ["borrowed-container"]
    assert kwargs["workspace_backend"].container == "borrowed-container"
    assert kwargs["worktree_environment"].container == "borrowed-container"
    assert kwargs["tool_workspace"] == "/testbed"
    assert kwargs["tool_worktrees"] == "/tmp/codeharness-worktrees"
    assert config.workspace == host_workspace.resolve()
    assert harness.started is True
    assert harness.tasks == ["fix it"]
    assert harness.closed is True
    assert harness.approval_handler is not None
    assert harness.approval_handler("File modification: write_file") is True
    assert harness.approval_handler("File modification: edit_file") is True
    assert harness.approval_handler(
        "Path outside allowed directories: /tmp/outside.py"
    ) is False
    assert harness.approval_handler(
        r"Command matches approval rule: \brm\s+"
    ) is False
    assert result == AdapterResult("done", harness.session_stats)


def test_host_mode_keeps_legacy_single_argument_harness_factory(tmp_path):
    calls = []
    harness = _RecordingHarness()

    def harness_factory(*args, **kwargs):
        calls.append((args, kwargs))
        return harness

    adapter = worker_module.SWEbenchCodeHarnessAdapter(
        _environment(), harness_factory=harness_factory
    )

    adapter.run_task("fix it", tmp_path / "workspace", tmp_path / "agent-home")

    assert len(calls) == 1
    assert len(calls[0][0]) == 1
    assert calls[0][1] == {}
    assert harness.approval_handler is None


def test_container_mode_only_approves_boundary_checked_file_mutations(
    monkeypatch, tmp_path
):
    from codeharness import app as app_module

    observations = {}

    class RecordingBackend:
        def __init__(self, container):
            self.container = container
            self.calls = []

        def bash(self, command, run_in_background=False, cwd=None):
            self.calls.append(("bash", command, run_in_background, cwd))
            return "container bash"

        def read_file(self, path, start_line=None, end_line=None, cwd=None):
            self.calls.append(("read_file", path, start_line, end_line, cwd))
            return "container read"

        def write_file(self, path, content, cwd=None):
            self.calls.append(("write_file", path, content, cwd))
            return "container write"

        def edit_file(self, path, old_text, new_text, cwd=None):
            self.calls.append(("edit_file", path, old_text, new_text, cwd))
            return "container edit"

        def glob(self, pattern, cwd=None):
            self.calls.append(("glob", pattern, cwd))
            return "container glob"

        def grep(self, pattern, path=".", file_pattern=None, cwd=None):
            self.calls.append(("grep", pattern, path, file_pattern, cwd))
            return "container grep"

    class RecordingWorktreeEnvironment:
        def __init__(self, container):
            self.container = container

        def git(self, args, cwd):
            return 0, ""

        def path_exists(self, path):
            return False

    class InspectingHarness(CodeHarness):
        def run(self, task):
            messages = []
            operations = [
                (
                    "write_file",
                    {"path": "/testbed/inside.py", "content": "x"},
                ),
                (
                    "edit_file",
                    {
                        "path": (
                            "/tmp/codeharness-worktrees/task-a/inside.py"
                        ),
                        "old_text": "x",
                        "new_text": "y",
                    },
                ),
                (
                    "write_file",
                    {"path": "/tmp/outside.py", "content": "x"},
                ),
                (
                    "write_file",
                    {"path": "/opt/outside.py", "content": "x"},
                ),
                (
                    "write_file",
                    {
                        "path": "/testbed/../tmp/escape.py",
                        "content": "x",
                    },
                ),
            ]
            results = []
            for index, (tool_name, args) in enumerate(operations):
                results.append(
                    self.agent._execute_tool(
                        self.agent.handlers[tool_name],
                        f"call-{index}",
                        tool_name,
                        args,
                        messages,
                    )
                )
            observations.update(
                results=results,
                messages=messages,
                backend=self.workspace_backend,
            )
            return "inspected"

    monkeypatch.setattr(worker_module, "DockerWorkspaceBackend", RecordingBackend)
    monkeypatch.setattr(
        worker_module,
        "DockerWorktreeEnvironment",
        RecordingWorktreeEnvironment,
    )
    monkeypatch.setattr(app_module, "OpenAI", lambda **kwargs: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.cron, "start", lambda **kwargs: None)
    monkeypatch.setattr(app_module.cron, "stop", lambda: True)
    monkeypatch.setattr(app_module.team_wakeup, "start", lambda **kwargs: None)
    monkeypatch.setattr(app_module.team_wakeup, "stop", lambda: True)
    adapter = worker_module.SWEbenchCodeHarnessAdapter(
        _environment(), harness_factory=InspectingHarness
    )

    result = adapter.run_task(
        "inspect approval policy",
        tmp_path / "host-runtime-state",
        tmp_path / "agent-home",
        container_id="borrowed-container",
    )

    assert result.final_answer == "inspected"
    assert observations["results"] == [True, True, False, False, False]
    assert observations["backend"].calls == [
        ("write_file", "/testbed/inside.py", "x", None),
        (
            "edit_file",
            "/tmp/codeharness-worktrees/task-a/inside.py",
            "x",
            "y",
            None,
        ),
    ]
    rejected_messages = observations["messages"][2:]
    assert len(rejected_messages) == 3
    assert all(
        "User rejected" in message["content"]
        for message in rejected_messages
    )


def test_container_mode_runtime_keeps_leader_subagent_team_and_workflow_tools(
    monkeypatch, tmp_path
):
    from codeharness import app as app_module
    from codeharness.team import TEAM
    from codeharness.tools import coding

    observations = {}

    class RecordingBackend:
        def __init__(self, container):
            self.container = container
            self.calls = []

        def bash(self, command, run_in_background=False, cwd=None):
            self.calls.append(("bash", command, run_in_background, cwd))
            return "container bash"

        def read_file(self, path, start_line=None, end_line=None, cwd=None):
            self.calls.append(("read_file", path, start_line, end_line, cwd))
            return "container read"

        def write_file(self, path, content, cwd=None):
            self.calls.append(("write_file", path, content, cwd))
            return "container write"

        def edit_file(self, path, old_text, new_text, cwd=None):
            self.calls.append(("edit_file", path, old_text, new_text, cwd))
            return "container edit"

        def glob(self, pattern, cwd=None):
            self.calls.append(("glob", pattern, cwd))
            return "container glob"

        def grep(self, pattern, path=".", file_pattern=None, cwd=None):
            self.calls.append(("grep", pattern, path, file_pattern, cwd))
            return "container grep"

    class RecordingWorktreeEnvironment:
        def __init__(self, container):
            self.container = container

        def git(self, args, cwd):
            return 0, ""

        def path_exists(self, path):
            return False

    class InspectingHarness(CodeHarness):
        def run(self, task):
            child = self.create_agent(memory_manager=False)
            observations.update(
                task=task,
                names=self.registry.names(),
                workflow_registry=self.workflow_registry,
                workflow_tools=self.workflow_runtime._tool_registry,
                team_backend=TEAM._workspace_backend,
                leader_read=self.agent.handlers["read_file"](path="leader.py"),
                child_read=child.handlers["read_file"](path="child.py"),
                backend=self.workspace_backend,
                worktree_environment=self.worktree_environment,
            )
            return "inspected"

    monkeypatch.setattr(worker_module, "DockerWorkspaceBackend", RecordingBackend)
    monkeypatch.setattr(
        worker_module,
        "DockerWorktreeEnvironment",
        RecordingWorktreeEnvironment,
    )
    monkeypatch.setattr(app_module, "OpenAI", lambda **kwargs: object())
    monkeypatch.setattr(app_module, "load_config", lambda path: {})
    monkeypatch.setattr(app_module, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(app_module, "shutdown_runtime", lambda: None)
    monkeypatch.setattr(app_module.cron, "start", lambda **kwargs: None)
    monkeypatch.setattr(app_module.cron, "stop", lambda: True)
    monkeypatch.setattr(app_module.team_wakeup, "start", lambda **kwargs: None)
    monkeypatch.setattr(app_module.team_wakeup, "stop", lambda: True)
    monkeypatch.setattr(
        coding,
        "run_read",
        lambda *args, **kwargs: pytest.fail("host read implementation called"),
    )
    adapter = worker_module.SWEbenchCodeHarnessAdapter(
        _environment(), harness_factory=InspectingHarness
    )

    result = adapter.run_task(
        "inspect wiring",
        tmp_path / "host-runtime-state",
        tmp_path / "agent-home",
        container_id="borrowed-container",
    )

    assert result.final_answer == "inspected"
    assert observations["task"] == "inspect wiring"
    assert {
        "task",
        "create_task",
        "spawn_teammate",
        "start_workflow",
        "workflow_status",
    } <= observations["names"]
    assert "review-changes" in observations["workflow_registry"]
    assert observations["workflow_tools"] is not None
    assert observations["team_backend"] is observations["backend"]
    assert observations["leader_read"] == "container read"
    assert observations["child_read"] == "container read"
    assert observations["backend"].container == "borrowed-container"
    assert observations["worktree_environment"].container == "borrowed-container"
    assert observations["backend"].calls == [
        ("read_file", "leader.py", None, None, None),
        ("read_file", "child.py", None, None, None),
    ]


def test_container_mode_closes_runtime_when_execution_fails(tmp_path):
    harness = _RecordingHarness(run_error=RuntimeError("runtime failed"))
    adapter = worker_module.SWEbenchCodeHarnessAdapter(
        _environment(), harness_factory=lambda config, **kwargs: harness
    )

    with pytest.raises(RuntimeError, match="runtime failed"):
        adapter.run_task(
            "fix it",
            tmp_path / "host-runtime-state",
            tmp_path / "agent-home",
            container_id="borrowed-container",
        )

    assert harness.closed is True


def test_container_start_failure_writes_error_result_and_closes_runtime(tmp_path):
    harness = _RecordingHarness(start_error=RuntimeError("start failed"))

    def adapter_factory(environment):
        return worker_module.SWEbenchCodeHarnessAdapter(
            environment,
            harness_factory=lambda config, **kwargs: harness,
        )

    result_path = tmp_path / "worker-result.json"

    exit_code = worker_module.run_worker(
        "Fix it.",
        tmp_path / "host-runtime-state",
        result_path,
        agent_home=tmp_path / "agent-home",
        container_id="borrowed-container",
        environment=_environment(),
        adapter_factory=adapter_factory,
    )

    payload = json.loads(result_path.read_text())
    assert exit_code == 1
    assert harness.started is True
    assert harness.closed is True
    assert payload["worker_status"] == "error"
    assert payload["final_answer"] == ""
    assert payload["session_stats"] == worker_module.ZERO_STATS
    assert payload["error"] == "RuntimeError: start failed"


class _RecordingContainerAdapter:
    calls: ClassVar[list] = []

    def __init__(self, environment):
        self.environment = environment

    def run_task(self, task, workspace, agent_home, *, container_id=None):
        self.__class__.calls.append(
            (task, workspace, agent_home, container_id, self.environment)
        )
        return AdapterResult(
            final_answer="done",
            session_stats={
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "tool_calls": 4,
            },
        )


def test_run_worker_forwards_borrowed_container_and_keeps_result_contract(tmp_path):
    _RecordingContainerAdapter.calls.clear()
    result_path = tmp_path / "worker-result.json"

    exit_code = worker_module.run_worker(
        "Fix it.",
        tmp_path / "host-runtime-state",
        result_path,
        agent_home=tmp_path / "agent-home",
        container_id="borrowed-container",
        environment=_environment(),
        adapter_factory=_RecordingContainerAdapter,
    )

    task, workspace, agent_home, container_id, environment = (
        _RecordingContainerAdapter.calls[-1]
    )
    assert exit_code == 0
    assert task == f"{worker_module.TASK_PREFIX}\n\nFix it."
    assert workspace == (tmp_path / "host-runtime-state").resolve()
    assert agent_home == (tmp_path / "agent-home").resolve()
    assert container_id == "borrowed-container"
    assert environment == _environment()
    assert json.loads(result_path.read_text()) == {
        "final_answer": "done",
        "session_stats": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "tool_calls": 4,
        },
        "worker_status": "completed",
        "error": None,
    }


@pytest.mark.parametrize("container_id", ["", "   "])
def test_empty_container_id_is_an_error_without_host_fallback(
    tmp_path, container_id
):
    factory_called = False

    def unexpected_harness_factory(*args, **kwargs):
        nonlocal factory_called
        factory_called = True
        pytest.fail("empty container ID silently fell back to a host Runtime")

    class Adapter(worker_module.SWEbenchCodeHarnessAdapter):
        def __init__(self, environment):
            super().__init__(environment, harness_factory=unexpected_harness_factory)

    result_path = tmp_path / "worker-result.json"

    exit_code = worker_module.run_worker(
        "Fix it.",
        tmp_path / "host-runtime-state",
        result_path,
        agent_home=tmp_path / "agent-home",
        container_id=container_id,
        environment=_environment(),
        adapter_factory=Adapter,
    )

    payload = json.loads(result_path.read_text())
    assert exit_code == 1
    assert factory_called is False
    assert payload["worker_status"] == "error"
    assert payload["final_answer"] == ""
    assert payload["session_stats"] == worker_module.ZERO_STATS
    assert "container" in payload["error"].lower()


def test_worker_cli_passes_container_id_explicitly(monkeypatch, tmp_path):
    calls = []

    def fake_run_worker(*args, **kwargs):
        calls.append((args, kwargs))
        return 17

    monkeypatch.setattr(worker_module, "run_worker", fake_run_worker)

    exit_code = worker_module.main(
        [
            "--problem-statement",
            "Fix it.",
            "--workspace",
            str(tmp_path / "host-runtime-state"),
            "--agent-home",
            str(tmp_path / "agent-home"),
            "--container-id",
            "borrowed-container",
            "--result",
            str(tmp_path / "worker-result.json"),
        ]
    )

    assert exit_code == 17
    assert calls[-1][1]["container_id"] == "borrowed-container"


def test_worker_surface_excludes_private_dataset_and_container_lifecycle_data():
    forbidden = {
        "test_patch",
        "fail_to_pass",
        "pass_to_pass",
        "eval_script",
        "gold_patch",
        "image",
    }
    parser_dests = {
        action.dest for action in worker_module.build_parser()._actions
    }
    run_worker_parameters = set(inspect.signature(worker_module.run_worker).parameters)
    run_task_parameters = set(
        inspect.signature(
            worker_module.SWEbenchCodeHarnessAdapter.run_task
        ).parameters
    )

    assert forbidden.isdisjoint(parser_dests)
    assert forbidden.isdisjoint(run_worker_parameters)
    assert forbidden.isdisjoint(run_task_parameters)
    assert not hasattr(worker_module, "SWEbenchRolloutContainer")
    assert not hasattr(worker_module, "subprocess")
    source = inspect.getsource(worker_module).lower()
    assert "rollout_container" not in source
    assert all(
        command not in source
        for command in (
            "docker create",
            "docker start",
            "docker stop",
            "docker rm",
            "docker pull",
        )
    )
