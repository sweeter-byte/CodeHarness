import subprocess
from types import SimpleNamespace

from evals.swebench.docker_worktree import DockerWorktreeEnvironment


class RecordingRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        result = self.results.pop(0) if self.results else None
        if isinstance(result, BaseException):
            raise result
        return result or subprocess.CompletedProcess(command, 0, "", "")


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_docker_worktree_git_runs_in_requested_container_cwd():
    runner = RecordingRunner(completed(stdout="true\n"))
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )

    assert environment.git(
        ["rev-parse", "--is-inside-work-tree"],
        "/tmp/codeharness-worktrees/task-a",
    ) == (0, "true")

    command, kwargs = runner.calls[0]
    assert command[:7] == [
        "docker",
        "exec",
        "-e",
        "BASH_ENV=/root/.bashrc",
        "-w",
        "/tmp/codeharness-worktrees/task-a",
        "task-container",
    ]
    assert command[-2:] == ["rev-parse", "--is-inside-work-tree"]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": 60,
    }


def test_docker_worktree_git_accepts_add_target_under_worktree_root():
    runner = RecordingRunner()
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )

    assert environment.git(
        [
            "worktree",
            "add",
            "/tmp/codeharness-worktrees/task-a",
            "-b",
            "wt/task-a",
        ],
        "/testbed",
    ) == (0, "")

    command, _kwargs = runner.calls[0]
    assert command[command.index("-w") + 1] == "/testbed"
    assert command[-5:] == [
        "worktree",
        "add",
        "/tmp/codeharness-worktrees/task-a",
        "-b",
        "wt/task-a",
    ]


def test_docker_worktree_path_exists_checks_inside_container():
    runner = RecordingRunner(
        completed(returncode=0),
        completed(returncode=1),
    )
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )

    assert environment.path_exists("/tmp/codeharness-worktrees/task-a") is True
    assert environment.path_exists("/tmp/codeharness-worktrees/missing") is False

    first_command, _kwargs = runner.calls[0]
    assert first_command[:6] == [
        "docker",
        "exec",
        "-w",
        "/",
        "task-container",
        "python",
    ]
    assert "/tmp/codeharness-worktrees/task-a" in first_command


def test_docker_worktree_rejects_outside_paths_without_execution():
    runner = RecordingRunner()
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )

    code, output = environment.git(["status", "--porcelain"], "/etc")

    assert code == -1
    assert output.startswith("cwd escapes Docker workspace roots:")
    assert environment.path_exists("/root/secret") is False
    assert runner.calls == []


def test_docker_worktree_runner_failures_are_contained():
    runner = RecordingRunner(FileNotFoundError("docker missing"))
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )

    assert environment.git(["status"], "/testbed") == (-1, "docker missing")


def test_core_create_worktree_routes_all_checks_to_docker(monkeypatch):
    from codeharness.team import worktree

    runner = RecordingRunner(
        completed(returncode=1),
        completed(returncode=0),
    )
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )
    task = SimpleNamespace(
        id="task_1",
        status="pending",
        owner=None,
        worktree=None,
    )
    monkeypatch.setattr(worktree.TASKS, "load", lambda _task_id: task)
    monkeypatch.setattr(worktree.TASKS, "save", lambda _task: None)
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host git called")
        ),
    )
    worktree.configure_worktrees(
        "/testbed",
        "/tmp/codeharness-worktrees",
        environment=environment,
    )

    path, error = worktree.create_worktree("task-a", task.id)

    assert error is None
    assert path == "/tmp/codeharness-worktrees/task-a"
    assert task.worktree == "task-a"
    assert "/tmp/codeharness-worktrees/task-a" in runner.calls[0][0]
    assert runner.calls[1][0][-5:] == [
        "worktree",
        "add",
        "/tmp/codeharness-worktrees/task-a",
        "-b",
        "wt/task-a",
    ]


def test_core_resolve_and_remove_worktree_route_to_docker(monkeypatch):
    from codeharness.team import worktree

    runner = RecordingRunner(
        completed(returncode=0),
        completed(stdout="true\n"),
        completed(returncode=0),
        completed(stdout=""),
        completed(returncode=0),
    )
    environment = DockerWorktreeEnvironment(
        "task-container",
        command_runner=runner,
    )
    worktree.configure_worktrees(
        "/testbed",
        "/tmp/codeharness-worktrees",
        environment=environment,
    )
    task = SimpleNamespace(worktree="task-a")
    monkeypatch.setattr(worktree.TASKS, "list_all", list)
    monkeypatch.setattr(
        "codeharness.team.manager.TEAM.list_states",
        list,
    )
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host git called")
        ),
    )

    assert worktree.resolve_worktree_cwd(task) == (
        "/tmp/codeharness-worktrees/task-a",
        None,
    )
    ok, message = worktree.remove_worktree("task-a")

    assert ok is True
    assert message.startswith("Removed worktree 'task-a'")
    commands = [call[0] for call in runner.calls]
    assert commands[1][-2:] == ["rev-parse", "--is-inside-work-tree"]
    assert commands[3][-2:] == ["status", "--porcelain"]
    assert commands[4][-3:] == [
        "worktree",
        "remove",
        "/tmp/codeharness-worktrees/task-a",
    ]
