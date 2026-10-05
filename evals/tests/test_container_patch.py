import os
import subprocess
from pathlib import Path

import pytest

from evals.swebench.container_patch import (
    ContainerPatchCollector,
    ContainerPatchError,
)


def _git(repository: Path, *arguments: str, check: bool = True):
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=check,
        capture_output=True,
        text=True,
    )


def _initialize_repository(repository: Path, files: dict[str, str | bytes]):
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "CodeHarness Tests")
    for relative_path, contents in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")


class LocalDockerRunner:
    """Execute docker-exec payloads against a local temporary repository."""

    container = "container-id"

    def __init__(self, repository: Path, temp_root: Path):
        self.repository = repository
        self.temp_root = temp_root
        self.calls: list[tuple[list[str], dict]] = []

    def _map_path(self, value: str) -> str:
        prefix = "/tmp/codeharness-patch/"
        if value.startswith(prefix):
            return str(self.temp_root / value.removeprefix(prefix))
        return value

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        assert command[:4] == [
            "/usr/bin/docker",
            "exec",
            "-w",
            "/testbed",
        ]

        index = 4
        environment = os.environ.copy()
        while command[index] == "-e":
            name, value = command[index + 1].split("=", 1)
            environment[name] = self._map_path(value)
            index += 2
        assert command[index] == self.container
        inner = [self._map_path(value) for value in command[index + 1 :]]
        return subprocess.run(
            inner,
            cwd=self.repository,
            env=environment,
            capture_output=kwargs.get("capture_output", False),
            text=kwargs.get("text", False),
            errors=kwargs.get("errors"),
            timeout=kwargs.get("timeout"),
            check=False,
        )


class FailingDockerRunner(LocalDockerRunner):
    def __init__(
        self,
        repository: Path,
        temp_root: Path,
        *,
        matching: tuple[str, ...],
        occurrence: int = 1,
        timeout: bool = False,
    ):
        super().__init__(repository, temp_root)
        self.matching = matching
        self.occurrence = occurrence
        self.timeout = timeout
        self._matches = 0

    def __call__(self, command, **kwargs):
        command = list(command)
        container_index = command.index(self.container)
        inner = tuple(command[container_index + 1 :])
        if inner[: len(self.matching)] == self.matching:
            self._matches += 1
            if self._matches == self.occurrence:
                self.calls.append((command, kwargs))
                if self.timeout:
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                return subprocess.CompletedProcess(
                    command,
                    23,
                    stdout="",
                    stderr="simulated git failure",
                )
        return super().__call__(command, **kwargs)


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "repository"
    path.mkdir()
    _initialize_repository(path, {"tracked.txt": "baseline\n"})
    return path


@pytest.fixture
def collector(repository, tmp_path):
    runner = LocalDockerRunner(repository, tmp_path / "container-tmp")
    instance = ContainerPatchCollector(
        runner.container,
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
        id_factory=lambda: "collector-id",
    )
    return instance, runner


def _repository_state(repository: Path):
    return {
        "head": _git(repository, "rev-parse", "HEAD").stdout,
        "status": _git(repository, "status", "--porcelain").stdout,
        "index": (repository / ".git" / "index").read_bytes(),
        "stages": _git(repository, "ls-files", "--stage").stdout,
    }


def test_snapshot_anchors_parentless_baseline_without_mutating_repository(
    repository,
    collector,
):
    instance, runner = collector
    (repository / "tracked.txt").write_text("dirty baseline\n")
    (repository / "startup.txt").write_text("startup untracked\n")
    before = _repository_state(repository)

    baseline_tree = instance.snapshot_baseline()

    assert baseline_tree == instance.baseline_tree
    assert instance.baseline_commit is not None
    assert instance.baseline_ref == (
        "refs/codeharness-eval/baseline/collector-id"
    )
    assert _repository_state(repository) == before
    ref_target = _git(
        repository,
        "rev-parse",
        "--verify",
        instance.baseline_ref,
    ).stdout.strip()
    assert ref_target == instance.baseline_commit
    commit = _git(
        repository,
        "cat-file",
        "-p",
        instance.baseline_commit,
    ).stdout
    assert commit.startswith(f"tree {baseline_tree}\n")
    assert not any(line.startswith("parent ") for line in commit.splitlines())

    flattened = [argument for call, _kwargs in runner.calls for argument in call]
    assert "checkout" not in flattened
    assert "gc" not in flattened


def test_no_change_capture_is_empty_and_preserves_head_status_and_real_index(
    repository,
    collector,
):
    instance, _runner = collector
    before = _repository_state(repository)
    instance.snapshot_baseline()

    patch, error = instance.capture_patch()

    assert error is None
    assert patch == ""
    assert instance.final_tree == instance.baseline_tree
    assert _repository_state(repository) == before


def test_close_deletes_ref_before_temp_dir_and_is_idempotent(
    repository,
    collector,
):
    instance, runner = collector
    instance.snapshot_baseline()
    ref = instance.baseline_ref
    temp_dir = runner.temp_root / "collector-id"
    assert temp_dir.is_dir()

    instance.close()
    instance.close()

    assert _git(
        repository,
        "show-ref",
        "--verify",
        ref,
        check=False,
    ).returncode != 0
    assert not temp_dir.exists()
    commands = [call for call, _kwargs in runner.calls]
    delete_index = next(
        index
        for index, command in enumerate(commands)
        if command[-4:-1] == ["git", "update-ref", "-d"]
    )
    cleanup_index = next(
        index
        for index, command in enumerate(commands)
        if command[-4:-1] == ["rm", "-rf", "--"]
    )
    assert delete_index < cleanup_index


def test_snapshot_twice_fails_clearly(collector):
    instance, _runner = collector
    instance.snapshot_baseline()

    with pytest.raises(ContainerPatchError, match="already captured"):
        instance.snapshot_baseline()


def test_capture_before_snapshot_returns_clear_error(collector):
    instance, _runner = collector

    patch, error = instance.capture_patch()

    assert patch == ""
    assert error is not None
    assert "baseline" in error.lower()


def test_tracked_modification_is_captured_from_baseline(repository, collector):
    instance, _runner = collector
    instance.snapshot_baseline()
    (repository / "tracked.txt").write_text("after\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert "diff --git a/tracked.txt b/tracked.txt" in patch
    assert "-baseline" in patch
    assert "+after" in patch


def test_new_non_ignored_untracked_file_is_captured(repository, collector):
    instance, _runner = collector
    instance.snapshot_baseline()
    (repository / "created.txt").write_text("new content\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert "diff --git a/created.txt b/created.txt" in patch
    assert "new file mode" in patch
    assert "+new content" in patch


def test_binary_modification_uses_binary_patch(repository, collector):
    instance, _runner = collector
    binary = repository / "payload.bin"
    binary.write_bytes(bytes(range(256)))
    _git(repository, "add", "payload.bin")
    _git(repository, "commit", "-m", "add binary")
    instance.snapshot_baseline()
    binary.write_bytes(bytes(reversed(range(256))))

    patch, error = instance.capture_patch()

    assert error is None
    assert "diff --git a/payload.bin b/payload.bin" in patch
    assert "GIT binary patch" in patch


def test_tracked_deletion_is_captured(repository, collector):
    instance, _runner = collector
    instance.snapshot_baseline()
    (repository / "tracked.txt").unlink()

    patch, error = instance.capture_patch()

    assert error is None
    assert "diff --git a/tracked.txt b/tracked.txt" in patch
    assert "deleted file mode" in patch
    assert "-baseline" in patch


def test_final_ignored_file_is_excluded(repository, collector):
    instance, _runner = collector
    (repository / ".gitignore").write_text("ignored.txt\n")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", "add ignore rule")
    instance.snapshot_baseline()
    (repository / "ignored.txt").write_text("build artifact\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert patch == ""


def test_committed_agent_change_is_captured_when_worktree_is_clean(
    repository,
    collector,
):
    instance, _runner = collector
    instance.snapshot_baseline()
    (repository / "tracked.txt").write_text("committed after baseline\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "agent commit")
    assert _git(repository, "status", "--porcelain").stdout == ""

    patch, error = instance.capture_patch()

    assert error is None
    assert "-baseline" in patch
    assert "+committed after baseline" in patch


def test_checkout_to_different_head_is_compared_with_startup_filesystem(
    repository,
    collector,
):
    instance, _runner = collector
    startup_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "tracked.txt").write_text("alternate commit\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "alternate")
    alternate_head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "checkout", "--detach", startup_head)
    instance.snapshot_baseline()

    _git(repository, "checkout", "--detach", alternate_head)
    patch, error = instance.capture_patch()

    assert error is None
    assert "-baseline" in patch
    assert "+alternate commit" in patch


def test_hard_reset_is_compared_with_startup_filesystem(repository, collector):
    instance, _runner = collector
    (repository / "tracked.txt").write_text("startup commit\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "startup state")
    instance.snapshot_baseline()

    _git(repository, "reset", "--hard", "HEAD^")
    patch, error = instance.capture_patch()

    assert error is None
    assert "-startup commit" in patch
    assert "+baseline" in patch


def test_unchanged_dirty_tracked_baseline_produces_empty_patch(
    repository,
    collector,
):
    instance, _runner = collector
    (repository / "tracked.txt").write_text("startup dirty\n")
    instance.snapshot_baseline()

    patch, error = instance.capture_patch()

    assert error is None
    assert patch == ""


def test_dirty_tracked_baseline_only_reports_incremental_change(
    repository,
    collector,
):
    instance, _runner = collector
    (repository / "tracked.txt").write_text("startup dirty\n")
    instance.snapshot_baseline()
    (repository / "tracked.txt").write_text("final content\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert "-startup dirty" in patch
    assert "+final content" in patch
    assert "-baseline" not in patch


def test_unchanged_startup_untracked_file_produces_empty_patch(
    repository,
    collector,
):
    instance, _runner = collector
    (repository / "startup.txt").write_text("startup content\n")
    instance.snapshot_baseline()

    patch, error = instance.capture_patch()

    assert error is None
    assert patch == ""


def test_modified_startup_untracked_file_is_compared_to_startup_content(
    repository,
    collector,
):
    instance, _runner = collector
    (repository / "startup.txt").write_text("startup content\n")
    instance.snapshot_baseline()
    (repository / "startup.txt").write_text("final content\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert "diff --git a/startup.txt b/startup.txt" in patch
    assert "-startup content" in patch
    assert "+final content" in patch
    assert "new file mode" not in patch


def test_snapshot_fails_clearly_when_docker_is_unavailable(repository):
    instance = ContainerPatchCollector(
        "container-id",
        executable_finder=lambda _name: None,
        command_runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
        id_factory=lambda: "collector-id",
    )

    with pytest.raises(ContainerPatchError, match="Docker executable"):
        instance.snapshot_baseline()


def test_snapshot_git_failure_is_clear_and_temp_directory_is_cleaned(
    repository,
    tmp_path,
):
    runner = FailingDockerRunner(
        repository,
        tmp_path / "container-tmp",
        matching=("git", "read-tree"),
    )
    instance = ContainerPatchCollector(
        runner.container,
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
        id_factory=lambda: "collector-id",
    )

    with pytest.raises(
        ContainerPatchError,
        match="baseline index initialization.*simulated git failure",
    ):
        instance.snapshot_baseline()

    assert not (runner.temp_root / "collector-id").exists()


def test_snapshot_timeout_is_reported_clearly(repository, tmp_path):
    runner = FailingDockerRunner(
        repository,
        tmp_path / "container-tmp",
        matching=("git", "read-tree"),
        timeout=True,
    )
    instance = ContainerPatchCollector(
        runner.container,
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
        id_factory=lambda: "collector-id",
        timeout=7,
    )

    with pytest.raises(
        ContainerPatchError,
        match="baseline index initialization timed out after 7s",
    ):
        instance.snapshot_baseline()


def test_capture_failure_returns_error_and_cleans_ref_and_temp_dir(
    repository,
    tmp_path,
):
    runner = FailingDockerRunner(
        repository,
        tmp_path / "container-tmp",
        matching=("git", "add", "-A", "--", "."),
        occurrence=2,
    )
    instance = ContainerPatchCollector(
        runner.container,
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
        id_factory=lambda: "collector-id",
    )
    instance.snapshot_baseline()
    ref = instance.baseline_ref

    patch, error = instance.capture_patch()

    assert patch == ""
    assert error is not None
    assert "final filesystem staging" in error
    assert "simulated git failure" in error
    assert _git(
        repository,
        "show-ref",
        "--verify",
        ref,
        check=False,
    ).returncode != 0
    assert not (runner.temp_root / "collector-id").exists()


def test_close_attempts_temp_cleanup_when_ref_deletion_fails_then_can_retry(
    repository,
    tmp_path,
):
    runner = FailingDockerRunner(
        repository,
        tmp_path / "container-tmp",
        matching=("git", "update-ref", "-d"),
    )
    instance = ContainerPatchCollector(
        runner.container,
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
        id_factory=lambda: "collector-id",
    )
    instance.snapshot_baseline()

    with pytest.raises(ContainerPatchError, match="ref deletion"):
        instance.close()

    assert not (runner.temp_root / "collector-id").exists()
    assert _git(
        repository,
        "show-ref",
        "--verify",
        instance.baseline_ref,
        check=False,
    ).returncode == 0
    instance.close()
    assert _git(
        repository,
        "show-ref",
        "--verify",
        instance.baseline_ref,
        check=False,
    ).returncode != 0


def test_tree_commands_use_private_index_and_only_testbed(collector):
    instance, runner = collector
    instance.snapshot_baseline()
    instance.capture_patch()

    expected_environment = f"GIT_INDEX_FILE={instance.index_file}"
    tree_commands = 0
    flattened: list[str] = []
    for command, _kwargs in runner.calls:
        flattened.extend(command)
        assert command[:4] == [
            "/usr/bin/docker",
            "exec",
            "-w",
            "/testbed",
        ]
        if any(
            operation in command
            for operation in ("read-tree", "add", "write-tree")
        ):
            tree_commands += 1
            assert expected_environment in command

    assert tree_commands == 6
    assert "refs/codeharness-eval/baseline/collector-id" in flattened
    assert "gc" not in flattened
    assert not any("codeharness-worktrees" in value for value in flattened)


def test_container_capture_never_calls_host_capture_patch(
    repository,
    collector,
    monkeypatch,
):
    from evals.runner import verifier

    monkeypatch.setattr(
        verifier,
        "capture_patch",
        lambda *_args, **_kwargs: pytest.fail("host capture called"),
    )
    instance, _runner = collector
    instance.snapshot_baseline()
    (repository / "tracked.txt").write_text("container final\n")

    patch, error = instance.capture_patch()

    assert error is None
    assert "+container final" in patch
