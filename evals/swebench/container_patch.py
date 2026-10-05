"""Patch capture from a borrowed SWE-bench rollout container."""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from types import TracebackType
from typing import Self


class ContainerPatchError(RuntimeError):
    """Raised when container-backed patch collection cannot proceed."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]
IdentifierFactory = Callable[[], str]

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TEMP_ROOT = "/tmp/codeharness-patch"
_WORKSPACE = "/testbed"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "CodeHarness Evaluation",
    "GIT_AUTHOR_EMAIL": "evaluation@codeharness.invalid",
    "GIT_COMMITTER_NAME": "CodeHarness Evaluation",
    "GIT_COMMITTER_EMAIL": "evaluation@codeharness.invalid",
}


def _default_identifier() -> str:
    return secrets.token_hex(16)


class ContainerPatchCollector:
    """Compare startup and final `/testbed` filesystems in one container."""

    def __init__(
        self,
        container_id: str,
        *,
        executable_finder: ExecutableFinder = shutil.which,
        command_runner: CommandRunner = subprocess.run,
        id_factory: IdentifierFactory = _default_identifier,
        timeout: int = 30,
    ) -> None:
        if not container_id.strip():
            raise ValueError("container_id must be non-empty")
        identifier = id_factory()
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("collector identifier is not path-safe")

        self.container_id = container_id
        self.executable_finder = executable_finder
        self.command_runner = command_runner
        self.timeout = timeout
        self.identifier = identifier
        self.temp_dir = f"{_TEMP_ROOT}/{identifier}"
        self.index_file = f"{self.temp_dir}/index"
        self.baseline_ref = (
            f"refs/codeharness-eval/baseline/{identifier}"
        )
        self.baseline_tree: str | None = None
        self.baseline_commit: str | None = None
        self.final_tree: str | None = None
        self._docker: str | None = None
        self._temp_created = False
        self._ref_created = False
        self._capture_attempted = False

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
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self._docker is None:
            self._docker = self.executable_finder("docker")
        if self._docker is None:
            raise ContainerPatchError("Docker executable is not available.")

        environment_arguments: list[str] = []
        for name, value in sorted((environment or {}).items()):
            environment_arguments.extend(["-e", f"{name}={value}"])
        command = [
            self._docker,
            "exec",
            "-w",
            _WORKSPACE,
            *environment_arguments,
            self.container_id,
            *arguments,
        ]
        try:
            return self.command_runner(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerPatchError(
                f"{label} timed out after {self.timeout}s."
            ) from exc
        except OSError as exc:
            raise ContainerPatchError(f"{label} failed: {exc}") from exc

    def _checked(
        self,
        arguments: list[str],
        *,
        label: str,
        environment: dict[str, str] | None = None,
    ) -> str:
        completed = self._execute(
            arguments,
            label=label,
            environment=environment,
        )
        if completed.returncode != 0:
            raise ContainerPatchError(
                f"{label} failed: {self._detail(completed)}"
            )
        return completed.stdout

    def _index_environment(self) -> dict[str, str]:
        return {"GIT_INDEX_FILE": self.index_file}

    def snapshot_baseline(self) -> str:
        """Snapshot the current `/testbed` filesystem and anchor its tree."""
        if self.baseline_tree is not None:
            raise ContainerPatchError("Container baseline is already captured.")

        try:
            self._checked(
                ["mkdir", "-p", self.temp_dir],
                label="Container patch temp directory creation",
            )
            self._temp_created = True
            index_environment = self._index_environment()
            self._checked(
                ["git", "read-tree", "HEAD"],
                label="Container baseline index initialization",
                environment=index_environment,
            )
            self._checked(
                ["git", "add", "-A", "--", "."],
                label="Container baseline filesystem staging",
                environment=index_environment,
            )
            baseline_tree = self._checked(
                ["git", "write-tree"],
                label="Container baseline tree creation",
                environment=index_environment,
            ).strip()
            if not baseline_tree:
                raise ContainerPatchError(
                    "Container baseline tree creation returned no object ID."
                )
            baseline_commit = self._checked(
                [
                    "git",
                    "commit-tree",
                    baseline_tree,
                    "-m",
                    "CodeHarness evaluation baseline",
                ],
                label="Container baseline commit creation",
                environment=_IDENTITY,
            ).strip()
            if not baseline_commit:
                raise ContainerPatchError(
                    "Container baseline commit creation returned no object ID."
                )
            self._checked(
                ["git", "update-ref", self.baseline_ref, baseline_commit],
                label="Container baseline ref creation",
            )
            self._ref_created = True
            self.baseline_tree = baseline_tree
            self.baseline_commit = baseline_commit
            return baseline_tree
        except Exception as exc:
            try:
                self.close()
            except ContainerPatchError as cleanup_exc:
                exc.add_note(f"Container patch cleanup failed: {cleanup_exc}")
            if isinstance(exc, ContainerPatchError):
                raise
            raise ContainerPatchError(
                f"Container baseline snapshot failed: {exc}"
            ) from exc

    def capture_patch(self) -> tuple[str, str | None]:
        """Return the binary-capable baseline-to-final `/testbed` diff."""
        if self.baseline_tree is None:
            return "", "Container baseline has not been captured."
        if self._capture_attempted:
            return "", "Container patch capture has already been attempted."
        self._capture_attempted = True

        patch = ""
        error: str | None = None
        try:
            index_environment = self._index_environment()
            self._checked(
                ["git", "read-tree", self.baseline_tree],
                label="Container final index initialization",
                environment=index_environment,
            )
            self._checked(
                ["git", "add", "-A", "--", "."],
                label="Container final filesystem staging",
                environment=index_environment,
            )
            final_tree = self._checked(
                ["git", "write-tree"],
                label="Container final tree creation",
                environment=index_environment,
            ).strip()
            if not final_tree:
                raise ContainerPatchError(
                    "Container final tree creation returned no object ID."
                )
            self.final_tree = final_tree
            patch = self._checked(
                [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--binary",
                    self.baseline_tree,
                    final_tree,
                ],
                label="Container patch diff creation",
            )
        except ContainerPatchError as exc:
            error = str(exc)
            patch = ""
        finally:
            try:
                self.close()
            except ContainerPatchError as cleanup_exc:
                patch = ""
                cleanup_detail = f"Container patch cleanup failed: {cleanup_exc}"
                error = (
                    f"{error} {cleanup_detail}" if error else cleanup_detail
                )
        return patch, error

    def close(self) -> None:
        """Best-effort remove the private ref, then the container temp dir."""
        errors: list[str] = []
        if self._ref_created:
            try:
                self._checked(
                    ["git", "update-ref", "-d", self.baseline_ref],
                    label="Container baseline ref deletion",
                )
                self._ref_created = False
            except ContainerPatchError as exc:
                errors.append(str(exc))
        if self._temp_created:
            try:
                self._checked(
                    ["rm", "-rf", "--", self.temp_dir],
                    label="Container patch temp directory cleanup",
                )
                self._temp_created = False
            except ContainerPatchError as exc:
                errors.append(str(exc))
        if errors:
            raise ContainerPatchError("; ".join(errors))

    def __enter__(self) -> Self:
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
            except ContainerPatchError as cleanup_exc:
                exc.add_note(f"Container patch cleanup failed: {cleanup_exc}")
        return False
