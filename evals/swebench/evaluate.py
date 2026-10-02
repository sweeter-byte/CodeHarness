"""Optional invocation of the official SWE-bench Docker evaluator."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.swebench.dataset import DEPENDENCY_ERROR, SWEbenchDependencyError


class OfficialEvaluationError(RuntimeError):
    """Raised when official evaluator preflight or execution fails."""


def build_evaluator_command(
    *,
    dataset_name: str,
    split: str,
    predictions_path: str | Path,
    instance_id: str,
    run_id: str,
    python_executable: str = sys.executable,
) -> list[str]:
    return [
        python_executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--split",
        split,
        "--predictions_path",
        str(predictions_path),
        "--instance_ids",
        instance_id,
        "--max_workers",
        "1",
        "--run_id",
        run_id,
    ]


def preflight_evaluator(
    *,
    module_finder: Callable[[str], Any] = importlib.util.find_spec,
    executable_finder: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if module_finder("swebench") is None:
        raise SWEbenchDependencyError(DEPENDENCY_ERROR)
    docker = executable_finder("docker")
    if docker is None:
        raise OfficialEvaluationError("Docker executable is not available.")
    try:
        completed = command_runner(
            [docker, "info"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfficialEvaluationError(
            f"Docker daemon is not accessible: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise OfficialEvaluationError(f"Docker daemon is not accessible: {detail}")
    return docker


def run_official_evaluation(
    *,
    dataset_name: str,
    split: str,
    predictions_path: str | Path,
    instance_id: str,
    run_id: str,
) -> dict[str, Any]:
    preflight_evaluator()
    command = build_evaluator_command(
        dataset_name=dataset_name,
        split=split,
        predictions_path=predictions_path,
        instance_id=instance_id,
        run_id=run_id,
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise OfficialEvaluationError(
            f"official SWE-bench evaluator failed to start: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise OfficialEvaluationError(f"official SWE-bench evaluator failed: {detail}")
    return {
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
