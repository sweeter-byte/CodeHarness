"""Prepare an isolated repository checkout for one SWE-bench instance."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.swebench.dataset import SWEbenchInstance


class WorkspacePreparationError(RuntimeError):
    """Raised when a SWE-bench repository cannot be prepared safely."""


@dataclass(frozen=True)
class PreparedSWEbenchWorkspace:
    case_root: Path
    workspace: Path
    agent_home: Path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class SWEbenchWorkspaceManager:
    """Clone one repository at its exact base commit outside CodeHarness."""

    def __init__(
        self,
        run_id: str,
        *,
        work_root: str | Path | None = None,
        repository_root: str | Path,
        command_runner: CommandRunner = subprocess.run,
    ):
        self.run_id = run_id
        self.repository_root = Path(repository_root).expanduser().resolve()
        self.work_root = (
            Path(
                work_root
                if work_root is not None
                else Path(tempfile.gettempdir()) / "codeharness-swebench"
            )
            .expanduser()
            .resolve()
        )
        if self.work_root == self.repository_root or self.work_root.is_relative_to(
            self.repository_root
        ):
            raise ValueError(
                "SWE-bench work root must be outside the CodeHarness repository"
            )
        self.command_runner = command_runner

    @staticmethod
    def clone_url(repo: str) -> str:
        parts = repo.split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise WorkspacePreparationError(
                f"invalid SWE-bench repository name: {repo!r}"
            )
        return f"https://github.com/{repo}.git"

    @staticmethod
    def _safe_instance_id(instance_id: str) -> str:
        if (
            not instance_id
            or Path(instance_id).name != instance_id
            or "/" in instance_id
            or "\\" in instance_id
            or instance_id in {".", ".."}
        ):
            raise WorkspacePreparationError(
                f"invalid SWE-bench instance id: {instance_id!r}"
            )
        return instance_id

    def _run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        label: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = self.command_runner(
                list(command),
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise WorkspacePreparationError(f"{label} failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise WorkspacePreparationError(f"{label} failed: {detail}")
        return completed

    def prepare(self, instance: SWEbenchInstance) -> PreparedSWEbenchWorkspace:
        instance_id = self._safe_instance_id(instance.instance_id)
        case_root = (self.work_root / self.run_id / instance_id).resolve()
        workspace = case_root / "workspace"
        agent_home = case_root / "agent-home"
        if case_root.exists():
            raise FileExistsError(f"SWE-bench workspace already exists: {case_root}")
        case_root.mkdir(parents=True)

        self._run(
            ["git", "clone", self.clone_url(instance.repo), str(workspace)],
            cwd=case_root,
            label="git clone",
        )
        self._run(
            ["git", "checkout", "--detach", instance.base_commit],
            cwd=workspace,
            label="git checkout",
        )
        head = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            label="git rev-parse HEAD",
        ).stdout.strip()
        if head != instance.base_commit:
            raise WorkspacePreparationError(
                "checkout HEAD mismatch: "
                f"expected {instance.base_commit}, got {head or '<empty>'}"
            )
        status = self._run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            label="git status",
        ).stdout
        if status.strip():
            raise WorkspacePreparationError(
                "SWE-bench checkout is not clean after checkout"
            )
        agent_home.mkdir()
        return PreparedSWEbenchWorkspace(case_root, workspace, agent_home)
