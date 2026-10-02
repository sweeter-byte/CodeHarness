"""Disposable workspace preparation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from evals.runner.case import EvalCase


@dataclass(frozen=True)
class PreparedWorkspace:
    case_root: Path
    workspace: Path
    agent_home: Path


class WorkspaceManager:
    """Create one fixture copy and local agent home per case."""

    def __init__(
        self,
        run_id: str,
        work_root: str | Path | None = None,
        repository_root: str | Path | None = None,
    ):
        self.run_id = run_id
        self.repository_root = Path(
            repository_root or Path(__file__).resolve().parents[2]
        ).resolve()
        configured_root = work_root or os.environ.get(
            "CODEHARNESS_EVAL_WORK_ROOT"
        )
        self.work_root = Path(
            configured_root
            if configured_root
            else Path(tempfile.gettempdir()) / "codeharness-eval"
        ).expanduser().resolve()
        if self.work_root == self.repository_root or self.work_root.is_relative_to(
            self.repository_root
        ):
            raise ValueError(
                "evaluation work root must be outside the CodeHarness repository"
            )

    def prepare(self, case: EvalCase) -> PreparedWorkspace:
        case_root = self.work_root / self.run_id / case.case_id
        workspace = case_root / "workspace"
        agent_home = case_root / "agent-home"
        if case_root.exists():
            raise FileExistsError(f"case workspace already exists: {case_root}")
        case_root.mkdir(parents=True)
        shutil.copytree(case.fixture_dir, workspace)
        agent_home.mkdir()
        self._initialize_git(workspace)
        return PreparedWorkspace(case_root, workspace, agent_home)

    @staticmethod
    def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        )

    def _initialize_git(self, workspace: Path) -> None:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if probe.returncode == 0:
            return
        self._git(workspace, "init")
        self._git(workspace, "config", "user.email", "eval@example.invalid")
        self._git(workspace, "config", "user.name", "CodeHarness Eval")
        self._git(workspace, "add", ".")
        self._git(workspace, "commit", "-m", "evaluation baseline")
