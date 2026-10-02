"""Environment-backed configuration for the CodeHarness runtime."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL_CONTEXT_WINDOW = 1_048_576
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_agent_home() -> Path:
    return Path(
        os.environ.get("CODEHARNESS_HOME", "~/.codeharness")
    ).expanduser().resolve()


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str
    base_url: str
    model: str
    model_context_window: int
    workspace: Path
    mcp_config_path: Path
    agent_home: Path = field(default_factory=_default_agent_home)
    evaluator_model: str | None = None  # Goal Evaluator model (defaults to main model)

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        load_dotenv(override=True)
        workspace = Path(os.environ.get("WORKSPACE", PROJECT_ROOT)).expanduser().resolve()
        os.environ.setdefault("WORKSPACE", str(workspace))
        agent_home = _default_agent_home()
        mcp_config_path = (
            Path(os.environ["MCP_CONFIG_PATH"]).expanduser().resolve()
            if "MCP_CONFIG_PATH" in os.environ
            else agent_home / "mcp" / "servers.json"
        )
        return cls(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
            model=os.environ["DEEPSEEK_MODEL_ID"],
            model_context_window=int(
                os.environ.get(
                    "MODEL_CONTEXT_WINDOW", str(DEFAULT_MODEL_CONTEXT_WINDOW)
                )
            ),
            workspace=workspace,
            mcp_config_path=mcp_config_path,
            agent_home=agent_home,
            evaluator_model=os.environ.get("GOAL_EVALUATOR_MODEL") or None,
        )
