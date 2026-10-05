import re
import subprocess

import pytest

from evals.swebench.dataset import SWEbenchInstance
from evals.swebench.docker_workspace import DockerWorkspaceBackend
from evals.swebench.docker_worktree import DockerWorktreeEnvironment
from evals.swebench.rollout_container import (
    RolloutContainerError,
    SWEbenchRolloutContainer,
    resolve_official_image,
)


def _instance(**overrides):
    raw = {
        "instance_id": "owner__repo-123",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Fix the public behavior.",
        "image": "registry.example/official/task:latest",
        "patch": "GOLD PATCH MUST STAY PRIVATE",
        "test_patch": "TEST PATCH MUST STAY PRIVATE",
        "FAIL_TO_PASS": '["private::failing"]',
        "PASS_TO_PASS": '["private::passing"]',
        "eval_script": "PRIVATE EVALUATOR SCRIPT",
    }
    raw.update(overrides)
    return SWEbenchInstance.from_raw(raw)


def test_resolve_official_image_uses_exact_non_empty_raw_image():
    instance = _instance(image=" registry.example/official/task:tag ")

    assert resolve_official_image(instance) == "registry.example/official/task:tag"


@pytest.mark.parametrize("image", [None, "", "   ", 42])
def test_resolve_official_image_fails_without_non_empty_raw_image(image):
    instance = _instance(image=image)

    with pytest.raises(
        RolloutContainerError,
        match=r"official image.*instance.raw\['image'\]",
    ):
        resolve_official_image(instance)


def test_container_name_is_deterministic_safe_and_run_specific():
    first = SWEbenchRolloutContainer(
        _instance(instance_id="Owner/Repo; $(unsafe)-123"),
        run_id="Run ID/../../one; echo nope",
    )
    same = SWEbenchRolloutContainer(
        _instance(instance_id="Owner/Repo; $(unsafe)-123"),
        run_id="Run ID/../../one; echo nope",
    )
    other_run = SWEbenchRolloutContainer(
        _instance(instance_id="Owner/Repo; $(unsafe)-123"),
        run_id="run-two",
    )

    assert first.container_name == same.container_name
    assert first.container_name != other_run.container_name
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,254}", first.container_name)
    assert "owner-repo-unsafe-123" in first.container_name
    assert "run-id-one-echo-nope" in first.container_name
    assert all(
        token not in first.container_name
        for token in ("/", ";", "$", "(", ")", " ")
    )


class FakeDockerRunner:
    def __init__(
        self,
        *,
        daemon_ok=True,
        image_local=True,
        pull_ok=True,
        existing_container=False,
        create_ok=True,
        start_ok=True,
        testbed_exists=True,
        git_repo=True,
        head="b" * 40,
        status=" M requests/sessions.py\n?? prepared.txt\n",
        status_ok=True,
        python_path="/opt/pyvenv/bin/python",
        python_version="Python 3.9.20",
        remove_ok=True,
    ):
        self.daemon_ok = daemon_ok
        self.image_local = image_local
        self.pull_ok = pull_ok
        self.existing_container = existing_container
        self.create_ok = create_ok
        self.start_ok = start_ok
        self.testbed_exists = testbed_exists
        self.git_repo = git_repo
        self.head = head
        self.status = status
        self.status_ok = status_ok
        self.python_path = python_path
        self.python_version = python_version
        self.remove_ok = remove_ok
        self.image_id = "sha256:" + "1" * 64
        self.container_id = "2" * 64
        self.calls = []

    @staticmethod
    def _completed(command, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))

        if command == ["/usr/bin/docker", "info"]:
            if self.daemon_ok:
                return self._completed(command)
            return self._completed(command, 1, stderr="daemon unavailable")

        if command[:4] == [
            "/usr/bin/docker", "image", "inspect", "--format",
        ]:
            if self.image_local:
                return self._completed(command, stdout=f"{self.image_id}\n")
            return self._completed(command, 1, stderr="No such image")

        if command[:2] == ["/usr/bin/docker", "pull"]:
            if not self.pull_ok:
                return self._completed(command, 1, stderr="pull denied")
            self.image_local = True
            return self._completed(command, stdout="pulled\n")

        if command[:3] == ["/usr/bin/docker", "container", "ls"]:
            output = "existing-id\n" if self.existing_container else ""
            return self._completed(command, stdout=output)

        if command[:2] == ["/usr/bin/docker", "create"]:
            if not self.create_ok:
                return self._completed(command, 1, stderr="create denied")
            return self._completed(command, stdout=f"{self.container_id}\n")

        if command[:2] == ["/usr/bin/docker", "start"]:
            if not self.start_ok:
                return self._completed(command, 1, stderr="start denied")
            return self._completed(command, stdout=f"{self.container_id}\n")

        if command[:2] == ["/usr/bin/docker", "exec"]:
            tail = command[3:] if command[2] == self.container_id else command[2:]
            if tail == ["test", "-d", "/testbed"]:
                return self._completed(command, 0 if self.testbed_exists else 1)
            if tail == [
                "git", "-C", "/testbed", "rev-parse", "--is-inside-work-tree",
            ]:
                if self.git_repo:
                    return self._completed(command, stdout="true\n")
                return self._completed(command, 128, stderr="not a git repository")
            if tail == ["git", "-C", "/testbed", "rev-parse", "HEAD"]:
                return self._completed(command, stdout=f"{self.head}\n")
            if tail == ["git", "-C", "/testbed", "status", "--porcelain"]:
                if not self.status_ok:
                    return self._completed(command, 128, stderr="status failed")
                return self._completed(command, stdout=self.status)
            if tail[-3:] == ["bash", "-c", "command -v python"]:
                return self._completed(command, stdout=f"{self.python_path}\n")
            if tail[-3:] == ["bash", "-c", "python --version"]:
                return self._completed(command, stderr=f"{self.python_version}\n")

        if command[:3] == ["/usr/bin/docker", "rm", "--force"]:
            if not self.remove_ok:
                return self._completed(command, 1, stderr="remove denied")
            return self._completed(command, stdout=f"{self.container_id}\n")

        raise AssertionError(f"unexpected Docker command: {command!r}")


def _rollout(runner, **instance_overrides):
    return SWEbenchRolloutContainer(
        _instance(**instance_overrides),
        run_id="run-1",
        executable_finder=lambda _name: "/usr/bin/docker",
        command_runner=runner,
    )


def _commands(runner):
    return [command for command, _kwargs in runner.calls]


def test_start_fails_clearly_when_docker_executable_is_missing():
    rollout = SWEbenchRolloutContainer(
        _instance(),
        run_id="run-1",
        executable_finder=lambda _name: None,
        command_runner=lambda *_args, **_kwargs: pytest.fail("runner called"),
    )

    with pytest.raises(RolloutContainerError, match="Docker executable"):
        rollout.start()


def test_start_fails_clearly_when_docker_daemon_is_unavailable():
    runner = FakeDockerRunner(daemon_ok=False)

    with pytest.raises(RolloutContainerError, match="Docker daemon.*unavailable"):
        _rollout(runner).start()


def test_local_image_is_inspected_without_pull_and_metadata_is_recorded():
    runner = FakeDockerRunner()

    info = _rollout(runner).start()

    commands = _commands(runner)
    assert ["/usr/bin/docker", "pull"] not in [command[:2] for command in commands]
    assert info.image == "registry.example/official/task:latest"
    assert info.image_id == runner.image_id
    assert info.container_id == runner.container_id


def test_missing_image_is_pulled_then_inspected():
    runner = FakeDockerRunner(image_local=False)

    info = _rollout(runner).start()

    commands = _commands(runner)
    assert [
        "/usr/bin/docker",
        "pull",
        "registry.example/official/task:latest",
    ] in commands
    image_inspects = [
        command for command in commands if command[1:3] == ["image", "inspect"]
    ]
    assert len(image_inspects) == 2
    assert info.image_id == runner.image_id


def test_image_pull_failure_is_reported_without_container_cleanup():
    runner = FakeDockerRunner(image_local=False, pull_ok=False)
    rollout = _rollout(runner)

    with pytest.raises(RolloutContainerError, match="pull.*pull denied"):
        rollout.start()

    assert not any(command[1:2] == ["rm"] for command in _commands(runner))


def test_existing_container_name_fails_without_removing_unknown_container():
    runner = FakeDockerRunner(existing_container=True)

    with pytest.raises(RolloutContainerError, match="already exists"):
        _rollout(runner).start()

    commands = _commands(runner)
    assert not any(command[1:2] == ["create"] for command in commands)
    assert not any(command[1:2] == ["rm"] for command in commands)


def test_create_and_start_match_official_container_runtime_semantics():
    runner = FakeDockerRunner()
    rollout = _rollout(runner)

    rollout.start()

    commands = _commands(runner)
    assert [
        "/usr/bin/docker",
        "create",
        "--name",
        rollout.container_name,
        "--user",
        "root",
        "--cap-add",
        "SYS_ADMIN",
        rollout.image,
        "tail",
        "-f",
        "/dev/null",
    ] in commands
    assert ["/usr/bin/docker", "start", runner.container_id] in commands


def test_create_failure_does_not_claim_or_remove_a_container():
    runner = FakeDockerRunner(create_ok=False)
    rollout = _rollout(runner)

    with pytest.raises(RolloutContainerError, match="create.*create denied"):
        rollout.start()

    assert not any(command[1:2] == ["rm"] for command in _commands(runner))


def test_start_failure_cleans_container_owned_immediately_after_create():
    runner = FakeDockerRunner(start_ok=False)
    rollout = _rollout(runner)

    with pytest.raises(RolloutContainerError, match="start.*start denied"):
        rollout.start()

    assert [
        "/usr/bin/docker", "rm", "--force", runner.container_id,
    ] in _commands(runner)


def test_preflight_records_initial_state_without_enforcing_dataset_base_commit():
    runner = FakeDockerRunner(head="b" * 40, status=" M prepared.py\n")

    info = _rollout(runner, base_commit="a" * 40).start()

    assert info.instance_id == "owner__repo-123"
    assert info.container_name.startswith("codeharness.sweb.owner-repo-123.run-1.")
    assert info.initial_head == "b" * 40
    assert info.initial_status == " M prepared.py\n"
    assert info.python_path == "/opt/pyvenv/bin/python"
    assert info.python_version == "Python 3.9.20"


def test_preflight_is_read_only_and_never_passes_hidden_evaluator_fields():
    runner = FakeDockerRunner()

    _rollout(runner).start()

    flattened = "\0".join(
        argument
        for command in _commands(runner)
        for argument in command
    )
    for private_value in (
        "GOLD PATCH MUST STAY PRIVATE",
        "TEST PATCH MUST STAY PRIVATE",
        "private::failing",
        "private::passing",
        "PRIVATE EVALUATOR SCRIPT",
    ):
        assert private_value not in flattened
    for mutating_git_operation in ("reset", "checkout", "clean", "apply"):
        assert mutating_git_operation not in flattened.split("\0")


def test_missing_testbed_fails_and_cleans_owned_container():
    runner = FakeDockerRunner(testbed_exists=False)

    with pytest.raises(RolloutContainerError, match="/testbed.*does not exist"):
        _rollout(runner).start()

    assert _commands(runner)[-1] == [
        "/usr/bin/docker", "rm", "--force", runner.container_id,
    ]


def test_non_git_testbed_fails_and_cleans_owned_container():
    runner = FakeDockerRunner(git_repo=False)

    with pytest.raises(RolloutContainerError, match="/testbed.*Git repository"):
        _rollout(runner).start()

    assert _commands(runner)[-1] == [
        "/usr/bin/docker", "rm", "--force", runner.container_id,
    ]


def test_metadata_inspection_failure_cleans_owned_container():
    runner = FakeDockerRunner(status_ok=False)

    with pytest.raises(RolloutContainerError, match="status.*status failed"):
        _rollout(runner).start()

    assert _commands(runner)[-1] == [
        "/usr/bin/docker", "rm", "--force", runner.container_id,
    ]


def test_close_removes_only_owned_container_and_is_idempotent():
    runner = FakeDockerRunner()
    rollout = _rollout(runner)

    rollout.start()
    rollout.close()
    rollout.close()

    assert _commands(runner).count(
        ["/usr/bin/docker", "rm", "--force", runner.container_id]
    ) == 1


def test_close_before_start_never_removes_a_container():
    runner = FakeDockerRunner()
    rollout = _rollout(runner)

    rollout.close()

    assert runner.calls == []


def test_cleanup_failure_is_reported_and_can_be_retried():
    runner = FakeDockerRunner(remove_ok=False)
    rollout = _rollout(runner)
    rollout.start()

    with pytest.raises(RolloutContainerError, match="remove.*remove denied"):
        rollout.close()

    runner.remove_ok = True
    rollout.close()
    assert _commands(runner).count(
        ["/usr/bin/docker", "rm", "--force", runner.container_id]
    ) == 2


def test_context_manager_cleans_up_after_body_exception():
    runner = FakeDockerRunner()

    with (
        pytest.raises(ValueError, match="body failed"),
        _rollout(runner) as rollout,
    ):
        assert rollout.container_id == runner.container_id
        raise ValueError("body failed")

    assert _commands(runner)[-1] == [
        "/usr/bin/docker", "rm", "--force", runner.container_id,
    ]


def test_existing_backends_can_share_the_started_container():
    runner = FakeDockerRunner()
    rollout = _rollout(runner)

    info = rollout.start()
    workspace = DockerWorkspaceBackend(info.container_id)
    worktrees = DockerWorktreeEnvironment(info.container_id)

    assert workspace.container == runner.container_id
    assert worktrees.container == runner.container_id
