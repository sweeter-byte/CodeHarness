"""Git worktree management for task workspace isolation.

Worktrees only separate Git working directories and branches — they are
NOT a security sandbox; shell commands can still reach anything the
process can access. Creation and removal are leader-only operations.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from codeharness.tasks import TASKS

BRANCH_PREFIX = "wt/"
_workspace_root: Path | None = None
_worktrees_dir: Path | None = None
_environment: WorktreeEnvironment | None = None


class WorktreeEnvironment(Protocol):
	"""Minimal execution boundary required by Team worktree operations."""

	def git(
		self,
		args: Sequence[str],
		cwd: str | Path,
	) -> tuple[int, str]: ...

	def path_exists(self, path: str | Path) -> bool: ...


class LocalWorktreeEnvironment:
	"""Run worktree Git and existence checks on the host."""

	def git(
		self,
		args: Sequence[str],
		cwd: str | Path,
	) -> tuple[int, str]:
		try:
			r = subprocess.run(
				["git", *args],
				cwd=str(cwd),
				capture_output=True,
				text=True,
				errors="replace",
				timeout=60,
				check=False,
			)
			return r.returncode, (r.stdout + r.stderr).strip()
		except (subprocess.TimeoutExpired, OSError) as exc:
			return -1, str(exc)

	def path_exists(self, path: str | Path) -> bool:
		return Path(path).exists()


def configure_worktrees(
	workspace_root: str | Path,
	worktrees_dir: str | Path,
	environment: WorktreeEnvironment | None = None,
) -> None:
	"""Configure explicit roots for all worktree operations."""
	global _workspace_root, _worktrees_dir, _environment
	if environment is None:
		_environment = LocalWorktreeEnvironment()
		_workspace_root = Path(workspace_root).expanduser().resolve()
		_worktrees_dir = Path(worktrees_dir).expanduser().resolve()
	else:
		workspace_path = Path(workspace_root)
		worktrees_path = Path(worktrees_dir)
		if not workspace_path.is_absolute() or not worktrees_path.is_absolute():
			raise ValueError("logical worktree roots must be absolute paths")
		_environment = environment
		_workspace_root = workspace_path
		_worktrees_dir = worktrees_path


def _git(args: list[str], cwd: str | Path | None = None) -> tuple[int, str]:
	"""Run a git command; returns (exit_code, combined_output)."""
	if _environment is None:
		return -1, "worktree environment is not configured; start CodeHarness first"
	if cwd is None:
		return -1, "git cwd is not configured; start CodeHarness first"
	return _environment.git(args, cwd)


def _path_exists(path: str | Path) -> bool:
	if _environment is None:
		return False
	return _environment.path_exists(path)


def _validate_name(name: str) -> str | None:
	"""Return an error message for invalid worktree names, else None."""
	if not name or not name.strip():
		return "Worktree name cannot be empty"
	if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name.strip()):
		return (f"Invalid worktree name '{name}': use letters, digits, "
				"'.', '_', '-' only")
	if name.strip() in {".", ".."}:
		return f"Reserved name: {name}"
	return None


def worktree_path(name: str) -> Path:
	if _worktrees_dir is None:
		raise RuntimeError("worktree paths are not configured; start CodeHarness first")
	return _worktrees_dir / name


# ── Create ────────────────────────────────────────────────────


def create_worktree(name: str, task_id: str) -> tuple[str | None, str | None]:
	"""Create .worktrees/<name> on branch wt/<name> and bind it to a task.

	Leader-only. The task must be pending, unclaimed and unbound. On Git
	failure with leftovers (branch or directory already exists), report a
	partial operation and leave the task unbound for manual recovery.
	Returns (path, error).
	"""
	name = name.strip()
	err = _validate_name(name)
	if err:
		return None, err
	try:
		task = TASKS.load(task_id)
	except FileNotFoundError as e:
		return None, str(e)
	if task.worktree is not None:
		return None, f"Task {task_id} is already bound to worktree '{task.worktree}'"
	if task.status != "pending" or task.owner is not None:
		return None, f"Task {task_id} is {task.status} (owner={task.owner}); only pending unclaimed tasks can bind a worktree"

	try:
		path = worktree_path(name)
	except RuntimeError as exc:
		return None, str(exc)
	if _path_exists(path):
		return None, f"Worktree directory already exists: {path}"

	code, out = _git(
		["worktree", "add", str(path), "-b", BRANCH_PREFIX + name],
		cwd=_workspace_root,
	)
	if code != 0:
		# Partial operation check: git may leave a branch or registration.
		partial = []
		bc, _ = _git(
			["rev-parse", "--verify", f"refs/heads/{BRANCH_PREFIX}{name}"],
			cwd=_workspace_root,
		)
		if bc == 0:
			partial.append(f"branch {BRANCH_PREFIX + name}")
		if _path_exists(path):
			partial.append(f"directory {path}")
		msg = f"git worktree add failed: {out}"
		if partial:
			msg += (f" (partial operation: {', '.join(partial)} left behind; "
					"task left unbound — recover manually)")
		return None, msg

	# Bind only after the checkout fully succeeded.
	task.worktree = name
	TASKS.save(task)
	return str(path), None


# ── Resolve ───────────────────────────────────────────────────


def resolve_worktree_cwd(task) -> tuple[str | None, str | None]:
	"""Resolve the working directory for a task about to be claimed.

	Bound tasks resolve to their worktree (must exist with a healthy Git
	state); unbound tasks resolve to the repository root. Refusing to claim
	is safer than silently writing to some other directory.
	"""
	if _workspace_root is None or _worktrees_dir is None:
		return None, "worktree paths are not configured; start CodeHarness first"
	if task.worktree is None:
		return str(_workspace_root), None
	path = worktree_path(task.worktree)
	if not _path_exists(path):
		return None, f"Bound worktree does not exist: {path}"
	code, out = _git(["rev-parse", "--is-inside-work-tree"], cwd=path)
	if code != 0:
		return None, f"Bound worktree has unhealthy Git state ({path}): {out}"
	return str(path), None


# ── Remove ────────────────────────────────────────────────────


def remove_worktree(name: str, force: bool = False) -> tuple[bool, str]:
	"""Safely remove a worktree directory; the wt/<name> branch is KEPT.

	Preconditions: no unfinished task is bound to it, no teammate is
	assigned to it, and the tree is clean (unless force, i.e. the user
	explicitly allows discarding uncommitted changes).
	"""
	name = name.strip()
	err = _validate_name(name)
	if err:
		return False, err
	try:
		path = worktree_path(name)
	except RuntimeError as exc:
		return False, str(exc)
	if not _path_exists(path):
		return False, f"Worktree does not exist: {path}"

	# 1. No unfinished task bound to this worktree.
	for t in TASKS.list_all():
		if t.worktree == name and t.status != "completed":
			return False, (f"Task {t.id} ({t.status}) is still bound to "
						   f"worktree '{name}'; complete or release it first")

	# 2. No teammate currently assigned to it (check live registry).
	from codeharness.team.manager import TEAM
	for state in TEAM.list_states():
		if state.assignment and Path(state.assignment.get("cwd", "")) == path:
			return False, (f"Teammate {state.name} is still working in "
						   f"'{name}'; shut it down or let it finish first")

	# 3. Clean tree, unless the user forces the discard.
	code, out = _git(["status", "--porcelain"], cwd=path)
	if code != 0:
		return False, f"Cannot inspect worktree state ({path}): {out}"
	if out and not force:
		return False, (f"Worktree '{name}' has uncommitted changes; pass "
					   "force=true to discard them, or handle them manually")

	remove_args = ["worktree", "remove", str(path)] + (["--force"] if force else [])
	code, out = _git(remove_args, cwd=_workspace_root)
	if code != 0:
		return False, f"git worktree remove failed: {out}"
	return True, (f"Removed worktree '{name}' (branch {BRANCH_PREFIX + name} "
				  "kept — committed work is safe)")
