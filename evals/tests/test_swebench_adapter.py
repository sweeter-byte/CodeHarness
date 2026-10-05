import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import evals.swebench.adapter as swebench_adapter
from evals.adapters.codeharness import AdapterResult
from evals.runner.runner import WorkerProcessResult
from evals.runner.verifier import capture_patch
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
    SWEbenchRuntimeWorkspaceManager,
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
        "FAIL_TO_PASS": '["test_failure"]',
        "PASS_TO_PASS": '["test_regression"]',
        "eval_script": "hidden evaluator script",
        "hints_text": "hidden hint",
        "image": "swebench/sweb.eval.x86_64.owner_1776_repo-123:latest",
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


def test_runtime_workspace_creates_only_host_state_directories(tmp_path):
    manager = SWEbenchRuntimeWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
    )

    prepared = manager.prepare(_instance())

    assert prepared.case_root == (
        tmp_path / "work/run-1/owner__repo-123"
    ).resolve()
    assert prepared.workspace == prepared.case_root / "runtime-state"
    assert prepared.agent_home == prepared.case_root / "agent-home"
    assert prepared.workspace.is_dir()
    assert prepared.agent_home.is_dir()
    assert not (prepared.case_root / "workspace").exists()
    assert not (prepared.workspace / ".git").exists()


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
        elif command[:3] == ["git", "rev-parse", "--git-path"]:
            stdout = ".git/info/exclude\n"
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
        (
            ["git", "rev-parse", "--git-path", "info/exclude"],
            prepared.workspace,
        ),
        (["git", "status", "--porcelain"], prepared.workspace),
    ]


def test_workspace_preparation_ignores_runtime_artifacts_and_keeps_source_files(
    tmp_path,
):
    source = tmp_path / "source"
    _init_git_workspace(source)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=tmp_path / "codeharness",
    )
    manager.clone_url = lambda _repo: str(source)

    prepared = manager.prepare(_instance(base_commit=base_commit))
    exclude_path = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=prepared.workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exclude_path = Path(exclude_path)
    if not exclude_path.is_absolute():
        exclude_path = prepared.workspace / exclude_path

    assert ".codeharness/" in exclude_path.read_text().splitlines()

    artifact = prepared.workspace / ".codeharness/artifacts/tool-test.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("runtime artifact\n", encoding="utf-8")
    source_file = prepared.workspace / "new_source.py"
    source_file.write_text("value = 1\n", encoding="utf-8")

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=prepared.workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    patch, error = capture_patch(prepared.workspace)

    assert ".codeharness" not in status
    assert "?? new_source.py" in status
    assert error is None
    assert ".codeharness" not in patch
    assert "diff --git a/new_source.py b/new_source.py" in patch


def test_workspace_local_exclude_preserves_content_and_is_idempotent(tmp_path):
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=tmp_path / "codeharness",
    )
    exclude_path = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exclude_path = Path(exclude_path)
    if not exclude_path.is_absolute():
        exclude_path = workspace / exclude_path
    original = b"# existing local rules\n*.scratch"
    exclude_path.write_bytes(original)

    manager._configure_local_excludes(workspace)
    manager._configure_local_excludes(workspace)

    contents = exclude_path.read_bytes()
    assert contents == original + b"\n.codeharness/\n"
    assert contents.splitlines().count(b".codeharness/") == 1


def test_workspace_local_exclude_uses_git_path_and_creates_missing_file(tmp_path):
    workspace = tmp_path / "workspace"
    git_dir = tmp_path / "git-data"
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(git_dir), str(workspace)],
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=workspace, check=True
    )
    (workspace / "module.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "module.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    exclude_path = Path(
        subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if not exclude_path.is_absolute():
        exclude_path = workspace / exclude_path
    exclude_path.unlink()
    manager = SWEbenchWorkspaceManager(
        "run-1",
        work_root=tmp_path / "work",
        repository_root=tmp_path / "codeharness",
    )

    manager._configure_local_excludes(workspace)

    assert (workspace / ".git").is_file()
    assert exclude_path.read_bytes() == b".codeharness/\n"
    assert subprocess.run(
        ["git", "status", "--short"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


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


def _prepared_runtime_workspace(tmp_path):
    case_root = tmp_path / "work/run-1/owner__repo-123"
    workspace = case_root / "runtime-state"
    agent_home = case_root / "agent-home"
    workspace.mkdir(parents=True)
    agent_home.mkdir()
    return PreparedSWEbenchWorkspace(case_root, workspace, agent_home)


def _environment(secret="adapter-secret"):
    return {
        "DEEPSEEK_API_KEY": secret,
        "DEEPSEEK_BASE_URL": "https://models.example/v1",
        "DEEPSEEK_MODEL_ID": "deepseek-test",
    }


class FakeRollout:
    def __init__(self, events, *, start_error=None):
        self.events = events
        self.start_error = start_error
        self.closed = False
        self.container_id = "borrowed-container-id"
        self.info = SimpleNamespace(
            instance_id="owner__repo-123",
            image="official-image:latest",
            image_id="sha256:image",
            container_name="codeharness.test",
            container_id=self.container_id,
            initial_head="a" * 40,
            initial_status="",
            python_path="/opt/python/bin/python",
            python_version="Python 3.11.9",
        )

    def start(self):
        self.events.append("rollout.start")
        if self.start_error is not None:
            raise self.start_error
        return self.info

    def close(self):
        if not self.closed:
            self.events.append("rollout.close")
            self.closed = True


class FakeCollector:
    def __init__(
        self,
        container_id,
        events,
        *,
        patch="diff --git a/module.py b/module.py\n+container change\n",
        patch_error=None,
        snapshot_error=None,
        capture_error=None,
    ):
        self.container_id = container_id
        self.events = events
        self.patch = patch
        self.patch_error = patch_error
        self.snapshot_error = snapshot_error
        self.capture_error = capture_error
        self.closed = False

    def snapshot_baseline(self):
        self.events.append("collector.snapshot")
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return "baseline-tree"

    def capture_patch(self):
        self.events.append("collector.capture")
        if self.capture_error is not None:
            raise self.capture_error
        return self.patch, self.patch_error

    def close(self):
        if not self.closed:
            self.events.append("collector.close")
            self.closed = True


def _worker_runner(
    events,
    captured,
    *,
    process=None,
    write_result=True,
    secret=None,
    error=None,
):
    def fake_runner(command, *, workspace, environment, timeout_seconds):
        events.append("worker")
        captured.update(
            command=list(command),
            workspace=workspace,
            environment=dict(environment),
            timeout_seconds=timeout_seconds,
        )
        if error is not None:
            raise error
        if write_result:
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
        return process or WorkerProcessResult(
            0, f"stdout {secret or ''}", "", False, 0.25
        )

    return fake_runner


def _orchestration_kwargs(tmp_path, events, rollout, collector, worker):
    return {
        "instance_id": "owner__repo-123",
        "results_root": tmp_path / "results",
        "environment": _environment(),
        "run_id": "run-1",
        "dataset_loader": lambda *_args, **_kwargs: [_raw_instance()],
        "workspace_manager": StaticWorkspaceManager(
            _prepared_runtime_workspace(tmp_path)
        ),
        "rollout_container_factory": lambda _instance, _run_id: rollout,
        "patch_collector_factory": lambda container_id: collector,
        "worker_process_runner": worker,
    }


def test_single_instance_orchestrates_borrowed_container_and_writes_artifacts(
    tmp_path,
    monkeypatch,
):
    prepared = _prepared_runtime_workspace(tmp_path)
    manager = StaticWorkspaceManager(prepared)
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(rollout.container_id, events)
    captured = {}
    evaluator_called = False
    secret = "adapter-secret"

    def forbidden_host_capture(_workspace):
        raise AssertionError("host capture_patch must not be called")

    monkeypatch.setattr(
        swebench_adapter,
        "capture_patch",
        forbidden_host_capture,
        raising=False,
    )

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
        rollout_container_factory=lambda _instance, _run_id: rollout,
        patch_collector_factory=lambda container_id: collector,
        worker_process_runner=_worker_runner(
            events,
            captured,
            secret=secret,
        ),
        official_evaluator=unexpected_evaluator,
    )

    assert run_root == tmp_path / "results/swebench/run-1"
    assert events == [
        "rollout.start",
        "collector.snapshot",
        "worker",
        "collector.capture",
        "collector.close",
        "rollout.close",
    ]
    assert collector.container_id == rollout.container_id
    assert manager.instances[0].instance_id == "owner__repo-123"
    assert captured["workspace"] == prepared.workspace
    assert captured["timeout_seconds"] == 47
    command = captured["command"]
    assert command[:3] == [sys.executable, "-m", "evals.swebench.worker"]
    problem = command[command.index("--problem-statement") + 1]
    assert problem == "Fix the public behavior."
    assert "GOLD PATCH" not in problem
    assert "TEST PATCH" not in problem
    assert command[command.index("--container-id") + 1] == rollout.container_id
    assert command[command.index("--workspace") + 1] == str(prepared.workspace)
    command_text = " ".join(command)
    for forbidden in (
        "official-image:latest",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "hidden evaluator script",
        "hidden hint",
    ):
        assert forbidden not in command_text
    assert captured["environment"]["CODEHARNESS_HOME"] == str(prepared.agent_home)
    mcp_path = Path(captured["environment"]["MCP_CONFIG_PATH"])
    assert mcp_path == prepared.agent_home / "mcp/servers.json"
    assert mcp_path.read_text() == "{}\n"
    prediction = json.loads((run_root / "prediction.jsonl").read_text())
    assert prediction["instance_id"] == "owner__repo-123"
    assert prediction["model_name_or_path"] == "CodeHarness::deepseek-test"
    assert prediction["model_patch"] == collector.patch
    assert (prepared.case_root / "prediction.jsonl").read_text() == (
        run_root / "prediction.jsonl"
    ).read_text()
    assert (run_root / "patch.diff").read_text() == prediction["model_patch"]
    assert result["worker_status"] == "completed"
    assert result["empty_patch"] is False
    assert result["official_evaluation"] is None
    assert result["total_tokens"] == 5
    assert evaluator_called is False
    instance_metadata = json.loads((run_root / "instance.json").read_text())
    assert instance_metadata == {
        "instance_id": "owner__repo-123",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Fix the public behavior.",
        "image": _raw_instance()["image"],
    }
    rollout_metadata = json.loads((run_root / "rollout.json").read_text())
    assert rollout_metadata == {
        "image": "official-image:latest",
        "image_id": "sha256:image",
        "container_name": "codeharness.test",
        "container_id": "borrowed-container-id",
        "initial_head": "a" * 40,
        "initial_status": "",
        "python_path": "/opt/python/bin/python",
        "python_version": "Python 3.11.9",
    }
    for artifact in run_root.iterdir():
        if artifact.is_file():
            assert secret not in artifact.read_text(errors="replace")


def test_single_instance_default_workspace_does_not_clone_repository(
    tmp_path,
    monkeypatch,
):
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(rollout.container_id, events, patch="")
    captured = {}

    class ForbiddenCloneManager:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("host repository clone manager must not be used")

    monkeypatch.setattr(
        swebench_adapter,
        "SWEbenchWorkspaceManager",
        ForbiddenCloneManager,
        raising=False,
    )

    run_single_instance(
        instance_id="owner__repo-123",
        results_root=tmp_path / "results",
        work_root=tmp_path / "work",
        environment=_environment(),
        run_id="run-1",
        dataset_loader=lambda *_args, **_kwargs: [_raw_instance()],
        rollout_container_factory=lambda _instance, _run_id: rollout,
        patch_collector_factory=lambda container_id: collector,
        worker_process_runner=_worker_runner(events, captured),
    )

    runtime_state = (
        tmp_path / "work/run-1/owner__repo-123/runtime-state"
    ).resolve()
    assert captured["workspace"] == runtime_state
    assert runtime_state.is_dir()
    assert (runtime_state.parent / "agent-home").is_dir()
    assert not (runtime_state.parent / "workspace").exists()
    assert not (runtime_state / ".git").exists()


def test_official_evaluator_runs_after_prediction_and_rollout_cleanup(tmp_path):
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(rollout.container_id, events)
    captured = {}
    calls = []

    def fake_evaluator(**kwargs):
        events.append("evaluator")
        assert rollout.closed is True
        prediction = json.loads(kwargs["predictions_path"].read_text())
        assert prediction["model_patch"] == collector.patch
        calls.append(kwargs)
        return {
            "command": ["python", "-m", "swebench.harness.run_evaluation"],
            "return_code": 0,
            "stdout": "evaluated",
            "stderr": "",
        }

    kwargs = _orchestration_kwargs(
        tmp_path,
        events,
        rollout,
        collector,
        _worker_runner(events, captured),
    )
    run_root, result = run_single_instance(
        evaluate=True,
        official_evaluator=fake_evaluator,
        **kwargs,
    )

    assert events[-3:] == ["collector.close", "rollout.close", "evaluator"]
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


@pytest.mark.parametrize(
    ("process", "write_result", "expected_status"),
    [
        (WorkerProcessResult(-15, "partial", "timed out", True, 1.1), False, "timeout"),
        (WorkerProcessResult(2, "partial", "failed", False, 0.2), True, "crashed"),
        (WorkerProcessResult(0, "partial", "", False, 0.2), False, "crashed"),
    ],
    ids=("timeout", "nonzero", "missing-result"),
)
def test_worker_failure_results_still_capture_container_patch(
    tmp_path,
    process,
    write_result,
    expected_status,
):
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(rollout.container_id, events)
    captured = {}
    kwargs = _orchestration_kwargs(
        tmp_path,
        events,
        rollout,
        collector,
        _worker_runner(
            events,
            captured,
            process=process,
            write_result=write_result,
        ),
    )

    run_root, result = run_single_instance(
        **kwargs,
    )

    assert "collector.capture" in events
    assert events.index("collector.capture") < events.index("rollout.close")
    assert result["worker_status"] == expected_status
    assert json.loads((run_root / "prediction.jsonl").read_text())[
        "model_patch"
    ] == collector.patch


@pytest.mark.parametrize(
    ("patch_error", "expected_error"),
    [(None, None), ("container capture failed", "container capture failed")],
    ids=("empty", "capture-error"),
)
def test_empty_container_patch_remains_a_valid_prediction(
    tmp_path,
    patch_error,
    expected_error,
):
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(
        rollout.container_id,
        events,
        patch="",
        patch_error=patch_error,
    )
    kwargs = _orchestration_kwargs(
        tmp_path,
        events,
        rollout,
        collector,
        _worker_runner(events, {}),
    )

    run_root, result = run_single_instance(
        **kwargs,
    )

    prediction = json.loads((run_root / "prediction.jsonl").read_text())
    assert prediction["model_patch"] == ""
    assert result["empty_patch"] is True
    assert result["patch_error"] == expected_error


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        ("start", ["rollout.start", "rollout.close"]),
        (
            "snapshot",
            [
                "rollout.start",
                "collector.snapshot",
                "collector.close",
                "rollout.close",
            ],
        ),
        (
            "worker",
            [
                "rollout.start",
                "collector.snapshot",
                "worker",
                "collector.close",
                "rollout.close",
            ],
        ),
        (
            "capture",
            [
                "rollout.start",
                "collector.snapshot",
                "worker",
                "collector.capture",
                "collector.close",
                "rollout.close",
            ],
        ),
    ],
)
def test_orchestration_exception_cleans_up_owned_resources(
    tmp_path,
    failure_stage,
    expected_events,
):
    events = []
    error = RuntimeError(f"{failure_stage} failed")
    rollout = FakeRollout(
        events,
        start_error=error if failure_stage == "start" else None,
    )
    collector = FakeCollector(
        rollout.container_id,
        events,
        snapshot_error=error if failure_stage == "snapshot" else None,
        capture_error=error if failure_stage == "capture" else None,
    )
    worker = _worker_runner(
        events,
        {},
        error=error if failure_stage == "worker" else None,
    )
    kwargs = _orchestration_kwargs(
        tmp_path,
        events,
        rollout,
        collector,
        worker,
    )

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        run_single_instance(**kwargs)

    assert events == expected_events


def test_evaluator_failure_occurs_after_rollout_cleanup(tmp_path):
    events = []
    rollout = FakeRollout(events)
    collector = FakeCollector(rollout.container_id, events)
    kwargs = _orchestration_kwargs(
        tmp_path,
        events,
        rollout,
        collector,
        _worker_runner(events, {}),
    )

    def failing_evaluator(**_kwargs):
        events.append("evaluator")
        assert rollout.closed is True
        raise RuntimeError("evaluator failed")

    with pytest.raises(RuntimeError, match="evaluator failed"):
        run_single_instance(
            evaluate=True,
            official_evaluator=failing_evaluator,
            **kwargs,
        )

    assert events[-2:] == ["rollout.close", "evaluator"]


def test_cli_defaults_to_prediction_only_and_accepts_explicit_evaluate():
    parser = build_parser()

    default = parser.parse_args(["--instance-id", "case"])
    explicit = parser.parse_args(["--instance-id", "case", "--evaluate"])

    assert default.dataset_name == DEFAULT_DATASET_NAME
    assert default.split == DEFAULT_SPLIT
    assert default.evaluate is False
    assert explicit.evaluate is True
