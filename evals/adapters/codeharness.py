"""Thin conversion from an EvalCase to one CodeHarness run."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codeharness.app import CodeHarness
from codeharness.config import DEFAULT_MODEL_CONTEXT_WINDOW, RuntimeConfig
from evals.runner.case import EvalCase


@dataclass(frozen=True)
class AdapterResult:
    final_answer: str
    session_stats: dict[str, int]


class CodeHarnessAdapter:
    """Build and run a case-scoped CodeHarness Runtime."""

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        harness_factory: Callable[[RuntimeConfig], Any] = CodeHarness,
    ):
        self.environment = environment if environment is not None else os.environ
        self.harness_factory = harness_factory

    def build_config(
        self,
        workspace: str | Path,
        agent_home: str | Path,
    ) -> RuntimeConfig:
        env = self.environment
        resolved_agent_home = Path(agent_home).expanduser().resolve()
        mcp_path = resolved_agent_home / "mcp" / "servers.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text("{}\n", encoding="utf-8")
        evaluator_model = env.get("GOAL_EVALUATOR_MODEL") or None
        return RuntimeConfig(
            api_key=env["DEEPSEEK_API_KEY"],
            base_url=env["DEEPSEEK_BASE_URL"],
            model=env["DEEPSEEK_MODEL_ID"],
            model_context_window=int(
                env.get("MODEL_CONTEXT_WINDOW", str(DEFAULT_MODEL_CONTEXT_WINDOW))
            ),
            workspace=Path(workspace).expanduser().resolve(),
            mcp_config_path=mcp_path,
            agent_home=resolved_agent_home,
            evaluator_model=evaluator_model,
        )

    def run(
        self,
        case: EvalCase,
        workspace: str | Path,
        agent_home: str | Path,
    ) -> AdapterResult:
        harness = self.harness_factory(
            self.build_config(workspace=workspace, agent_home=agent_home)
        )
        try:
            harness.start()
            if case.goal is not None:
                harness.goal_controller.set_goal(case.goal)
            final_answer = harness.run(case.task)
            stats = {
                name: int(harness.session_stats.get(name, 0))
                for name in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "tool_calls",
                )
            }
            return AdapterResult(
                final_answer=final_answer,
                session_stats=stats,
            )
        finally:
            harness.close()
