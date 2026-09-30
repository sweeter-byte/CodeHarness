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
SAFE_TOOLS_AUTO = {"glob", "grep", "todo_write", "task", "load_skill", "cron_create", "cron_list", "cron_delete"}

# ── MCP host-side policy ─────────────────────────────────────
# The Harness is the FINAL authorization gate. Server-declared annotations
# (readOnlyHint / destructiveHint / ...) are hints only and are never used to
# grant access. Keyed by (server_name, raw_tool_name) — NOT by the prefixed
# alias, because aliases past 64 chars are hash-shortened and cannot be
# reliably reverse-parsed. Values: "allow" | "ask" | "deny". Anything not
# listed defaults to "ask".
MCP_TOOL_PREFIX = "mcp__"
MCP_HOST_POLICY = {
    # Filesystem MCP: reads auto-allowed, mutations require approval.
    ("filesystem", "list_directory"): "allow",
    ("filesystem", "list_directory_with_sizes"): "allow",
    ("filesystem", "search_files"): "allow",
    ("filesystem", "get_file_info"): "allow",
    ("filesystem", "read_file"): "allow",
    ("filesystem", "read_text_file_lines"): "allow",
    ("filesystem", "read_multiple_files"): "allow",
    ("filesystem", "write_file"): "ask",
    ("filesystem", "create_directory"): "ask",
    ("filesystem", "move_file"): "ask",
    ("filesystem", "edit_file"): "ask",
    ("filesystem", "delete_file"): "ask",
    # GitHub MCP: reads auto-allowed, writes require approval.
    ("github", "get_issue"): "allow",
    ("github", "list_issues"): "allow",
    ("github", "get_pull_request"): "allow",
    ("github", "list_pull_requests"): "allow",
    ("github", "get_file_contents"): "allow",
    ("github", "search_code"): "allow",
    ("github", "create_issue"): "ask",
    ("github", "create_pull_request"): "ask",
    ("github", "merge_pull_request"): "ask",
}


class PermissionManager:
    def __init__(self, allowed_dirs: list[str] | None = None,
                 mcp_resolve=None, mcp_annotations_of=None,
                 base_dir: str | Path | None = None):
        if allowed_dirs:
            self.allowed_dirs = [Path(d).expanduser().resolve() for d in allowed_dirs]
        else:
            self.allowed_dirs = [Path(os.getcwd()).resolve()]
        # Base directory for resolving RELATIVE paths in safety checks. The
        # Runtime passes its workspace here so permission checks evaluate the
        # same paths the coding tool handlers (bound to RuntimeConfig.workspace)
        # will actually touch. When absent (standalone construction in tests)
        # we fall back to the process cwd, preserving the old behavior.
        if base_dir is not None:
            self.base_dir = Path(base_dir).expanduser().resolve()
        else:
            self.base_dir = Path(os.getcwd()).resolve()
        # Small injected callables (not the MCPManager itself) keep the
        # permission layer decoupled from MCP lifecycle management:
        #   mcp_resolve(prefixed)        -> (server, raw_tool) | None
        #   mcp_annotations_of(prefixed) -> server hint object | None
        self._mcp_resolve = mcp_resolve
        self._mcp_annotations_of = mcp_annotations_of

    def set_mcp_provider(self, resolve, annotations_of=None):
        """Inject (or replace) the MCP name-resolver / annotation callables."""
        self._mcp_resolve = resolve
        self._mcp_annotations_of = annotations_of

    # ── Path helpers ──────────────────────────────────────────

    def _is_subpath(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve a possibly-relative path against self.base_dir.

        Single resolution entry point for every path safety check: relative
        paths are anchored to base_dir (the Runtime workspace), never to the
        implicit process cwd.
        """
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = self.base_dir / p
        return p.resolve()

    def _is_path_safe(self, path_str: str) -> bool:
        try:
            p = self._resolve_path(path_str)
        except (OSError, ValueError):
            return False
        return any(self._is_subpath(p, d) for d in self.allowed_dirs)

    def _glob_pattern_escapes(self, pattern: str) -> bool:
        """Lightweight boundary check for glob patterns.

        Deliberately conservative: any '..' path component in the pattern
        means the glob may traverse outside the workspace, so it must be
        approved. Ordinary wildcards ('*', '**', '?') are never blocked and
        no glob parser is implemented here.
        """
        return ".." in pattern.split("/")

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
        for match in re.finditer(r"(?:\d*>>|\d*>)\s*(\S+)", command):
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

        Order matters: MCP policy → path boundary → read-only auto-allow →
        bash deny/ask rules → write confirmation. Read-only means "needs no
        dangerous-operation approval", NOT "may cross the workspace
        boundary", so the safe-path check runs before SAFE_TOOLS_AUTO.
        """
        # MCP tools are gated by the host policy, not by path/bash rules.
        if tool_name.startswith(MCP_TOOL_PREFIX):
            return self._check_mcp(tool_name, args)

        # Layer 1: Safe-path check — every extracted path (including those
        # scraped from bash commands, best-effort: this is a permission hint,
        # not an OS-level sandbox) must stay inside the allowed directories.
        paths = self._extract_paths_from_args(tool_name, args)
        if tool_name == "bash":
            paths += self._extract_bash_paths(args.get("command", ""))

        for p in paths:
            if not self._is_path_safe(p):
                return "ask", f"Path outside allowed directories: {p}"

        # glob has no path parameter, only a pattern; reject conservative
        # '..' traversal before the read-only auto-allow below.
        if tool_name == "glob":
            pattern = args.get("pattern", "")
            if self._glob_pattern_escapes(pattern):
                return "ask", f"Glob pattern may escape allowed directories: {pattern}"

        # Read-only tools are auto-allowed (after the boundary checks above).
        if tool_name in SAFE_TOOLS_AUTO:
            return "allow", "Read-only tool, auto-allowed"

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

    # ── MCP check ─────────────────────────────────────────────

    def _mcp_annotation_hint(self, prefixed: str) -> str | None:
        """Render server-declared hints for display only (never authorization)."""
        if self._mcp_annotations_of is None:
            return None
        ann = self._mcp_annotations_of(prefixed)
        if ann is None:
            return None
        hints = []
        if getattr(ann, "read_only_hint", False):
            hints.append("read-only")
        if getattr(ann, "destructive_hint", False):
            hints.append("destructive")
        if getattr(ann, "idempotent_hint", False):
            hints.append("idempotent")
        if getattr(ann, "open_world_hint", False):
            hints.append("open-world")
        return ", ".join(hints) or None

    def _check_mcp(self, prefixed: str, args: dict) -> tuple[str, str]:
        pair = self._mcp_resolve(prefixed) if self._mcp_resolve else None
        if pair is None:
            # Unregistered / unresolvable external tool: default to approval.
            return "ask", f"Unregistered MCP tool '{prefixed}'"

        server, raw = pair
        decision = MCP_HOST_POLICY.get(pair, "ask")
        hint = self._mcp_annotation_hint(prefixed)
        label = f"MCP tool {server}.{raw}" + (f" [{hint}]" if hint else "")

        if decision == "deny":
            return "deny", f"{label} is denied by host policy"
        if decision == "ask":
            return "ask", f"{label} requires approval"
        return "allow", f"{label} allowed by host policy"
