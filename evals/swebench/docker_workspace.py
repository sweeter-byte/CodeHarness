"""Workspace-tool execution inside an already-running SWE-bench container."""

from __future__ import annotations

import json
import posixpath
import subprocess
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

WORKSPACE_ROOT = PurePosixPath("/testbed")
WORKTREE_ROOT = PurePosixPath("/tmp/codeharness-worktrees")
ALLOWED_WORKSPACE_ROOTS = (WORKSPACE_ROOT, WORKTREE_ROOT)
SHELL_LAUNCHER = 'python -c "$1" "${@:2}"'

_CONFINEMENT_SCRIPT = r"""
import json
import sys
from pathlib import Path

allowed_roots = [
    Path(raw_root).resolve()
    for raw_root in json.loads(sys.argv[1])
]


def _is_allowed(path):
    return any(path == root or root in path.parents for root in allowed_roots)


def _confined_path(raw_path, display_path):
    path = Path(raw_path).resolve(strict=False)
    if not _is_allowed(path):
        roots = ", ".join(str(root) for root in allowed_roots)
        print(
            f"Error: Path escapes Docker workspace roots: "
            f"{display_path} (allowed: {roots})"
        )
        raise SystemExit(0)
    return path


cwd = Path.cwd().resolve()
if not _is_allowed(cwd):
    roots = ", ".join(str(root) for root in allowed_roots)
    print(
        f"Error: cwd escapes Docker workspace roots: "
        f"{cwd} (allowed: {roots})"
    )
    raise SystemExit(0)
""".strip()

_BASH_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
import subprocess

completed = subprocess.run(["bash", "-c", sys.argv[2]], check=False)
raise SystemExit(completed.returncode)
""".strip()

_READ_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
path = _confined_path(sys.argv[2], sys.argv[2])
if not path.exists():
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

_WRITE_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
path = _confined_path(sys.argv[2], sys.argv[3])
try:
    content = sys.stdin.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"Written {len(content)} chars to {sys.argv[3]}")
except Exception as exc:
    print(f"Error: {exc}")
""".strip()

_EDIT_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
display_path, old_text, new_text = sys.argv[3:6]
path = _confined_path(sys.argv[2], display_path)
if not path.exists():
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

_GLOB_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
import glob

matches = []
for raw_match in sorted(glob.glob(sys.argv[2], recursive=True)):
    match = Path(raw_match)
    _confined_path(raw_match, raw_match)
    try:
        matches.append(str(match.relative_to(cwd)))
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

_GREP_SCRIPT = _CONFINEMENT_SCRIPT + "\n" + r"""
import re

pattern, raw_root, file_pattern = sys.argv[2:5]
try:
    regex = re.compile(pattern)
except re.error as exc:
    print(f"Error: Invalid regex: {exc}")
    raise SystemExit(0)
root = _confined_path(raw_root, raw_root)
if not root.exists():
    print(f"Error: Path not found: {raw_root}")
else:
    results = []
    files = [root] if root.is_file() else root.rglob(file_pattern or "*")
    for candidate in files:
        resolved = candidate.resolve(strict=False)
        if not _is_allowed(resolved):
            continue
        if not candidate.is_file():
            continue
        try:
            for line_number, line in enumerate(
                candidate.read_text(errors="replace").splitlines(), 1
            ):
                if regex.search(line):
                    try:
                        relative = candidate.relative_to(cwd)
                    except ValueError:
                        relative = candidate
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
    """Run coding tools in allowed roots of a persistent Docker container."""

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
    def _is_allowed(path: PurePosixPath) -> bool:
        return any(
            path == root or root in path.parents
            for root in ALLOWED_WORKSPACE_ROOTS
        )

    @classmethod
    def _validated_path(
        cls,
        path: str,
        *,
        base: PurePosixPath,
        label: str,
    ) -> str:
        raw = PurePosixPath(path)
        if ".." in raw.parts:
            raise ValueError(
                f"{label} escapes Docker workspace roots: {path}"
            )
        candidate = raw if raw.is_absolute() else base / raw
        normalized = PurePosixPath(posixpath.normpath(str(candidate)))
        if not cls._is_allowed(normalized):
            raise ValueError(
                f"{label} escapes Docker workspace roots: {path}"
            )
        return str(normalized)

    @classmethod
    def _cwd(cls, cwd: str | None) -> str:
        if cwd is None:
            return str(WORKSPACE_ROOT)
        return cls._validated_path(
            cwd,
            base=WORKSPACE_ROOT,
            label="cwd",
        )

    @classmethod
    def _path(cls, path: str, *, cwd: str | None = None) -> str:
        effective_cwd = PurePosixPath(cls._cwd(cwd))
        return cls._validated_path(path, base=effective_cwd, label="Path")

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
        cwd: str,
        input_text: str | None = None,
    ) -> str:
        argv = [
            "docker",
            "exec",
            "-e",
            "BASH_ENV=/root/.bashrc",
            "-w",
            cwd,
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
        cwd: str,
        input_text: str | None = None,
    ) -> str:
        return self._exec(
            [
                "bash",
                "-c",
                SHELL_LAUNCHER,
                "_",
                script,
                json.dumps([str(root) for root in ALLOWED_WORKSPACE_ROOTS]),
                *arguments,
            ],
            operation=operation,
            cwd=cwd,
            input_text=input_text,
        )

    def bash(
        self,
        command: str,
        run_in_background: bool = False,
        cwd: str | None = None,
    ) -> str:
        if run_in_background:
            return "Error: Docker background execution is not supported"
        try:
            effective_cwd = self._cwd(cwd)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _BASH_SCRIPT,
            [command],
            operation="bash command",
            cwd=effective_cwd,
        )

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cwd: str | None = None,
    ) -> str:
        try:
            effective_cwd = self._cwd(cwd)
            target = self._path(path, cwd=effective_cwd)
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
            cwd=effective_cwd,
        )

    def write_file(
        self,
        path: str,
        content: str,
        cwd: str | None = None,
    ) -> str:
        try:
            effective_cwd = self._cwd(cwd)
            target = self._path(path, cwd=effective_cwd)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _WRITE_SCRIPT,
            [target, path],
            operation="write_file",
            cwd=effective_cwd,
            input_text=content,
        )

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        cwd: str | None = None,
    ) -> str:
        try:
            effective_cwd = self._cwd(cwd)
            target = self._path(path, cwd=effective_cwd)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _EDIT_SCRIPT,
            [target, path, old_text, new_text],
            operation="edit_file",
            cwd=effective_cwd,
        )

    def glob(self, pattern: str, cwd: str | None = None) -> str:
        try:
            effective_cwd = self._cwd(cwd)
            mapped_pattern = self._path(pattern, cwd=effective_cwd)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _GLOB_SCRIPT,
            [mapped_pattern],
            operation="glob",
            cwd=effective_cwd,
        )

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        cwd: str | None = None,
    ) -> str:
        try:
            effective_cwd = self._cwd(cwd)
            target = self._path(path, cwd=effective_cwd)
            safe_file_pattern = self._validate_file_pattern(file_pattern)
        except ValueError as exc:
            return f"Error: {exc}"
        return self._helper(
            _GREP_SCRIPT,
            [pattern, target, safe_file_pattern or ""],
            operation="grep",
            cwd=effective_cwd,
        )
