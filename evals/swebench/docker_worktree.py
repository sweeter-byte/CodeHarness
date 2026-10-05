"""Git worktree execution inside an already-running SWE-bench container."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

from evals.swebench.docker_workspace import (
    ALLOWED_WORKSPACE_ROOTS,
    SHELL_LAUNCHER,
    WORKSPACE_ROOT,
    DockerWorkspaceBackend,
)

_GIT_SCRIPT = r"""
import json
import os
import sys
from pathlib import Path

allowed_roots = [Path(root).resolve() for root in json.loads(sys.argv[1])]


def is_allowed(path):
    return any(path == root or root in path.parents for root in allowed_roots)


cwd = Path.cwd().resolve()
if not is_allowed(cwd):
    print(f"cwd escapes Docker workspace roots: {cwd}", file=sys.stderr)
    raise SystemExit(2)

for argument in sys.argv[2:]:
    raw_path = Path(argument)
    if raw_path.is_absolute():
        path = raw_path.resolve(strict=False)
        if not is_allowed(path):
            print(
                f"Path escapes Docker workspace roots: {argument}",
                file=sys.stderr,
            )
            raise SystemExit(2)

os.execvp("git", ["git", *sys.argv[2:]])
""".strip()

_PATH_EXISTS_SCRIPT = r"""
import json
import sys
from pathlib import Path

allowed_roots = [Path(root).resolve() for root in json.loads(sys.argv[1])]
path = Path(sys.argv[2]).resolve(strict=False)
if not any(path == root or root in path.parents for root in allowed_roots):
    raise SystemExit(2)
raise SystemExit(0 if path.exists() else 1)
""".strip()


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class DockerWorktreeEnvironment:
    """Run Team worktree operations in one persistent Docker container."""

    def __init__(
        self,
        container: str,
        *,
        command_runner: CommandRunner = subprocess.run,
        timeout: int = 60,
    ) -> None:
        if not container or container.startswith("-") or "\x00" in container:
            raise ValueError("container must be a non-empty Docker name or ID")
        self.container = container
        self.command_runner = command_runner
        self.timeout = timeout

    @staticmethod
    def _roots_json() -> str:
        return json.dumps([str(root) for root in ALLOWED_WORKSPACE_ROOTS])

    @staticmethod
    def _cwd(cwd: str) -> str:
        return DockerWorkspaceBackend._cwd(cwd)

    @staticmethod
    def _path(path: str) -> str:
        return DockerWorkspaceBackend._path(path, cwd=str(WORKSPACE_ROOT))

    def git(
        self,
        args: Sequence[str],
        cwd: str,
    ) -> tuple[int, str]:
        try:
            effective_cwd = self._cwd(str(cwd))
            for argument in args:
                if PurePosixPath(argument).is_absolute():
                    self._path(argument)
        except ValueError as exc:
            return -1, str(exc)

        command = [
            "docker",
            "exec",
            "-e",
            "BASH_ENV=/root/.bashrc",
            "-w",
            effective_cwd,
            self.container,
            "bash",
            "-c",
            SHELL_LAUNCHER,
            "_",
            _GIT_SCRIPT,
            self._roots_json(),
            *args,
        ]
        try:
            completed = self.command_runner(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return -1, str(exc)
        return (
            completed.returncode,
            (completed.stdout + completed.stderr).strip(),
        )

    def path_exists(self, path: str) -> bool:
        try:
            target = self._path(str(path))
        except ValueError:
            return False
        command = [
            "docker",
            "exec",
            "-w",
            "/",
            self.container,
            "python",
            "-c",
            _PATH_EXISTS_SCRIPT,
            self._roots_json(),
            target,
        ]
        try:
            completed = self.command_runner(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return completed.returncode == 0
