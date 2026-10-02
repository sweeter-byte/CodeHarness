"""Canonical filesystem locations for one CodeHarness runtime."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _run_git(workspace: Path, argument: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", argument],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _project_identity(workspace: Path) -> tuple[Path, str]:
    common_dir = _run_git(workspace, "--git-common-dir")
    if common_dir is None:
        return workspace, workspace.name

    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = workspace / common_path
    common_path = common_path.resolve()

    if common_path.name == ".git":
        project_name = common_path.parent.name
    else:
        project_name = common_path.name.removesuffix(".git")
    return common_path, project_name


def _sanitize_project_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return sanitized or "project"


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved storage scopes without creating any directories."""

    workspace: Path
    agent_home: Path

    project_root: Path
    state_root: Path
    tasks_dir: Path
    scheduler_dir: Path
    scheduler_jobs_file: Path

    runs_root: Path
    workflow_runs_dir: Path

    runtime_root: Path
    team_runtime_dir: Path
    mailboxes_dir: Path

    artifacts_dir: Path
    transcripts_dir: Path
    worktrees_dir: Path

    project_id: str
    project_home: Path
    project_memory_dir: Path

    global_memory_dir: Path

    @classmethod
    def build(
        cls,
        workspace: str | Path,
        agent_home: str | Path | None = None,
    ) -> "RuntimePaths":
        resolved_workspace = Path(workspace).expanduser().resolve()
        resolved_agent_home = Path(
            agent_home
            if agent_home is not None
            else os.environ.get("CODEHARNESS_HOME", "~/.codeharness")
        ).expanduser().resolve()

        identity, project_name = _project_identity(resolved_workspace)
        digest = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:10]
        project_id = f"{_sanitize_project_name(project_name)}-{digest}"

        project_root = resolved_workspace / ".codeharness"
        state_root = project_root / "state"
        scheduler_dir = state_root / "scheduler"
        runs_root = project_root / "runs"
        runtime_root = project_root / "runtime"
        team_runtime_dir = runtime_root / "team"
        project_home = resolved_agent_home / "projects" / project_id

        return cls(
            workspace=resolved_workspace,
            agent_home=resolved_agent_home,
            project_root=project_root,
            state_root=state_root,
            tasks_dir=state_root / "tasks",
            scheduler_dir=scheduler_dir,
            scheduler_jobs_file=scheduler_dir / "jobs.json",
            runs_root=runs_root,
            workflow_runs_dir=runs_root / "workflows",
            runtime_root=runtime_root,
            team_runtime_dir=team_runtime_dir,
            mailboxes_dir=team_runtime_dir / "mailboxes",
            artifacts_dir=project_root / "artifacts",
            transcripts_dir=project_root / "transcripts",
            worktrees_dir=project_root / "worktrees",
            project_id=project_id,
            project_home=project_home,
            project_memory_dir=project_home / "memory",
            global_memory_dir=resolved_agent_home / "memory",
        )
