"""Lifecycle for one persistent container from an official SWE-bench image."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from evals.swebench.dataset import SWEbenchInstance


class RolloutContainerError(RuntimeError):
    """Raised when an official SWE-bench rollout container cannot be managed."""


def resolve_official_image(instance: SWEbenchInstance) -> str:
    """Return the exact official image reference carried by the dataset row."""
    image = instance.raw.get("image")
    if not isinstance(image, str) or not image.strip():
        raise RolloutContainerError(
            "SWE-bench official image is missing from instance.raw['image']; "
            "refusing to guess an image reference."
        )
    return image.strip()


def _safe_name_part(value: str, *, limit: int = 80) -> str:
    part = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-.")
    part = part[:limit].rstrip("-.")
    return part or "x"


def _container_name(instance_id: str, run_id: str) -> str:
    digest = hashlib.sha256(
        f"{instance_id}\0{run_id}".encode()
    ).hexdigest()[:12]
    return ".".join(
        (
            "codeharness",
            "sweb",
            _safe_name_part(instance_id),
            _safe_name_part(run_id),
            digest,
        )
    )


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]


def _official_container_user() -> str:
    try:
        from swebench.image_builder.constants import CONTAINER_USER
    except (ImportError, ModuleNotFoundError) as exc:
        raise RolloutContainerError(
            "The installed SWE-bench package does not expose CONTAINER_USER."
        ) from exc
    if not isinstance(CONTAINER_USER, str) or not CONTAINER_USER.strip():
        raise RolloutContainerError(
            "The installed SWE-bench CONTAINER_USER is not a non-empty string."
        )
    return CONTAINER_USER.strip()


@dataclass(frozen=True)
class RolloutContainerInfo:
    """Read-only baseline metadata captured from a started task container."""

    instance_id: str
    image: str
    image_id: str
    container_name: str
    container_id: str
    initial_head: str
    initial_status: str
    python_path: str
    python_version: str


class SWEbenchRolloutContainer:
    """Own one persistent container created from an official task image."""

    def __init__(
        self,
        instance: SWEbenchInstance,
        run_id: str,
        *,
        executable_finder: ExecutableFinder = shutil.which,
        command_runner: CommandRunner = subprocess.run,
        timeout: int = 600,
    ) -> None:
        self.instance = instance
        self.run_id = run_id
        self.image = resolve_official_image(instance)
        self.container_name = _container_name(instance.instance_id, run_id)
        self.executable_finder = executable_finder
        self.command_runner = command_runner
        self.timeout = timeout
        self.container_id: str | None = None
        self.info: RolloutContainerInfo | None = None
        self._docker: str | None = None
        self._owned = False
        self._cleanup_target: str | None = None

    @staticmethod
    def _detail(completed: subprocess.CompletedProcess[str]) -> str:
        return (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit code {completed.returncode}"
        )

    def _execute(
        self,
        arguments: list[str],
        *,
        label: str,
    ) -> subprocess.CompletedProcess[str]:
        if self._docker is None:
            raise RolloutContainerError("Docker executable is not available.")
        command = [self._docker, *arguments]
        try:
            return self.command_runner(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RolloutContainerError(
                f"{label} timed out after {self.timeout}s."
            ) from exc
        except OSError as exc:
            raise RolloutContainerError(f"{label} failed: {exc}") from exc

    def _checked(self, arguments: list[str], *, label: str) -> str:
        completed = self._execute(arguments, label=label)
        if completed.returncode != 0:
            raise RolloutContainerError(
                f"{label} failed: {self._detail(completed)}"
            )
        return completed.stdout

    def _ensure_image(self) -> str:
        inspect_arguments = [
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            self.image,
        ]
        inspected = self._execute(
            inspect_arguments,
            label="Docker image inspection",
        )
        if inspected.returncode != 0:
            detail = self._detail(inspected)
            missing = (
                "no such image" in detail.lower()
                or "not found" in detail.lower()
            )
            if not missing:
                raise RolloutContainerError(
                    f"Docker image inspection failed: {detail}"
                )
            self._checked(
                ["pull", self.image],
                label="Docker image pull",
            )
            inspected = self._execute(
                inspect_arguments,
                label="Docker image inspection after pull",
            )
            if inspected.returncode != 0:
                raise RolloutContainerError(
                    "Docker image inspection after pull failed: "
                    f"{self._detail(inspected)}"
                )
        image_id = inspected.stdout.strip()
        if not image_id:
            raise RolloutContainerError(
                f"Docker image inspection returned no ID for {self.image}."
            )
        return image_id

    def _ensure_name_available(self) -> None:
        output = self._checked(
            [
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{self.container_name}$",
                "--format",
                "{{.ID}}",
            ],
            label="Docker container name inspection",
        )
        if output.strip():
            raise RolloutContainerError(
                f"Docker container {self.container_name!r} already exists; "
                "refusing to take ownership."
            )

    def _create(self) -> None:
        completed = self._execute(
            [
                "create",
                "--name",
                self.container_name,
                "--network",
                "none",
                "--user",
                _official_container_user(),
                "--cap-add",
                "SYS_ADMIN",
                self.image,
                "tail",
                "-f",
                "/dev/null",
            ],
            label="Docker container create",
        )
        if completed.returncode != 0:
            raise RolloutContainerError(
                f"Docker container create failed: {self._detail(completed)}"
            )

        # A successful create transfers lifecycle ownership before any later work.
        self._owned = True
        self._cleanup_target = self.container_name

        container_id = completed.stdout.strip()
        if not container_id:
            raise RolloutContainerError(
                "Docker container create returned an empty container ID."
            )
        self.container_id = container_id
        self._cleanup_target = container_id

    def _exec(self, arguments: list[str], *, label: str) -> str:
        if self.container_id is None:
            raise RolloutContainerError(
                f"{label} failed: rollout container has no ID."
            )
        return self._checked(
            ["exec", self.container_id, *arguments],
            label=label,
        )

    def _task_shell(self, command: str, *, label: str) -> str:
        if self.container_id is None:
            raise RolloutContainerError(
                f"{label} failed: rollout container has no ID."
            )
        completed = self._execute(
            [
                "exec",
                "-e",
                "BASH_ENV=/root/.bashrc",
                "-w",
                "/testbed",
                self.container_id,
                "bash",
                "-c",
                command,
            ],
            label=label,
        )
        if completed.returncode != 0:
            raise RolloutContainerError(
                f"{label} failed: {self._detail(completed)}"
            )
        return completed.stdout + completed.stderr

    def _inspect_runtime(self, image_id: str) -> RolloutContainerInfo:
        exists = self._execute(
            ["exec", self.container_id or "", "test", "-d", "/testbed"],
            label="SWE-bench /testbed inspection",
        )
        if exists.returncode != 0:
            raise RolloutContainerError(
                "SWE-bench /testbed does not exist in the rollout container."
            )

        repository = self._execute(
            [
                "exec",
                self.container_id or "",
                "git",
                "-C",
                "/testbed",
                "rev-parse",
                "--is-inside-work-tree",
            ],
            label="SWE-bench Git repository inspection",
        )
        if repository.returncode != 0 or repository.stdout.strip() != "true":
            raise RolloutContainerError(
                "SWE-bench /testbed is not a Git repository: "
                f"{self._detail(repository)}"
            )

        initial_head = self._exec(
            ["git", "-C", "/testbed", "rev-parse", "HEAD"],
            label="SWE-bench initial HEAD inspection",
        ).strip()
        if not initial_head:
            raise RolloutContainerError(
                "SWE-bench initial HEAD inspection returned no commit."
            )
        initial_status = self._exec(
            ["git", "-C", "/testbed", "status", "--porcelain"],
            label="SWE-bench initial status inspection",
        )
        python_path = self._task_shell(
            "command -v python",
            label="SWE-bench Python path inspection",
        ).strip()
        if not python_path:
            raise RolloutContainerError(
                "SWE-bench Python path inspection returned no path."
            )
        python_version = self._task_shell(
            "python --version",
            label="SWE-bench Python version inspection",
        ).strip()
        if not python_version:
            raise RolloutContainerError(
                "SWE-bench Python version inspection returned no version."
            )

        return RolloutContainerInfo(
            instance_id=self.instance.instance_id,
            image=self.image,
            image_id=image_id,
            container_name=self.container_name,
            container_id=self.container_id or "",
            initial_head=initial_head,
            initial_status=initial_status,
            python_path=python_path,
            python_version=python_version,
        )

    def start(self) -> RolloutContainerInfo:
        """Create, start, and inspect the owned rollout container."""
        if self.info is not None and self._owned:
            return self.info
        if self._owned:
            raise RolloutContainerError(
                "Rollout container startup is already in progress."
            )

        self._docker = self.executable_finder("docker")
        if self._docker is None:
            raise RolloutContainerError("Docker executable is not available.")

        try:
            self._checked(["info"], label="Docker daemon check")
            image_id = self._ensure_image()
            self._ensure_name_available()
            self._create()
            self._checked(
                ["start", self.container_id or self.container_name],
                label="Docker container start",
            )
            self.info = self._inspect_runtime(image_id)
            return self.info
        except Exception as exc:
            if self._owned:
                try:
                    self.close()
                except RolloutContainerError as cleanup_exc:
                    raise RolloutContainerError(
                        f"{exc} Cleanup also failed: {cleanup_exc}"
                    ) from exc
            if isinstance(exc, RolloutContainerError):
                raise
            raise RolloutContainerError(
                f"SWE-bench rollout container startup failed: {exc}"
            ) from exc

    def close(self) -> None:
        """Remove this lifecycle's container, if one is currently owned."""
        if not self._owned:
            return
        target = self._cleanup_target
        if not target:
            raise RolloutContainerError(
                "Cannot remove owned rollout container without an identifier."
            )
        self._checked(
            ["rm", "--force", target],
            label="Docker container remove",
        )
        self._owned = False
        self._cleanup_target = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        if exc is None:
            self.close()
        else:
            try:
                self.close()
            except RolloutContainerError as cleanup_exc:
                exc.add_note(f"Rollout container cleanup failed: {cleanup_exc}")
        return False
