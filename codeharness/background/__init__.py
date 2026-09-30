"""Background task management (long-running shell commands)."""

from .manager import BackgroundManager, should_run_background

__all__ = ["BackgroundManager", "should_run_background"]
