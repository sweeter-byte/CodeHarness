"""One-process worker for a single SWE-bench problem statement."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from evals.adapters.codeharness import AdapterResult, CodeHarnessAdapter
from evals.runner.runner import _redact_text
from evals.swebench.docker_workspace import DockerWorkspaceBackend
from evals.swebench.docker_worktree import DockerWorktreeEnvironment

TASK_PREFIX = """You are working on a SWE-bench repository checkout.
Resolve the issue described below.
Inspect the repository, make the necessary code changes, and validate your work."""

ZERO_STATS = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "tool_calls": 0,
}


class SWEbenchCodeHarnessAdapter(CodeHarnessAdapter):
    """Run a SWE-bench task in host mode or one borrowed container."""

    def run_task(
        self,
        task: str,
        workspace: str | Path,
        agent_home: str | Path,
        container_id: str | None = None,
    ) -> AdapterResult:
        if container_id is not None and not container_id.strip():
            raise ValueError("container_id must be a non-empty Docker name or ID")
        config = self.build_config(workspace=workspace, agent_home=agent_home)
        if container_id is None:
            harness = self.harness_factory(config)
        else:
            workspace_backend = DockerWorkspaceBackend(container_id)
            worktree_environment = DockerWorktreeEnvironment(container_id)
            harness = self.harness_factory(
                config,
                workspace_backend=workspace_backend,
                tool_workspace="/testbed",
                worktree_environment=worktree_environment,
                tool_worktrees="/tmp/codeharness-worktrees",
            )
        try:
            harness.start()
            final_answer = harness.run(task)
            stats = {
                name: int(harness.session_stats.get(name, 0)) for name in ZERO_STATS
            }
            return AdapterResult(
                final_answer=final_answer,
                session_stats=stats,
            )
        finally:
            harness.close()


def _write_result(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_worker(
    problem_statement: str,
    workspace: str | Path,
    result_path: str | Path,
    *,
    agent_home: str | Path,
    container_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    adapter_factory: Callable[..., Any] = SWEbenchCodeHarnessAdapter,
) -> int:
    """Run one task; ``workspace`` always stores host-side Runtime state."""
    env = environment if environment is not None else os.environ
    output_path = Path(result_path)
    try:
        task = f"{TASK_PREFIX}\n\n{problem_statement}"
        task_adapter = adapter_factory(env)
        task_args = (
            task,
            Path(workspace).expanduser().resolve(),
            Path(agent_home).expanduser().resolve(),
        )
        if container_id is None:
            result = task_adapter.run_task(*task_args)
        else:
            result = task_adapter.run_task(*task_args, container_id=container_id)
        _write_result(
            output_path,
            {
                "final_answer": _redact_text(result.final_answer, env),
                "session_stats": result.session_stats,
                "worker_status": "completed",
                "error": None,
            },
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - worker must persist all failures
        _write_result(
            output_path,
            {
                "final_answer": "",
                "session_stats": dict(ZERO_STATS),
                "worker_status": "error",
                "error": _redact_text(f"{type(exc).__name__}: {exc}", env),
            },
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problem-statement", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--agent-home", required=True, type=Path)
    parser.add_argument(
        "--container-id",
        help=(
            "borrowed Docker container ID or name for coding execution; "
            "--workspace remains the host Runtime-state directory"
        ),
    )
    parser.add_argument("--result", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker(
        args.problem_statement,
        args.workspace,
        args.result,
        agent_home=args.agent_home,
        container_id=args.container_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
