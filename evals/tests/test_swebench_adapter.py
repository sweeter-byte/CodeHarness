import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.adapters.codeharness import AdapterResult
from evals.runner.runner import WorkerProcessResult
from evals.swebench.adapter import build_parser, run_single_instance
from evals.swebench.dataset import (
    DEFAULT_DATASET_NAME,
    DEFAULT_SPLIT,
    SWEbenchDependencyError,
    SWEbenchInstance,
    load_instance,
)
from evals.swebench.evaluate import (
    OfficialEvaluationError,
    build_evaluator_command,
    preflight_evaluator,
)
from evals.swebench.prediction import build_prediction, write_prediction
from evals.swebench.worker import TASK_PREFIX, run_worker
from evals.swebench.workspace import (
    PreparedSWEbenchWorkspace,
    SWEbenchWorkspaceManager,
    WorkspacePreparationError,
)


def _raw_instance(**overrides):
    payload = {
        "instance_id": "owner__repo-123",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Fix the public behavior.",
        "patch": "GOLD PATCH MUST STAY PRIVATE",
        "test_patch": "TEST PATCH MUST STAY PRIVATE",
        "version": "1.0",
    }
    payload.update(overrides)
    return payload


def _instance(**overrides):
    return SWEbenchInstance.from_raw(_raw_instance(**overrides))


def test_loader_selects_one_instance_and_preserves_raw_metadata():
    calls = []

    def fake_loader(name, *, split):
        calls.append((name, split))
        return [_raw_instance(instance_id="other"), _raw_instance()]

    instance = load_instance(
        "owner__repo-123",
        dataset_loader=fake_loader,
    )

    assert calls == [(DEFAULT_DATASET_NAME, DEFAULT_SPLIT)]
    assert instance.instance_id == "owner__repo-123"
    assert instance.repo == "owner/repo"
    assert instance.base_commit == "a" * 40
    assert instance.problem_statement == "Fix the public behavior."
    assert instance.raw["version"] == "1.0"


def test_loader_reports_missing_instance_clearly():
    with pytest.raises(LookupError, match="SWE-bench instance not found: missing"):
        load_instance("missing", dataset_loader=lambda *_args, **_kwargs: [])


def test_loader_reports_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "datasets", None)

    with pytest.raises(
        SWEbenchDependencyError,
        match="SWE-bench evaluation dependencies are not installed\\.",
    ):
        load_instance("owner__repo-123")


def test_workspace_root_must_be_outside_codeharness_repository(tmp_path):
    repository_root = tmp_path / "codeharness"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="outside the CodeHarness repository"):
        SWEbenchWorkspaceManager(
            run_id="run-1",
            work_root=repository_root / "work",
            repository_root=repository_root,
        )


class RecordingGitRunner:
    def __init__(self, *, head=None, status="", failures=None):
        self.head = head or "a" * 40
        self.status = status
        self.failures = failures or {}
        self.calls = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, Path(kwargs["cwd"])))
        key = tuple(command[:2])
        if key in self.failures:
            return subprocess.CompletedProcess(
                command, self.failures[key], stdout="", stderr="command failed"
            )
        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir()
            stdout = ""
        elif command[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = f"{self.head}\n"
        elif command[:3] == ["git", "status", "--porcelain"]:
            stdout = self.status
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_workspace_clones_checks_out_and_verifies_exact_head(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    runner = RecordingGitRunner()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=source_root,
        command_runner=runner,
    )

    prepared = manager.prepare(_instance())

    assert (
        prepared.workspace
        == (tmp_path / "work/run-1/owner__repo-123/workspace").resolve()
    )
    assert (
        prepared.agent_home
        == (tmp_path / "work/run-1/owner__repo-123/agent-home").resolve()
    )
    assert prepared.agent_home.is_dir()
    assert runner.calls == [
        (
            [
                "git",
                "clone",
                "https://github.com/owner/repo.git",
                str(prepared.workspace),
            ],
            prepared.case_root,
        ),
        (
            ["git", "checkout", "--detach", "a" * 40],
            prepared.workspace,
        ),
        (["git", "rev-parse", "HEAD"], prepared.workspace),
        (["git", "status", "--porcelain"], prepared.workspace),
    ]


def test_workspace_rejects_head_mismatch(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=source_root,
        command_runner=RecordingGitRunner(head="b" * 40),
    )

    with pytest.raises(WorkspacePreparationError, match="HEAD mismatch"):
        manager.prepare(_instance())


def test_workspace_rejects_dirty_checkout(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=source_root,
        command_runner=RecordingGitRunner(status=" M changed.py\n"),
    )

    with pytest.raises(WorkspacePreparationError, match="not clean"):
        manager.prepare(_instance())


def test_workspace_reports_clone_failure_clearly(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=source_root,
        command_runner=RecordingGitRunner(failures={("git", "clone"): 128}),
    )

    with pytest.raises(WorkspacePreparationError, match="git clone failed"):
        manager.prepare(_instance())


class SuccessfulTaskAdapter:
    calls = []  # noqa: RUF012 - shared fake call log

    def __init__(self, environment):
        self.environment = environment

    def run_task(self, task, workspace, agent_home):
        self.__class__.calls.append((task, workspace, agent_home, self.environment))
        return AdapterResult(
            final_answer="done",
            session_stats={
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "total_tokens": 12,
                "tool_calls": 3,
            },
        )


class FailingTaskAdapter:
    def __init__(self, environment):
        self.environment = environment

    def run_task(self, task, workspace, agent_home):
        raise RuntimeError(f"provider rejected {self.environment['DEEPSEEK_API_KEY']}")


def test_swebench_worker_passes_only_problem_prompt_and_case_paths(tmp_path):
    SuccessfulTaskAdapter.calls.clear()
    workspace = tmp_path / "workspace"
    agent_home = tmp_path / "agent-home"
    workspace.mkdir()
    result_path = tmp_path / "worker-result.json"

    exit_code = run_worker(
        "Fix this issue only.",
        workspace,
        result_path,
        agent_home=agent_home,
        environment={"DEEPSEEK_API_KEY": "key"},
        adapter_factory=SuccessfulTaskAdapter,
    )

    task, used_workspace, used_home, _ = SuccessfulTaskAdapter.calls[-1]
    assert exit_code == 0
    assert task == f"{TASK_PREFIX}\n\nFix this issue only."
    assert "GOLD" not in task
    assert "TEST PATCH" not in task
    assert used_workspace == workspace.resolve()
    assert used_home == agent_home.resolve()
    assert json.loads(result_path.read_text())["session_stats"]["total_tokens"] == 12


def test_swebench_worker_redacts_api_key_from_error(tmp_path):
    secret = "worker-secret"
    result_path = tmp_path / "worker-result.json"

    exit_code = run_worker(
        "Fix it.",
        tmp_path / "workspace",
        result_path,
        agent_home=tmp_path / "agent-home",
        environment={"DEEPSEEK_API_KEY": secret},
        adapter_factory=FailingTaskAdapter,
    )

    raw = result_path.read_text()
    assert exit_code == 1
    assert secret not in raw
    assert "***REDACTED***" in raw


def test_prediction_jsonl_uses_official_three_field_format(tmp_path):
    prediction = build_prediction(
        instance_id="owner__repo-123",
        model="deepseek-test",
        model_patch="diff --git a/a.py b/a.py\n",
    )
    path = tmp_path / "prediction.jsonl"

    write_prediction(path, prediction)

    assert prediction == {
        "instance_id": "owner__repo-123",
        "model_name_or_path": "CodeHarness::deepseek-test",
        "model_patch": "diff --git a/a.py b/a.py\n",
    }
    assert json.loads(path.read_text()) == prediction
    assert path.read_text().endswith("\n")


def test_prediction_preserves_empty_patch():
    prediction = build_prediction("case", "model", "")

    assert prediction["model_patch"] == ""


def test_official_evaluator_command_is_single_instance(tmp_path):
    command = build_evaluator_command(
        dataset_name="SWE-bench/SWE-bench_Lite",
        split="test",
        predictions_path=tmp_path / "prediction.jsonl",
        instance_id="owner__repo-123",
        run_id="run-1",
        python_executable="/python",
    )

    assert command == [
        "/python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "SWE-bench/SWE-bench_Lite",
        "--split",
        "test",
        "--predictions_path",
        str(tmp_path / "prediction.jsonl"),
        "--instance_ids",
        "owner__repo-123",
        "--max_workers",
        "1",
        "--run_id",
        "run-1",
    ]


def test_evaluator_preflight_reports_missing_swebench():
    with pytest.raises(
        SWEbenchDependencyError,
        match="SWE-bench evaluation dependencies are not installed\\.",
    ):
        preflight_evaluator(
            module_finder=lambda _name: None,
            executable_finder=lambda _name: "/docker",
            command_runner=lambda *_args, **_kwargs: None,
        )


def test_evaluator_preflight_reports_missing_docker():
    with pytest.raises(OfficialEvaluationError, match="Docker executable"):
        preflight_evaluator(
            module_finder=lambda _name: object(),
            executable_finder=lambda _name: None,
            command_runner=lambda *_args, **_kwargs: None,
        )


def test_evaluator_preflight_reports_unavailable_daemon():
    def failed_info(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")

    with pytest.raises(OfficialEvaluationError, match="Docker daemon"):
        preflight_evaluator(
            module_finder=lambda _name: object(),
            executable_finder=lambda _name: "/docker",
            command_runner=failed_info,
        )


def _init_git_workspace(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "module.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


class StaticWorkspaceManager:
    def __init__(self, prepared):
        self.prepared = prepared
        self.instances = []

    def prepare(self, instance):
        self.instances.append(instance)
        return self.prepared


def _prepared_workspace(tmp_path):
    case_root = tmp_path / "work/run-1/owner__repo-123"
    workspace = case_root / "workspace"
    agent_home = case_root / "agent-home"
    _init_git_workspace(workspace)
    agent_home.mkdir()
    return PreparedSWEbenchWorkspace(case_root, workspace, agent_home)


def _worker_that_changes_file(captured, secret=None):
    def fake_runner(command, *, workspace, environment, timeout_seconds):
        captured.update(
            command=list(command),
            workspace=workspace,
            environment=dict(environment),
            timeout_seconds=timeout_seconds,
        )
        (workspace / "module.py").write_text("value = 2\n", encoding="utf-8")
        result_path = Path(command[command.index("--result") + 1])
        result_path.write_text(
            json.dumps(
                {
                    "final_answer": f"done {secret or ''}",
                    "session_stats": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                        "tool_calls": 1,
                    },
                    "worker_status": "completed",
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        return WorkerProcessResult(0, f"stdout {secret or ''}", "", False, 0.25)

    return fake_runner


def _environment(secret="adapter-secret"):
    return {
        "DEEPSEEK_API_KEY": secret,
        "DEEPSEEK_BASE_URL": "https://models.example/v1",
        "DEEPSEEK_MODEL_ID": "deepseek-test",
    }


def test_single_instance_run_writes_artifacts_without_official_evaluation(tmp_path):
    prepared = _prepared_workspace(tmp_path)
    manager = StaticWorkspaceManager(prepared)
    captured = {}
    evaluator_called = False
    secret = "adapter-secret"

    def unexpected_evaluator(**kwargs):
        nonlocal evaluator_called
        evaluator_called = True

    run_root, result = run_single_instance(
        instance_id="owner__repo-123",
        timeout_seconds=47,
        results_root=tmp_path / "results",
        environment=_environment(secret),
        run_id="run-1",
        dataset_loader=lambda *_args, **_kwargs: [_raw_instance()],
        workspace_manager=manager,
        worker_process_runner=_worker_that_changes_file(captured, secret),
        official_evaluator=unexpected_evaluator,
    )

    assert run_root == tmp_path / "results/swebench/run-1"
    assert manager.instances[0].instance_id == "owner__repo-123"
    assert captured["workspace"] == prepared.workspace
    assert captured["timeout_seconds"] == 47
    command = captured["command"]
    assert command[:3] == [sys.executable, "-m", "evals.swebench.worker"]
    problem = command[command.index("--problem-statement") + 1]
    assert problem == "Fix the public behavior."
    assert "GOLD PATCH" not in problem
    assert "TEST PATCH" not in problem
    assert captured["environment"]["CODEHARNESS_HOME"] == str(prepared.agent_home)
    mcp_path = Path(captured["environment"]["MCP_CONFIG_PATH"])
    assert mcp_path == prepared.agent_home / "mcp/servers.json"
    assert mcp_path.read_text() == "{}\n"
    prediction = json.loads((run_root / "prediction.jsonl").read_text())
    assert prediction["instance_id"] == "owner__repo-123"
    assert prediction["model_name_or_path"] == "CodeHarness::deepseek-test"
    assert "+value = 2" in prediction["model_patch"]
    assert (prepared.case_root / "prediction.jsonl").read_text() == (
        run_root / "prediction.jsonl"
    ).read_text()
    assert (run_root / "patch.diff").read_text() == prediction["model_patch"]
    assert result["worker_status"] == "completed"
    assert result["empty_patch"] is False
    assert result["official_evaluation"] is None
    assert result["total_tokens"] == 5
    assert evaluator_called is False
    for artifact in run_root.iterdir():
        if artifact.is_file():
            assert secret not in artifact.read_text(errors="replace")


def test_single_instance_run_calls_official_evaluator_only_when_requested(tmp_path):
    prepared = _prepared_workspace(tmp_path)
    calls = []

    def fake_evaluator(**kwargs):
        calls.append(kwargs)
        return {
            "command": ["python", "-m", "swebench.harness.run_evaluation"],
            "return_code": 0,
            "stdout": "evaluated",
            "stderr": "",
        }

    run_root, result = run_single_instance(
        instance_id="owner__repo-123",
        evaluate=True,
        results_root=tmp_path / "results",
        environment=_environment(),
        run_id="run-1",
        dataset_loader=lambda *_args, **_kwargs: [_raw_instance()],
        workspace_manager=StaticWorkspaceManager(prepared),
        worker_process_runner=_worker_that_changes_file({}),
        official_evaluator=fake_evaluator,
    )

    assert calls == [
        {
            "dataset_name": DEFAULT_DATASET_NAME,
            "split": DEFAULT_SPLIT,
            "predictions_path": run_root / "prediction.jsonl",
            "instance_id": "owner__repo-123",
            "run_id": "run-1",
        }
    ]
    assert result["official_evaluation"]["return_code"] == 0


def test_single_instance_run_records_empty_patch(tmp_path):
    prepared = _prepared_workspace(tmp_path)

    def unchanged_worker(command, *, workspace, environment, timeout_seconds):
        result_path = Path(command[command.index("--result") + 1])
        result_path.write_text(
            json.dumps(
                {
                    "final_answer": "no change",
                    "session_stats": {},
                    "worker_status": "completed",
                    "error": None,
                }
            )
        )
        return WorkerProcessResult(0, "", "", False, 0.1)

    run_root, result = run_single_instance(
        instance_id="owner__repo-123",
        results_root=tmp_path / "results",
        environment=_environment(),
        run_id="run-1",
        dataset_loader=lambda *_args, **_kwargs: [_raw_instance()],
        workspace_manager=StaticWorkspaceManager(prepared),
        worker_process_runner=unchanged_worker,
    )

    assert result["empty_patch"] is True
    assert json.loads((run_root / "prediction.jsonl").read_text())["model_patch"] == ""


def test_single_instance_run_records_worker_timeout(tmp_path):
    prepared = _prepared_workspace(tmp_path)

    def timed_out_worker(command, *, workspace, environment, timeout_seconds):
        return WorkerProcessResult(-15, "partial", "timed out", True, 1.1)

    _, result = run_single_instance(
        instance_id="owner__repo-123",
        timeout_seconds=1,
        results_root=tmp_path / "results",
        environment=_environment(),
        run_id="run-1",
        dataset_loader=lambda *_args, **_kwargs: [_raw_instance()],
        workspace_manager=StaticWorkspaceManager(prepared),
        worker_process_runner=timed_out_worker,
    )

    assert result["timeout"] is True
    assert result["worker_status"] == "timeout"


def test_cli_defaults_to_prediction_only_and_accepts_explicit_evaluate():
    parser = build_parser()

    default = parser.parse_args(["--instance-id", "case"])
    explicit = parser.parse_args(["--instance-id", "case", "--evaluate"])

    assert default.dataset_name == DEFAULT_DATASET_NAME
    assert default.split == DEFAULT_SPLIT
    assert default.evaluate is False
    assert explicit.evaluate is True
