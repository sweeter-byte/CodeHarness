from types import SimpleNamespace

import pytest


class RecordingWorkspaceBackend:
    def __init__(self):
        self.calls = []

    def bash(self, command, run_in_background=False, cwd=None):
        self.calls.append(("bash", command, run_in_background, cwd))
        return "backend bash"

    def read_file(self, path, start_line=None, end_line=None, cwd=None):
        self.calls.append(("read_file", path, start_line, end_line, cwd))
        return "backend read"

    def write_file(self, path, content, cwd=None):
        self.calls.append(("write_file", path, content, cwd))
        return "backend write"

    def edit_file(self, path, old_text, new_text, cwd=None):
        self.calls.append(("edit_file", path, old_text, new_text, cwd))
        return "backend edit"

    def glob(self, pattern, cwd=None):
        self.calls.append(("glob", pattern, cwd))
        return "backend glob"

    def grep(self, pattern, path=".", file_pattern=None, cwd=None):
        self.calls.append(("grep", pattern, path, file_pattern, cwd))
        return "backend grep"


class FakeThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


def test_team_manager_propagates_backend_and_assignment_cwd(
    monkeypatch, tmp_path,
):
    from codeharness.skills import SkillRegistry
    from codeharness.team import manager as manager_module
    from codeharness.tools import coding

    def host_fallback(*_args, **_kwargs):
        pytest.fail("host coding implementation called")

    for name in (
        "run_bash",
        "run_read",
        "run_write",
        "run_edit",
        "run_glob",
        "run_grep",
    ):
        monkeypatch.setattr(coding, name, host_fallback)

    backend = RecordingWorkspaceBackend()
    created = []
    task = SimpleNamespace(
        id="task_backend",
        subject="subject",
        description="desc",
    )
    manager = manager_module.TeamManager()
    manager.set_agent_factory(
        lambda **kwargs: created.append(kwargs) or SimpleNamespace(),
        workspace_backend=backend,
    )
    skills = SkillRegistry(
        workspace=tmp_path,
        agent_home=tmp_path / "agent-home",
    )
    monkeypatch.setattr(
        manager_module.TASKS,
        "claim",
        lambda *args, **kwargs: (
            task,
            "/tmp/codeharness-worktrees/task-a",
            None,
        ),
    )
    monkeypatch.setattr(manager_module.threading, "Thread", FakeThread)

    result = manager.spawn(task.id, name="Alice", skill_registry=skills)

    assert result.startswith("Spawned teammate Alice")
    assert created[0]["skill_registry"] is skills
    handlers = created[0]["handlers"]
    assert handlers["read_file"](path="x.py") == "backend read"
    assert handlers["bash"](command="pytest -q") == "backend bash"
    assert handlers["write_file"](path="x.py", content="x") == "backend write"
    assert handlers["edit_file"](
        path="x.py", old_text="x", new_text="y"
    ) == "backend edit"
    assert handlers["glob"](pattern="*.py") == "backend glob"
    assert handlers["grep"](pattern="needle") == "backend grep"
    cwd = "/tmp/codeharness-worktrees/task-a"
    assert backend.calls == [
        ("read_file", "x.py", None, None, cwd),
        ("bash", "pytest -q", False, cwd),
        ("write_file", "x.py", "x", cwd),
        ("edit_file", "x.py", "x", "y", cwd),
        ("glob", "*.py", cwd),
        ("grep", "needle", ".", None, cwd),
    ]
