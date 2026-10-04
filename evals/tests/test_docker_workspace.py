import subprocess
import sys

import pytest

from evals.swebench.docker_workspace import (
    _EDIT_SCRIPT,
    _READ_SCRIPT,
    _WRITE_SCRIPT,
    DockerWorkspaceBackend,
)


class RecordingRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        result = self.results.pop(0) if self.results else None
        if isinstance(result, BaseException):
            raise result
        return result or subprocess.CompletedProcess(command, 0, "", "")


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_docker_bash_executes_in_testbed_with_image_environment():
    runner = RecordingRunner(completed(stdout="Python 3.9.20\n"))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)

    assert backend.bash("python -V") == "Python 3.9.20"
    command, kwargs = runner.calls[0]
    assert command == [
        "docker", "exec", "-e", "BASH_ENV=/root/.bashrc", "-w", "/testbed",
        "task-container", "bash", "-c", "python -V",
    ]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": 120,
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("requests/utils.py", "/testbed/requests/utils.py"),
        ("/testbed/requests/utils.py", "/testbed/requests/utils.py"),
    ],
)
def test_docker_read_maps_allowed_paths_into_testbed(path, expected):
    runner = RecordingRunner(completed(stdout="    1\tcontents"))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.read_file(path) == "    1\tcontents"
    assert expected in runner.calls[0][0]


@pytest.mark.parametrize(
    "path", ["../outside", "/testbed/../etc/passwd", "/etc/passwd"]
)
def test_docker_read_rejects_paths_outside_testbed_without_execution(path):
    runner = RecordingRunner()
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    result = backend.read_file(path)
    assert result.startswith("Error: Path escapes Docker workspace /testbed:")
    assert runner.calls == []


def test_docker_write_sends_content_on_stdin_and_maps_relative_path():
    runner = RecordingRunner(completed(stdout="Written 7 chars to notes.txt\n"))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.write_file("notes.txt", "content") == "Written 7 chars to notes.txt"
    command, kwargs = runner.calls[0]
    assert command[:3] == ["docker", "exec", "-i"]
    assert "/testbed/notes.txt" in command
    assert kwargs["input"] == "content"


def test_docker_edit_maps_absolute_testbed_path():
    runner = RecordingRunner(completed(stdout="Edited /testbed/requests/utils.py\n"))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.edit_file(
        "/testbed/requests/utils.py", "old", "new"
    ) == "Edited /testbed/requests/utils.py"
    command = runner.calls[0][0]
    assert "/testbed/requests/utils.py" in command
    assert "old" in command
    assert "new" in command


@pytest.mark.parametrize(
    ("pattern", "mapped"),
    [
        ("requests/**/*.py", "/testbed/requests/**/*.py"),
        ("/testbed/requests/*.py", "/testbed/requests/*.py"),
    ],
)
def test_docker_glob_maps_patterns_inside_testbed(pattern, mapped):
    runner = RecordingRunner(completed(stdout="requests/utils.py\n"))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.glob(pattern) == "requests/utils.py"
    assert mapped in runner.calls[0][0]


def test_docker_grep_maps_search_root_and_passes_patterns_as_arguments():
    runner = RecordingRunner(
        completed(stdout="requests/utils.py:10: def default_headers():\n")
    )
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.grep(
        "default_headers", path="requests", file_pattern="*.py"
    ) == "requests/utils.py:10: def default_headers():"
    command = runner.calls[0][0]
    assert "/testbed/requests" in command
    assert "default_headers" in command
    assert "*.py" in command


def test_docker_grep_rejects_traversal_in_file_pattern_without_execution():
    runner = RecordingRunner()
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    result = backend.grep("secret", path=".", file_pattern="../*")
    assert result == "Error: file_pattern must stay within Docker workspace: ../*"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("write_file", ("../notes.txt", "content")),
        ("edit_file", ("/etc/passwd", "old", "new")),
        ("glob", ("../*.py",)),
        ("grep", ("secret", "/root")),
    ],
)
def test_docker_file_tools_reject_workspace_escape_without_execution(method, args):
    runner = RecordingRunner()
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    result = getattr(backend, method)(*args)
    assert result.startswith("Error:")
    assert runner.calls == []


def test_docker_file_tools_never_call_local_coding_implementations(monkeypatch):
    from codeharness.tools import coding

    def host_fallback(*_args, **_kwargs):
        raise AssertionError("host coding implementation was called")

    for name in ("run_read", "run_write", "run_edit", "run_glob", "run_grep"):
        monkeypatch.setattr(coding, name, host_fallback)
    runner = RecordingRunner(*[completed(stdout="container result\n")] * 5)
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    assert backend.read_file("a.py") == "container result"
    assert backend.write_file("a.py", "x") == "container result"
    assert backend.edit_file("a.py", "x", "y") == "container result"
    assert backend.glob("*.py") == "container result"
    assert backend.grep("x") == "container result"
    assert len(runner.calls) == 5


def test_docker_command_failure_reports_container_operation_and_exit_code():
    runner = RecordingRunner(completed(stderr="container is not running\n", returncode=125))
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    result = backend.bash("pytest -q")
    assert result.startswith("Error: Docker bash command failed")
    assert "task-container" in result
    assert "exit code 125" in result
    assert "container is not running" in result


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (FileNotFoundError("docker executable missing"), "docker executable missing"),
        (subprocess.TimeoutExpired("docker", 120), "timed out after 120s"),
    ],
)
def test_docker_runner_failures_return_explicit_errors(failure, detail):
    runner = RecordingRunner(failure)
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    result = backend.read_file("requests/utils.py")
    assert result.startswith("Error: Docker read_file")
    assert "task-container" in result
    assert detail in result


def test_docker_background_is_rejected_without_docker_or_host_shell():
    from codeharness.tools.coding import make_coding_handlers

    class HostBackgroundManager:
        def __init__(self):
            self.calls = []

        def start(self, command, cwd=None):
            self.calls.append((command, cwd))
            return "bg_001", None

    runner = RecordingRunner()
    host_background = HostBackgroundManager()
    backend = DockerWorkspaceBackend("task-container", command_runner=runner)
    handlers = make_coding_handlers(background_manager=host_background, backend=backend)
    assert handlers["bash"](
        command="pytest -q", run_in_background=True
    ) == "Error: Docker background execution is not supported"
    assert runner.calls == []
    assert host_background.calls == []


@pytest.mark.parametrize(
    ("script", "extra_args", "input_text"),
    [
        (_READ_SCRIPT, ("", ""), None),
        (_WRITE_SCRIPT, ("link.txt",), "replacement"),
        (_EDIT_SCRIPT, ("link.txt", "outside", "replacement"), None),
    ],
    ids=["read", "write", "edit"],
)
def test_container_file_helpers_reject_symlink_escape(
    tmp_path, script, extra_args, input_text
):
    workspace = tmp_path / "testbed"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(outside)
    completed_process = subprocess.run(
        [sys.executable, "-c", script, str(workspace), str(link), *extra_args],
        input=input_text,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert completed_process.returncode == 0
    assert completed_process.stdout.startswith(
        f"Error: Path escapes Docker workspace {workspace}:"
    )
    assert outside.read_text(encoding="utf-8") == "outside"
