"""Workspace-tool execution inside an already-running SWE-bench container."""

from __future__ import annotations

import posixpath
import subprocess
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

WORKSPACE_ROOT = PurePosixPath("/testbed")
SHELL_LAUNCHER = 'python -c "$1" "${@:2}"'

_READ_SCRIPT = r"""
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2]).resolve(strict=False)
if path != root and root not in path.parents:
    print(f"Error: Path escapes Docker workspace {root}: {sys.argv[2]}")
elif not path.exists():
    print(f"Error: File not found: {sys.argv[2]}")
else:
    try:
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        start = int(sys.argv[3]) if sys.argv[3] else None
        end = int(sys.argv[4]) if sys.argv[4] else None
        if start is not None or end is not None:
            offset = (start or 1) - 1
            lines = lines[offset : end or len(lines)]
        else:
            offset = 0
        result = "".join(
            f"{index + offset + 1:>5}\t{line}"
            for index, line in enumerate(lines)
        ).rstrip()
        print(result or "(empty file)")
    except Exception as exc:
        print(f"Error: {exc}")
""".strip()

_WRITE_SCRIPT = r"""
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2]).resolve(strict=False)
if path != root and root not in path.parents:
    print(f"Error: Path escapes Docker workspace {root}: {sys.argv[3]}")
else:
    try:
        content = sys.stdin.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"Written {len(content)} chars to {sys.argv[3]}")
    except Exception as exc:
        print(f"Error: {exc}")
""".strip()

_EDIT_SCRIPT = r"""
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2]).resolve(strict=False)
display_path, old_text, new_text = sys.argv[3:6]
if path != root and root not in path.parents:
    print(f"Error: Path escapes Docker workspace {root}: {display_path}")
elif not path.exists():
    print(f"Error: File not found: {display_path}")
else:
    try:
        text = path.read_text(errors="replace")
        count = text.count(old_text)
        if count == 0:
            print("Error: old_text not found in file")
        elif count > 1:
            print(f"Error: old_text found {count} times, must be unique")
        else:
            path.write_text(text.replace(old_text, new_text, 1))
            print(f"Edited {display_path}")
    except Exception as exc:
        print(f"Error: {exc}")
""".strip()

_GLOB_SCRIPT = r"""
import glob
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
matches = []
for raw_match in sorted(glob.glob(sys.argv[2], recursive=True)):
    match = Path(raw_match)
    resolved = match.resolve(strict=False)
    if resolved == root or root in resolved.parents:
        try:
            matches.append(str(match.relative_to(root)))
        except ValueError:
            matches.append(str(match))
if not matches:
    print("(no matches)")
else:
    visible = matches[:200]
    if len(matches) > 200:
        visible.append(
            f"... ({len(matches)} total matches, showing first 200. "
            "Use a more specific pattern to narrow results.)"
        )
    print("\n".join(visible))
""".strip()

_GREP_SCRIPT = r"""
import re
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
pattern, raw_root, file_pattern = sys.argv[2:5]
try:
    regex = re.compile(pattern)
except re.error as exc:
    print(f"Error: Invalid regex: {exc}")
    raise SystemExit(0)
root = Path(raw_root).resolve(strict=False)
if root != workspace and workspace not in root.parents:
    print(f"Error: Path escapes Docker workspace {workspace}: {raw_root}")
elif not root.exists():
    print(f"Error: Path not found: {raw_root}")
else:
    results = []
    files = [root] if root.is_file() else root.rglob(file_pattern or "*")
    for candidate in files:
        resolved = candidate.resolve(strict=False)
        if resolved != workspace and workspace not in resolved.parents:
            continue
        if not candidate.is_file():
            continue
        try:
            for line_number, line in enumerate(
                candidate.read_text(errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    relative = candidate.relative_to(workspace)
                    results.append(f"{relative}:{line_number}: {line.strip()}")
        except (PermissionError, OSError):
            continue
        if len(results) > 500:
            results.append(
                "... (truncated at 500 matches. "
                "Use a more specific pattern or file_pattern to narrow results.)"
            )
            break
    print("\n".join(results) if results else "(no matches)")
""".strip()


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class DockerWorkspaceBackend:
    """Run coding tools against `/testbed` in a persistent Docker container."""

    def __init__(
        self,
        container: str,
        *,
        command_runner: CommandRunner = subprocess.run,
        timeout: int = 120,
    ) -> None:
        if not container or container.startswith("-") or "\x00" in container:
            raise ValueError("container must be a non-empty Docker name or ID")
        self.container = container
        self.command_runner = command_runner
        self.timeout = timeout

    @staticmethod
    def _path(path: str) -> str:
        raw = PurePosixPath(path)
        if ".." in raw.parts:
            raise ValueError(
                f"Path escapes Docker workspace {WORKSPACE_ROOT}: {path}"
            )
        candidate = raw if raw.is_absolute() else WORKSPACE_ROOT / raw
        normalized = PurePosixPath(posixpath.normpath(str(candidate)))
        if normalized != WORKSPACE_ROOT and WORKSPACE_ROOT not in normalized.parents:
            raise ValueError(
                f"Path escapes Docker workspace {WORKSPACE_ROOT}: {path}"
            )
        return str(normalized)

    @staticmethod
    def _validate_file_pattern(file_pattern: str | None) -> str | None:
        if file_pattern is None:
            return None
        pattern = PurePosixPath(file_pattern)
        if pattern.is_absolute() or ".." in pattern.parts:
            raise ValueError(
                "file_pattern must stay within Docker workspace: "
                f"{file_pattern}"
            )
        return file_pattern

    def _exec(
        self,
        command: Sequence[str],
        *,
        operation: str,
        input_text: str | None = None,
    ) -> str:
        argv = [
            "docker",
            "exec",
            "-e",
            "BASH_ENV=/root/.bashrc",
            "-w",
            str(WORKSPACE_ROOT),
            self.container,
            *command,
        ]
        kwargs = {
            "capture_output": True,
            "text": True,
            "errors": "replace",
            "timeout": self.timeout,
        }
        if input_text is not None:
            argv.insert(2, "-i")
            kwargs["input"] = input_text
        try:
            completed = self.command_runner(argv, **kwargs)
        except subprocess.TimeoutExpired:
            return (
                f"Error: Docker {operation} in container {self.container} "
                f"timed out after {self.timeout}s"
            )
        except (FileNotFoundError, OSError) as exc:
            return (
                f"Error: Docker {operation} failed in container "
                f"{self.container}: {exc}"
            )

        output = (completed.stdout + completed.stderr).rstrip()
        if completed.returncode != 0:
            detail = output.strip() or f"exit code {completed.returncode}"
            return (
                f"Error: Docker {operation} failed in container {self.container} "
                f"(exit code {completed.returncode}): {detail}"
            )
        return output[:50000] or "(no output)"

    def _helper(
        self,
        script: str,
        arguments: Sequence[str],
        *,
        operation: str,
        input_text: str | None = None,
    ) -> str:
        return self._exec(
            [
                "bash",
                "-c",
                SHELL_LAUNCHER,
                "_",
                script,
                str(WORKSPACE_ROOT),
                *arguments,
            ],
            operation=operation,
            input_text=input_text,
        )

    def bash(
        self,
        command: str,
        run_in_background: bool = False,
        cwd: str | None = None,
    ) -> str:
        del cwd
        if run_in_background:
            return "Error: Docker background execution is not supported"
        return self._exec(["bash", "-c", command], operation="bash command")

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cwd: str | None = None,
    ) -> str:
        del cwd
        try:
            target = self._path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _READ_SCRIPT,
            [
                target,
                "" if start_line is None else str(start_line),
                "" if end_line is None else str(end_line),
            ],
            operation="read_file",
        )

    def write_file(
        self,
        path: str,
        content: str,
        cwd: str | None = None,
    ) -> str:
        del cwd
        try:
            target = self._path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _WRITE_SCRIPT,
            [target, path],
            operation="write_file",
            input_text=content,
        )

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        cwd: str | None = None,
    ) -> str:
        del cwd
        try:
            target = self._path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _EDIT_SCRIPT,
            [target, path, old_text, new_text],
            operation="edit_file",
        )

    def glob(self, pattern: str, cwd: str | None = None) -> str:
        del cwd
        try:
            mapped_pattern = self._path(pattern)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _GLOB_SCRIPT,
            [mapped_pattern],
            operation="glob",
        )

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        cwd: str | None = None,
    ) -> str:
        del cwd
        try:
            target = self._path(path)
            safe_file_pattern = self._validate_file_pattern(file_pattern)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _GREP_SCRIPT,
            [pattern, target, safe_file_pattern or ""],
            operation="grep",
        )
