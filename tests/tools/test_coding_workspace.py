import time


class RecordingBackgroundManager:
    def __init__(self):
        self.calls = []

    def start(self, command, cwd=None):
        self.calls.append((command, cwd))
        return "bg_001", None


def test_coding_handlers_use_default_workspace_for_sync_tools(tmp_path):
    from codeharness.tools.coding import make_coding_handlers

    handlers = make_coding_handlers(
        RecordingBackgroundManager(), default_cwd=str(tmp_path)
    )

    assert handlers["write_file"](path="note.txt", content="workspace data") == (
        "Written 14 chars to note.txt"
    )
    assert "workspace data" in handlers["read_file"](path="note.txt")
    assert handlers["bash"](command="pwd") == str(tmp_path)


def test_bash_false_is_sync_and_does_not_leak_flag_to_run_bash(tmp_path):
    from codeharness.tools.coding import make_coding_handlers

    background = RecordingBackgroundManager()
    handlers = make_coding_handlers(background, default_cwd=str(tmp_path))

    assert handlers["bash"](command="printf sync", run_in_background=False) == "sync"
    assert background.calls == []


def test_background_bash_uses_effective_cwd(tmp_path):
    from codeharness.tools.coding import make_coding_handlers

    background = RecordingBackgroundManager()
    handlers = make_coding_handlers(background, default_cwd="/default")

    result = handlers["bash"](
        command="pwd", run_in_background=True, cwd=str(tmp_path)
    )

    assert result == "[Background task bg_001 started: pwd]"
    assert background.calls == [("pwd", str(tmp_path))]


def test_background_manager_runs_command_in_explicit_cwd(tmp_path):
    from codeharness.background import BackgroundManager

    manager = BackgroundManager()
    bg_id, error = manager.start("pwd", cwd=str(tmp_path))

    assert error is None
    manager.tasks[bg_id]["thread"].join(timeout=5)
    deadline = time.monotonic() + 1
    notifications = []
    while not notifications and time.monotonic() < deadline:
        notifications = manager.collect()
        if not notifications:
            time.sleep(0.01)

    assert len(notifications) == 1
    assert str(tmp_path) in notifications[0]


def test_local_workspace_backend_preserves_existing_tool_behavior(tmp_path):
    from codeharness.tools.workspace import LocalWorkspaceBackend

    background = RecordingBackgroundManager()
    backend = LocalWorkspaceBackend(
        background_manager=background,
        default_cwd=str(tmp_path),
    )

    assert backend.write_file("note.txt", "workspace data") == (
        "Written 14 chars to note.txt"
    )
    assert backend.read_file("note.txt") == "    1\tworkspace data"
    assert backend.bash("pwd") == str(tmp_path)
    assert backend.bash("pwd", run_in_background=True) == (
        "[Background task bg_001 started: pwd]"
    )
    assert background.calls == [("pwd", str(tmp_path))]


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


def test_make_coding_handlers_routes_calls_to_injected_backend():
    from codeharness.tools.coding import make_coding_handlers

    backend = RecordingWorkspaceBackend()
    handlers = make_coding_handlers(backend=backend)

    assert handlers["read_file"](
        path="src/app.py", start_line=2, end_line=4, cwd="logical"
    ) == "backend read"
    assert backend.calls == [
        ("read_file", "src/app.py", 2, 4, "logical"),
    ]


def test_local_backend_keeps_a_falsy_injected_background_manager():
    from codeharness.tools.workspace import LocalWorkspaceBackend

    class FalsyBackgroundManager(RecordingBackgroundManager):
        def __bool__(self):
            return False

    background = FalsyBackgroundManager()

    backend = LocalWorkspaceBackend(background_manager=background)

    assert backend.background_manager is background


def test_make_coding_handlers_never_replaces_a_falsy_injected_backend():
    from codeharness.tools.coding import make_coding_handlers

    class FalsyWorkspaceBackend(RecordingWorkspaceBackend):
        def __bool__(self):
            return False

    backend = FalsyWorkspaceBackend()
    handlers = make_coding_handlers(backend=backend)

    assert handlers["read_file"](path="only-in-container.txt") == "backend read"
    assert backend.calls == [
        ("read_file", "only-in-container.txt", None, None, None),
    ]
