"""Independent verification and patch collection."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence


@dataclass(frozen=True)
class VerifierResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def run_verifier(
    command: Sequence[str],
    workspace: str | Path,
    timeout_seconds: int,
) -> VerifierResult:
    """Run a case verifier directly, never through the agent."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=Path(workspace),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
        return VerifierResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return VerifierResult(
            exit_code=None,
            stdout=_timeout_text(exc.stdout),
            stderr=_timeout_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return VerifierResult(
            exit_code=None,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def capture_patch(workspace: str | Path) -> tuple[str, str | None]:
    """Return the binary-capable diff, preserving failures as metadata."""
    try:
        completed = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
            cwd=Path(workspace),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if completed.returncode != 0:
        return "", completed.stderr.strip() or "git diff failed"
    return completed.stdout, None
