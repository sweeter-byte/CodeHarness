import subprocess
from types import SimpleNamespace


class RecordingWorktreeEnvironment:
    def __init__(self, *, existing=(), git_results=()):
        self.existing = {str(path) for path in existing}
        self.git_results = list(git_results)
        self.calls = []

    def git(self, args, cwd):
        self.calls.append(("git", list(args), str(cwd)))
        if self.git_results:
            return self.git_results.pop(0)
        return 0, ""

    def path_exists(self, path):
        self.calls.append(("path_exists", str(path)))
        return str(path) in self.existing


def _task(**overrides):
    values = {
        "id": "task_1",
        "subject": "subject",
        "description": "",
        "status": "pending",
        "owner": None,
        "worktree": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_worktree_uses_injected_environment(monkeypatch):
    from codeharness.team import worktree

    environment = RecordingWorktreeEnvironment()
    task = _task()
    saved = []
    monkeypatch.setattr(worktree.TASKS, "load", lambda _task_id: task)
    monkeypatch.setattr(worktree.TASKS, "save", saved.append)
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
    assert saved == [task]
    assert environment.calls == [
        ("path_exists", "/tmp/codeharness-worktrees/task-a"),
        (
            "git",
            [
                "worktree",
                "add",
                "/tmp/codeharness-worktrees/task-a",
                "-b",
                "wt/task-a",
            ],
            "/testbed",
        ),
    ]


def test_resolve_worktree_cwd_uses_environment_for_bound_task():
    from codeharness.team import worktree

    path = "/tmp/codeharness-worktrees/task-a"
    environment = RecordingWorktreeEnvironment(
        existing=[path],
        git_results=[(0, "true")],
    )
    worktree.configure_worktrees(
        "/testbed",
        "/tmp/codeharness-worktrees",
        environment=environment,
    )

    assert worktree.resolve_worktree_cwd(_task(worktree="task-a")) == (
        path,
        None,
    )
    assert environment.calls == [
        ("path_exists", path),
        ("git", ["rev-parse", "--is-inside-work-tree"], path),
    ]


def test_resolve_unbound_task_uses_logical_repository_without_host_checks():
    from codeharness.team import worktree

    environment = RecordingWorktreeEnvironment()
    worktree.configure_worktrees(
        "/testbed",
        "/tmp/codeharness-worktrees",
        environment=environment,
    )

    assert worktree.resolve_worktree_cwd(_task()) == ("/testbed", None)
    assert environment.calls == []


def test_remove_worktree_uses_injected_environment(monkeypatch):
    from codeharness.team import worktree

    path = "/tmp/codeharness-worktrees/task-a"
    environment = RecordingWorktreeEnvironment(
        existing=[path],
        git_results=[(0, ""), (0, "")],
    )
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
    worktree.configure_worktrees(
        "/testbed",
        "/tmp/codeharness-worktrees",
        environment=environment,
    )

    ok, message = worktree.remove_worktree("task-a")

    assert ok is True
    assert message.startswith("Removed worktree 'task-a'")
    assert environment.calls == [
        ("path_exists", path),
        ("git", ["status", "--porcelain"], path),
        (
            "git",
            ["worktree", "remove", path],
            "/testbed",
        ),
    ]


def test_local_worktree_create_resolve_and_remove_regression(
    monkeypatch, tmp_path
):
    from codeharness.team import worktree

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=repository,
        check=True,
    )

    task = _task()
    monkeypatch.setattr(worktree.TASKS, "load", lambda _task_id: task)
    monkeypatch.setattr(worktree.TASKS, "save", lambda _task: None)
    monkeypatch.setattr(worktree.TASKS, "list_all", lambda: [task])
    monkeypatch.setattr(
        "codeharness.team.manager.TEAM.list_states",
        list,
    )
    worktrees_dir = repository / ".codeharness/worktrees"
    worktree.configure_worktrees(repository, worktrees_dir)

    path, error = worktree.create_worktree("task-a", task.id)

    assert error is None
    assert path == str(worktrees_dir / "task-a")
    assert (worktrees_dir / "task-a/tracked.txt").read_text() == "base\n"
    assert worktree.resolve_worktree_cwd(task) == (path, None)

    task.status = "completed"
    ok, message = worktree.remove_worktree("task-a")

    assert ok is True
    assert message.startswith("Removed worktree 'task-a'")
    assert not (worktrees_dir / "task-a").exists()
