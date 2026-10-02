"""One-process-per-case CodeHarness worker."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from evals.adapters.codeharness import CodeHarnessAdapter
from evals.runner.case import load_case


ZERO_STATS = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "tool_calls": 0,
}


def redact_text(value: str, environment: Mapping[str, str]) -> str:
    """Remove known secret values before persistence."""
    secret = environment.get("DEEPSEEK_API_KEY", "")
    if secret:
        return value.replace(secret, "***REDACTED***")
    return value


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_worker(
    case_path: str | Path,
    workspace: str | Path,
    result_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
    adapter_factory: Callable[..., Any] = CodeHarnessAdapter,
) -> int:
    """Run one case and always attempt to write the worker result."""
    env = environment if environment is not None else os.environ
    output_path = Path(result_path)
    try:
        case = load_case(case_path)
        agent_home = Path(
            env.get("CODEHARNESS_HOME", str(Path(workspace).parent / "agent-home"))
        )
        result = adapter_factory(env).run(case, Path(workspace), agent_home)
        payload = {
            "final_answer": redact_text(result.final_answer, env),
            "session_stats": result.session_stats,
            "worker_status": "completed",
            "error": None,
        }
        _write_result(output_path, payload)
        return 0
    except Exception as exc:
        error = redact_text(f"{type(exc).__name__}: {exc}", env)
        _write_result(
            output_path,
            {
                "final_answer": "",
                "session_stats": dict(ZERO_STATS),
                "worker_status": "error",
                "error": error,
            },
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker(args.case, args.workspace, args.result)


if __name__ == "__main__":
    raise SystemExit(main())
