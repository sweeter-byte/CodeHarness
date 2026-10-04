"""Execution boundary for coding-tool workspaces."""

from __future__ import annotations

from typing import Protocol

from codeharness.background.manager import BackgroundManager


class WorkspaceBackend(Protocol):
    """Narrow execution interface used by the six coding tools."""

    def bash(
        self,
        command: str,
        run_in_background: bool = False,
        cwd: str | None = None,
    ) -> str: ...

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cwd: str | None = None,
    ) -> str: ...

    def write_file(
        self,
        path: str,
        content: str,
        cwd: str | None = None,
    ) -> str: ...

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        cwd: str | None = None,
    ) -> str: ...

    def glob(self, pattern: str, cwd: str | None = None) -> str: ...

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        cwd: str | None = None,
    ) -> str: ...


class LocalWorkspaceBackend:
    """Preserve the existing host execution semantics behind the boundary."""

    def __init__(
        self,
        background_manager: BackgroundManager | None = None,
        default_cwd: str | None = None,
    ) -> None:
        self.background_manager = (
            background_manager
            if background_manager is not None
            else BackgroundManager()
        )
        self.default_cwd = default_cwd

    def _cwd(self, cwd: str | None) -> str | None:
        return cwd if cwd is not None else self.default_cwd

    def bash(
        self,
        command: str,
        run_in_background: bool = False,
        cwd: str | None = None,
    ) -> str:
        effective_cwd = self._cwd(cwd)
        if run_in_background:
            bg_id, error = self.background_manager.start(command, cwd=effective_cwd)
            if bg_id is not None:
                return f"[Background task {bg_id} started: {command}]"
            return error

        from codeharness.tools.coding import run_bash

        return run_bash(command, cwd=effective_cwd)

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cwd: str | None = None,
    ) -> str:
        from codeharness.tools.coding import run_read

        return run_read(path, start_line, end_line, cwd=self._cwd(cwd))

    def write_file(
        self,
        path: str,
        content: str,
        cwd: str | None = None,
    ) -> str:
        from codeharness.tools.coding import run_write

        return run_write(path, content, cwd=self._cwd(cwd))

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        cwd: str | None = None,
    ) -> str:
        from codeharness.tools.coding import run_edit

        return run_edit(path, old_text, new_text, cwd=self._cwd(cwd))

    def glob(self, pattern: str, cwd: str | None = None) -> str:
        from codeharness.tools.coding import run_glob

        return run_glob(pattern, cwd=self._cwd(cwd))

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        cwd: str | None = None,
    ) -> str:
        from codeharness.tools.coding import run_grep

        return run_grep(pattern, path, file_pattern, cwd=self._cwd(cwd))
