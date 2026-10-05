"""Run CodeHarness against one SWE-bench instance and write a prediction."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.runner.runner import (
    RESULTS_ROOT,
    EvaluationConfigurationError,
    WorkerProcessResult,
    _load_worker_payload,
    _redact_text,
    _validate_evaluation_environment,
    _worker_environment,
    _write_log,
    load_evaluation_environment,
    make_run_id,
    run_worker_process,
    write_json,
)
from evals.swebench.container_patch import (
    ContainerPatchCollector,
    ContainerPatchError,
)
from evals.swebench.dataset import (
    DEFAULT_DATASET_NAME,
    DEFAULT_SPLIT,
    DatasetLoader,
    SWEbenchDependencyError,
    load_instance,
)
from evals.swebench.evaluate import (
    OfficialEvaluationError,
    run_official_evaluation,
)
from evals.swebench.prediction import build_prediction, write_prediction
from evals.swebench.rollout_container import (
    RolloutContainerError,
    SWEbenchRolloutContainer,
)
from evals.swebench.workspace import (
    SWEbenchRuntimeWorkspaceManager,
    WorkspacePreparationError,
)

DEFAULT_TIMEOUT_SECONDS = 1_800


def _build_config(
    *,
    run_id: str,
    instance_id: str,
    dataset_name: str,
    split: str,
    timeout_seconds: int,
    evaluate: bool,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instance_id": instance_id,
        "dataset_name": dataset_name,
        "split": split,
        "model": environment.get("DEEPSEEK_MODEL_ID"),
        "timeout_seconds": timeout_seconds,
        "evaluate": evaluate,
    }


def _instance_metadata(instance: Any) -> dict[str, Any]:
    """Return only public rollout inputs and reproducibility metadata."""
    return {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "problem_statement": instance.problem_statement,
        "image": instance.raw.get("image"),
    }


def _rollout_metadata(info: Any) -> dict[str, str]:
    """Serialize the non-hidden startup facts for one rollout container."""
    return {
        "image": info.image,
        "image_id": info.image_id,
        "container_name": info.container_name,
        "container_id": info.container_id,
        "initial_head": info.initial_head,
        "initial_status": info.initial_status,
        "python_path": info.python_path,
        "python_version": info.python_version,
    }


def run_single_instance(
    *,
    instance_id: str,
    dataset_name: str = DEFAULT_DATASET_NAME,
    split: str = DEFAULT_SPLIT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    evaluate: bool = False,
    results_root: str | Path = RESULTS_ROOT,
    work_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    run_id: str | None = None,
    dataset_loader: DatasetLoader | None = None,
    workspace_manager: Any | None = None,
    rollout_container_factory: Callable[..., Any] = SWEbenchRolloutContainer,
    patch_collector_factory: Callable[[str], Any] = ContainerPatchCollector,
    worker_process_runner: Callable[..., WorkerProcessResult] = run_worker_process,
    official_evaluator: Callable[..., dict[str, Any]] = run_official_evaluation,
) -> tuple[Path, dict[str, Any]]:
    """Run one instance; external-boundary arguments keep unit tests offline."""
    if timeout_seconds <= 0:
        raise ValueError("timeout must be a positive integer")
    env = load_evaluation_environment() if environment is None else dict(environment)
    _validate_evaluation_environment(env)
    instance = load_instance(
        instance_id,
        dataset_name=dataset_name,
        split=split,
        dataset_loader=dataset_loader,
    )
    resolved_run_id = run_id or make_run_id()
    run_root = Path(results_root).expanduser().resolve() / "swebench" / resolved_run_id
    run_root.mkdir(parents=True)
    write_json(
        run_root / "config.json",
        _build_config(
            run_id=resolved_run_id,
            instance_id=instance.instance_id,
            dataset_name=dataset_name,
            split=split,
            timeout_seconds=timeout_seconds,
            evaluate=evaluate,
            environment=env,
        ),
        env,
    )
    write_json(run_root / "instance.json", _instance_metadata(instance), env)

    manager = workspace_manager or SWEbenchRuntimeWorkspaceManager(
        resolved_run_id,
        work_root=work_root,
    )
    prepared = manager.prepare(instance)
    worker_result_path = prepared.case_root / "worker-result.json"
    child_env = _worker_environment(prepared.workspace, prepared.agent_home, env)

    started = time.monotonic()
    rollout = rollout_container_factory(instance, resolved_run_id)
    collector = None
    try:
        info = rollout.start()
        write_json(run_root / "rollout.json", _rollout_metadata(info), env)
        collector = patch_collector_factory(info.container_id)
        collector.snapshot_baseline()
        command = [
            sys.executable,
            "-m",
            "evals.swebench.worker",
            "--problem-statement",
            instance.problem_statement,
            "--workspace",
            str(prepared.workspace),
            "--agent-home",
            str(prepared.agent_home),
            "--container-id",
            info.container_id,
            "--result",
            str(worker_result_path),
        ]
        process = worker_process_runner(
            command,
            workspace=prepared.workspace,
            environment=child_env,
            timeout_seconds=timeout_seconds,
        )
        _write_log(run_root / "agent.stdout.log", process.stdout, env)
        _write_log(run_root / "agent.stderr.log", process.stderr, env)
        worker_payload = _load_worker_payload(worker_result_path, process)
        patch, patch_error = collector.capture_patch()
    finally:
        try:
            if collector is not None:
                collector.close()
        finally:
            rollout.close()

    safe_patch = _redact_text(patch, env)
    _write_log(run_root / "patch.diff", safe_patch, env)
    prediction = build_prediction(
        instance.instance_id,
        env["DEEPSEEK_MODEL_ID"],
        safe_patch,
    )
    prediction_path = run_root / "prediction.jsonl"
    write_prediction(prediction_path, prediction)
    write_prediction(prepared.case_root / "prediction.jsonl", prediction)

    official_evaluation = None
    if evaluate:
        official_evaluation = official_evaluator(
            dataset_name=dataset_name,
            split=split,
            predictions_path=prediction_path,
            instance_id=instance.instance_id,
            run_id=resolved_run_id,
        )

    stats = worker_payload.get("session_stats")
    if not isinstance(stats, dict):
        stats = {}
    result = {
        "run_id": resolved_run_id,
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "model": env["DEEPSEEK_MODEL_ID"],
        "worker_status": str(worker_payload.get("worker_status", "crashed")),
        "worker_error": worker_payload.get("error"),
        "worker_return_code": process.return_code,
        "timeout": process.timed_out,
        "wall_time_seconds": time.monotonic() - started,
        "prompt_tokens": int(stats.get("prompt_tokens", 0)),
        "completion_tokens": int(stats.get("completion_tokens", 0)),
        "total_tokens": int(stats.get("total_tokens", 0)),
        "tool_calls": int(stats.get("tool_calls", 0)),
        "empty_patch": not bool(safe_patch),
        "patch_error": patch_error,
        "prediction_path": str(prediction_path),
        "workspace_path": str(prepared.workspace),
        "official_evaluation": official_evaluation,
    }
    write_json(run_root / "result.json", result, env)
    return run_root, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--evaluate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_root, _ = run_single_instance(
            instance_id=args.instance_id,
            dataset_name=args.dataset_name,
            split=args.split,
            timeout_seconds=args.timeout,
            evaluate=args.evaluate,
        )
    except (
        EvaluationConfigurationError,
        SWEbenchDependencyError,
        OfficialEvaluationError,
        ContainerPatchError,
        RolloutContainerError,
        WorkspacePreparationError,
        FileExistsError,
        LookupError,
        ValueError,
    ) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(run_root / "prediction.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
