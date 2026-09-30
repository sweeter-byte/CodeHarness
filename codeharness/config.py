"""Environment-backed configuration for the CodeHarness runtime."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL_CONTEXT_WINDOW = 1_048_576
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str
    base_url: str
    model: str
    model_context_window: int
    workspace: Path
    mcp_config_path: Path

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        load_dotenv(override=True)
        workspace = Path(os.environ.get("WORKSPACE", PROJECT_ROOT)).expanduser().resolve()
        os.environ.setdefault("WORKSPACE", str(workspace))
        mcp_config_path = Path(
            os.environ.get("MCP_CONFIG_PATH", PROJECT_ROOT / "mcp_servers.json")
        ).expanduser().resolve()
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
        )
