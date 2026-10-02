"""Serial parent-process evaluation runner."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evals.metrics.aggregate import aggregate_results
from evals.runner.case import EvalCase, discover_cases
from evals.runner.verifier import capture_patch, run_verifier
from evals.runner.workspace import WorkspaceManager

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPOSITORY_ROOT / "eval_results"
TERMINATE_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class WorkerProcessResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float


def _redact_text(value: str, environment: Mapping[str, str]) -> str:
    secret = environment.get("DEEPSEEK_API_KEY", "")
    return value.replace(secret, "***REDACTED***") if secret else value


def _redact_value(value: Any, environment: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, environment)
    if isinstance(value, dict):
        return {key: _redact_value(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, environment) for item in value]
    return value


def write_json(
    path: str | Path,
    payload: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
) -> None:
    env = environment if environment is not None else os.environ
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_redact_value(dict(payload), env), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _write_log(path: Path, value: str, environment: Mapping[str, str]) -> None:
    path.write_text(_redact_text(value, environment), encoding="utf-8")


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_worker_process(
    command: Sequence[str],
    *,
    workspace: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> WorkerProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=workspace,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
    return WorkerProcessResult(
        return_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
    )


def _worker_environment(
    workspace: Path,
    agent_home: Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    child_env = dict(environment)
    old_pythonpath = child_env.get("PYTHONPATH")
    source_path = str(REPOSITORY_ROOT)
    child_env["PYTHONPATH"] = (
        os.pathsep.join((source_path, old_pythonpath))
        if old_pythonpath
        else source_path
    )
    child_env["WORKSPACE"] = str(workspace)
    child_env["CODEHARNESS_HOME"] = str(agent_home)
    return child_env


def _load_worker_payload(
    path: Path,
    process: WorkerProcessResult,
) -> dict[str, Any]:
    if process.timed_out:
        return {
            "worker_status": "timeout",
            "error": "worker exceeded wall-clock timeout",
            "final_answer": "",
            "session_stats": {},
        }
    if not path.is_file():
        return {
            "worker_status": "crashed",
            "error": f"worker exited with code {process.return_code}",
            "final_answer": "",
            "session_stats": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "worker_status": "crashed",
            "error": f"invalid worker result: {exc}",
            "final_answer": "",
            "session_stats": {},
        }
    if not isinstance(payload, dict):
        return {
            "worker_status": "crashed",
            "error": "invalid worker result: expected JSON object",
            "final_answer": "",
            "session_stats": {},
        }
    if process.return_code != 0 and payload.get("worker_status") == "completed":
        payload["worker_status"] = "crashed"
        payload["error"] = f"worker exited with code {process.return_code}"
    return payload


def run_case(
    case: EvalCase,
    *,
    run_id: str,
    workspace_manager: WorkspaceManager,
    result_root: str | Path,
    environment: Mapping[str, str] | None = None,
    worker_command_factory: Callable[[Path], Sequence[str]] | None = None,
) -> dict[str, Any]:
    env = dict(os.environ if environment is None else environment)
    prepared = workspace_manager.prepare(case)
    case_result_dir = Path(result_root) / "cases" / case.case_id
    case_result_dir.mkdir(parents=True, exist_ok=True)
    worker_result_path = prepared.case_root / "worker-result.json"
    command = (
        list(worker_command_factory(worker_result_path))
        if worker_command_factory is not None
        else [
            sys.executable,
            "-m",
            "evals.worker",
            "--case",
            str(case.source_path),
            "--workspace",
            str(prepared.workspace),
            "--result",
            str(worker_result_path),
        ]
    )
    child_env = _worker_environment(prepared.workspace, prepared.agent_home, env)

    started = time.monotonic()
    process = run_worker_process(
        command,
        workspace=prepared.workspace,
        environment=child_env,
        timeout_seconds=case.timeout_seconds,
    )
    _write_log(case_result_dir / "agent.stdout.log", process.stdout, env)
    _write_log(case_result_dir / "agent.stderr.log", process.stderr, env)

    worker_payload = _load_worker_payload(worker_result_path, process)
    verifier = run_verifier(
        case.verification.command,
        workspace=prepared.workspace,
        timeout_seconds=case.timeout_seconds,
    )
    _write_log(case_result_dir / "verifier.stdout.log", verifier.stdout, env)
    _write_log(case_result_dir / "verifier.stderr.log", verifier.stderr, env)

    patch, patch_error = capture_patch(prepared.workspace)
    _write_log(case_result_dir / "patch.diff", patch, env)
    stats = worker_payload.get("session_stats")
    if not isinstance(stats, dict):
        stats = {}
    worker_status = str(worker_payload.get("worker_status", "crashed"))
    success = (
        not process.timed_out
        and process.return_code == 0
        and worker_status == "completed"
        and verifier.passed
    )
    result = {
        "run_id": run_id,
        "case_id": case.case_id,
        "category": case.category,
        "success": success,
        "timeout": process.timed_out,
        "worker_status": worker_status,
        "worker_error": worker_payload.get("error"),
        "worker_return_code": process.return_code,
        "verifier_exit_code": verifier.exit_code,
        "verifier_timed_out": verifier.timed_out,
        "wall_time_seconds": time.monotonic() - started,
        "prompt_tokens": int(stats.get("prompt_tokens", 0)),
        "completion_tokens": int(stats.get("completion_tokens", 0)),
        "total_tokens": int(stats.get("total_tokens", 0)),
        "tool_calls": int(stats.get("tool_calls", 0)),
        "final_answer": str(worker_payload.get("final_answer", "")),
        "workspace_path": str(prepared.workspace),
        "patch_error": patch_error,
    }
    result = _redact_value(result, env)
    write_json(case_result_dir / "result.json", result, env)
    return result


def _git_commit(repository_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def build_run_config(
    *,
    run_id: str,
    suite: str,
    case_ids: Sequence[str],
    repository_root: Path = REPOSITORY_ROOT,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = environment if environment is not None else os.environ
    parsed = urlsplit(env.get("DEEPSEEK_BASE_URL", ""))
    return {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(repository_root),
        "model": env.get("DEEPSEEK_MODEL_ID"),
        "base_url": parsed.netloc or parsed.path or None,
        "suite": suite,
        "case_ids": list(case_ids),
    }


def make_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def print_summary(summary: Mapping[str, Any]) -> None:
    print("CodeHarness Evaluation")
    print("----------------------")
    print(f"Cases:       {summary['total_cases']}")
    print(f"Passed:      {summary['passed_cases']}")
    print(f"Failed:      {summary['failed_cases']}")
    print(f"Success:     {summary['success_rate'] * 100:.1f}%")
    print(f"Avg Tokens:  {summary['average_tokens']:.1f}")
    print(f"Avg Tools:   {summary['average_tool_calls']:.1f}")
    print(f"Avg Time:    {summary['average_wall_time_seconds']:.2f}s")


def run_evaluation(
    *,
    suite: str,
    case_id: str | None = None,
    results_root: str | Path = RESULTS_ROOT,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    env = dict(os.environ if environment is None else environment)
    cases = discover_cases(
        REPOSITORY_ROOT / "evals" / "cases" / suite,
        case_id=case_id,
    )
    run_id = make_run_id()
    run_root = Path(results_root) / run_id
    run_root.mkdir(parents=True)
    write_json(
        run_root / "config.json",
        build_run_config(
            run_id=run_id,
            suite=suite,
            case_ids=[case.case_id for case in cases],
            environment=env,
        ),
        env,
    )
    manager = WorkspaceManager(run_id=run_id, repository_root=REPOSITORY_ROOT)
    results = [
        run_case(
            case,
            run_id=run_id,
            workspace_manager=manager,
            result_root=run_root,
            environment=env,
        )
        for case in cases
    ]
    summary = aggregate_results(results)
    write_json(run_root / "summary.json", summary, env)
    print_summary(summary)
    return run_root, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="smoke")
    parser.add_argument("--case")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_evaluation(suite=args.suite, case_id=args.case)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
