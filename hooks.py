import os
import re
import threading
from pathlib import Path
from permission import PermissionManager

# ── Hook Registry ─────────────────────────────────────────────

HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """Run all callbacks for event. First non-None return stops the chain."""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── Shared State ──────────────────────────────────────────────

# Token / tool-call counters, accumulated by the agent loop each turn.
SESSION_STATS = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "tool_calls": 0,
}

# Set by permission_hook when the decision is "ask"; read & cleared by the loop.
# Thread-local: the leader and teammate threads run tool calls concurrently;
# a process-global flag would let one thread reset another's pending ask.
class _PendingAskLocal(threading.local):
    def __init__(self):
        self.value = None


PENDING_USER_ASK = _PendingAskLocal()

_perm_manager = PermissionManager()


def configure_mcp_permissions(resolve, annotations_of=None):
    """Inject MCP name-resolution into the shared PermissionManager.

    Keeps permission.py decoupled from MCPManager: only two small callables
    (resolve / annotations_of) are handed over, not the manager object itself.
    Called once at startup after the MCP tool pool has been assembled.
    """
    _perm_manager.set_mcp_provider(resolve, annotations_of)

# ── UserPromptSubmit Hook ─────────────────────────────────────

def context_inject_hook(query: str):
    """Print working-directory context before the prompt enters the LLM."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {os.getcwd()}\033[0m")
    return None


# ── PreToolUse Hooks ─────────────────────────────────────────
# Execution order: privacy mask → permission check → call log

# Patterns that indicate a literal secret value (not just a variable name).
_SENSITIVE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),                       # OpenAI-style key
    re.compile(r"\bghp_[A-Za-z0-9]{36}"),                       # GitHub PAT
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                        # AWS access key ID
    re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----"),
]

# `password=xxx`, `api_key=xxx`, `secret_key=xxx`  (value must be non-empty)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?:password|api_key|apikey|secret_key|token)\s*=\s*['\"]?[^\s'\"]{4,}",
    re.IGNORECASE,
)


_MASK = "***REDACTED***"


def _mask_text(text: str) -> str:
    """Replace all sensitive patterns in text with a mask."""
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(_MASK, text)
    text = _ASSIGNMENT_PATTERN.sub(
        lambda m: m.group(0).split("=")[0] + "=" + _MASK, text
    )
    return text


def privacy_mask_hook(tool_name: str, args: dict):
    """Mask sensitive values in tool arguments (in-place), then allow execution."""
    masked = False
    for key, value in args.items():
        if isinstance(value, str):
            new_value = _mask_text(value)
            if new_value != value:
                args[key] = new_value
                masked = True
    if masked:
        print(f"\033[33m[HOOK] Privacy mask applied to {tool_name} arguments\033[0m")
    return None


def permission_hook(tool_name: str, args: dict):
    """
    Wrap PermissionManager.check().
      deny  → return rejection string (blocks execution)
      ask   → set PENDING_USER_ASK, return None (loop handles the prompt)
      allow → return None
    During cron turns, 'ask' decisions are rejected instead of prompting.
    """
    decision, reason = _perm_manager.check(tool_name, args)

    if decision == "deny":
        return (
            f"Error: Permission denied - {reason}. "
            "Do NOT retry this operation via alternative commands."
        )
    if decision == "ask":
        # During scheduled (cron) turns, reject interactive approvals
        from cron_scheduler import CRON_TURN
        if CRON_TURN:
            return (
                f"Error: Interactive approval not available during scheduled execution - {reason}. "
                "Use non-interactive commands only."
            )
        PENDING_USER_ASK.value = reason
        return None
    return None


def log_hook(tool_name: str, args: dict):
    """Print a one-line summary of the tool invocation."""
    print(f"\033[33m> {tool_name}({args})\033[0m")
    return None


# ── PostToolUse Hook ──────────────────────────────────────────

def output_display_hook(tool_name: str, args: dict, output: str):
    """Show truncated output; warn when the result is unusually large."""
    text = str(output)
    if len(text) > 100_000:
        print(f"\033[33m[HOOK] ⚠ Large output from {tool_name} ({len(text)} chars)\033[0m")
    print(text[:200])
    return None


# ── Stop Hook ─────────────────────────────────────────────────

def session_summary_hook(stats: dict):
    """Print a token / tool-call summary when the session ends."""
    print(f"\033[90m[HOOK] Session Summary\033[0m")
    print(f"\033[90m  Tool calls       : {stats.get('tool_calls', 0)}\033[0m")
    print(f"\033[90m  Prompt tokens    : {stats.get('prompt_tokens', 0)}\033[0m")
    print(f"\033[90m  Completion tokens: {stats.get('completion_tokens', 0)}\033[0m")
    print(f"\033[90m  Total tokens     : {stats.get('total_tokens', 0)}\033[0m")
    return None


# ── Register All Hooks ────────────────────────────────────────

register_hook("UserPromptSubmit", context_inject_hook)

register_hook("PreToolUse", privacy_mask_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)

register_hook("PostToolUse", output_display_hook)

register_hook("Stop", session_summary_hook)
