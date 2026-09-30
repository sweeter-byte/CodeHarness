"""Background task management (long-running shell commands)."""

from .manager import BACKGROUND, BackgroundManager, should_run_background

__all__ = ["BACKGROUND", "BackgroundManager", "should_run_background"]
