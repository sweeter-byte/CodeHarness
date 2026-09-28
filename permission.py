import os
import re
from pathlib import Path

# ── Level 1: Deny List ───────────────────────────────────────
# Commands matching these patterns are ALWAYS rejected, no user prompt.
DENY_LIST = [
    r"rm\s+-rf\s+/", # rm -rf /
    r"mkfs\.", # mkfs.
    r"dd\s+if=.+of=/dev/", # dd if=of=/dev/
    r">\s*/dev/sd", # > /dev/sd
    r"chmod\s+-R\s+777\s+/", # chmod -R 777 /
    r":\(\)\s*\{", # :(){ :; };
    r"shutdown",
    r"reboot",
    r"halt",
    r"poweroff",
    r"init\s+0", # init 0
]
DENY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DENY_LIST]

# ── Level 2: Ask Rules ───────────────────────────────────────
# Commands matching these patterns require user approval.
ASK_RULES_RAW = [
    r"sudo\s+",
    r"pip\s+install",
    r"apt(-get)?\s+(install|remove|purge)",
    r"npm\s+(install|uninstall)",
    r"curl\s+.+\|\s*(bash|sh)",
    r"wget\s+.+\|\s*(bash|sh)",
    r"git\s+push",
    r"docker\s+(rm|stop|kill|rmi)",
    r"chmod\s+777",
    r"mv\s+.*\s+/",
    r"cp\s+-r",
    r"kill\s+-9",
    r"\brm\s+",
    r"\bmkdir\s+",
    r"\btouch\s+",
    r"\btouch\s+",
    r"\btee\s+",
    r"\btruncate\s+",
]
ASK_RULES = [re.compile(p, re.IGNORECASE) for p in ASK_RULES_RAW]

# Tools that are read-only / side-effect-free and always auto-allowed.
# 'task' (delegation) is safe itself; risk control happens on each of the
# subagent's own tool calls, which go through the same permission gates.
SAFE_TOOLS_AUTO = {"glob", "grep", "todo_write", "task", "load_skill"}


class PermissionManager:
    def __init__(self, allowed_dirs: list[str] | None = None):
        if allowed_dirs:
            self.allowed_dirs = [Path(d).resolve() for d in allowed_dirs]
        else:
            self.allowed_dirs = [Path(os.getcwd()).resolve()]

    # ── Path helpers ──────────────────────────────────────────

    def _is_subpath(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _is_path_safe(self, path_str: str) -> bool:
        try:
            p = Path(path_str).expanduser().resolve()
        except (OSError, ValueError):
            return False
        return any(self._is_subpath(p, d) for d in self.allowed_dirs)

    # ── Path extraction ───────────────────────────────────────

    def _extract_paths_from_args(self, tool_name: str, args: dict) -> list[str]:
        path_keys = {
            "read_file": ["path"],
            "write_file": ["path"],
            "edit_file": ["path"],
            "grep": ["path"],
            "bash": [],
            "glob": [],
        }
        keys = path_keys.get(tool_name, [])
        return [args[k] for k in keys if k in args and args[k]]

    def _extract_bash_paths(self, command: str) -> list[str]:
        paths = []
        for match in re.finditer(
            r"(?:cat|less|more|head|tail|vim|nano|vi|open|mkdir)\s+(?:-\S+\s+)*(\S+)", command
        ):
            paths.append(match.group(1))
            
        _SYSTEM_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}
        for match in re.finditer(r"(?:>>|>)\s*(\S+)", command):
            if match.group(1) not in _SYSTEM_SINKS:
                paths.append(match.group(1))
        return paths

    # ── Main check ────────────────────────────────────────────

    def check(self, tool_name: str, args: dict) -> tuple[str, str]:
        """
        Evaluate whether a tool invocation is allowed.

        Returns:
            (decision, reason) where decision is one of:
              - "allow": safe to execute without asking
              - "deny":  forbidden, do NOT execute
              - "ask":   requires user approval before executing
        """
        # Read-only tools are always safe.
        if tool_name in SAFE_TOOLS_AUTO:
            return "allow", "Read-only tool, auto-allowed"

        # Layer 1: Safe-path check.
        paths = self._extract_paths_from_args(tool_name, args)
        if tool_name == "bash":
            paths += self._extract_bash_paths(args.get("command", ""))

        for p in paths:
            if not self._is_path_safe(p):
                return "ask", f"Path outside allowed directories: {p}"

        # Layer 2, Level 1: Deny list.
        if tool_name == "bash":
            command = args.get("command", "")
            for pattern in DENY_PATTERNS:
                if pattern.search(command):
                    return "deny", f"Command matches deny-list pattern: {pattern.pattern}"

            # Layer 2, Level 2: Ask rules.
            for pattern in ASK_RULES:
                if pattern.search(command):
                    return "ask", f"Command matches approval rule: {pattern.pattern}"

        # Layer 2, Level 3: File-write tools always need confirmation.
        if tool_name in ("write_file", "edit_file"):
            return "ask", f"File modification: {tool_name}"

        return "allow", "Passed all checks"
