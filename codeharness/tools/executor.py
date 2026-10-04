"""Shared Harness boundary for executing one resolved tool handler.

This module deliberately knows nothing about Agent messages or Workflow
steps.  Callers resolve a handler, provide execution policy, and translate
the structured result into their own protocol.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from codeharness import hooks

HookRunner = Callable[..., Any]
ToolHandler = Callable[..., Any]
ApprovalHandler = Callable[[str], bool]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Outcome from the shared tool execution boundary."""

    executed: bool
    output: Any
    status: str
    reason: str | None = None


class ToolExecutor:
    """Run PreToolUse → approval → statistics → handler → PostToolUse."""

    def __init__(self, hook_runner: HookRunner = hooks.trigger_hooks):
        self._trigger_hooks = hook_runner

    def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        handler: ToolHandler,
        interactive: bool = True,
        approval_handler: ApprovalHandler | None = None,
        approval_context: str = "teammates",
        session_stats: MutableMapping[str, int] | None = None,
    ) -> ToolExecutionResult:
        """Execute a resolved handler subject to Harness hooks and approval."""
        hooks.PENDING_USER_ASK.value = None
        hook_result = self._trigger_hooks("PreToolUse", tool_name, args)

        if hook_result is not None:
            return ToolExecutionResult(False, hook_result, "blocked")

        if hooks.PENDING_USER_ASK.value is not None:
            reason = hooks.PENDING_USER_ASK.value
            hooks.PENDING_USER_ASK.value = None

            if not interactive:
                if approval_context == "teammates":
                    output = (
                        "Error: This operation requires user approval, which is "
                        f"not available for teammates - {reason}. Do NOT retry. "
                        "Report the blocker to your leader via send_message, or "
                        "adjust your approach to avoid this operation."
                    )
                else:
                    output = (
                        "Error: This operation requires user approval, which is "
                        f"not available for {approval_context} background "
                        f"execution - {reason}. Operation rejected."
                    )
                return ToolExecutionResult(
                    False, output, "approval_unavailable", reason
                )

            if approval_handler is None:
                output = (
                    "Error: This operation requires user approval, but no "
                    "approval handler is configured. Operation rejected."
                )
                return ToolExecutionResult(
                    False, output, "approval_unavailable", reason
                )

            if not approval_handler(reason):
                output = (
                    "Error: User rejected this operation. "
                    "Do NOT retry via alternative commands or paths. "
                    "If you cannot complete the task without this operation, "
                    "report what you have done and stop."
                )
                return ToolExecutionResult(False, output, "rejected", reason)

        if session_stats is not None:
            session_stats["tool_calls"] += 1
        try:
            output = handler(**args)
        except Exception as exc:
            output = (
                f"Error: Tool '{tool_name}' failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return ToolExecutionResult(False, output, "error", str(exc))

        self._trigger_hooks("PostToolUse", tool_name, args, output)
        return ToolExecutionResult(True, output, "executed")
