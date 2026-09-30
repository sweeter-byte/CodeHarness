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
