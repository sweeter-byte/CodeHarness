"""Task System — persistent, recoverable task graph for multi-agent collaboration.

Each task is one JSON file in the configured runtime directory.
Dependencies form a DAG via blockedBy edges.
Two-phase construction: create all nodes first, then add edges via update_task.

This module holds the data model and file-backed store; the LLM tool schemas
and handlers live in ``codeharness.tasks.tools``.
"""

import json
import os
import secrets
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path

AGENT_NAMES = [
	"Alice", "Bob", "Carol", "Dave", "Eve", "Frank",
	"Grace", "Hank", "Iris", "Jack", "Kate", "Leo",
]

# ── Data Model ─────────────────────────────────────────────────


@dataclass
class Task:
	id: str
	subject: str
	description: str
	status: str                       # pending | in_progress | completed
	owner: str | None
	blockedBy: list[str] = field(default_factory=list)
	worktree: str | None = None      # optional git worktree name (.worktrees/<name>)


# ── TaskStore ──────────────────────────────────────────────────


class TaskStore:
	"""File-backed task store with DAG dependency validation."""

	def __init__(self, tasks_dir: str | Path | None = None):
		self._tasks_dir = Path(tasks_dir) if tasks_dir is not None else None
		# Single-process, multi-thread team: claim/release/save must be atomic.
		self._lock = threading.RLock()

	@property
	def tasks_dir(self) -> Path:
		if self._tasks_dir is None:
			raise RuntimeError(
				"task store is not configured; start CodeHarness first"
			)
		return self._tasks_dir

	def set_directory(self, tasks_dir: str | Path) -> None:
		"""Point this store at an explicit runtime-owned directory."""
		with self._lock:
			self._tasks_dir = Path(tasks_dir)

	# ── ID generation ──

	@staticmethod
	def _generate_id() -> str:
		return f"task_{secrets.token_hex(4)}"

	def _unique_id(self) -> str:
		for _ in range(100):
			tid = self._generate_id()
			if not (self.tasks_dir / f"{tid}.json").exists():
				return tid
		raise RuntimeError("Failed to generate unique task ID after 100 attempts")

	# ── CRUD ──

	def load(self, task_id: str) -> Task:
		path = self.tasks_dir / f"{task_id}.json"
		if not path.exists():
			raise FileNotFoundError(f"Task not found: {task_id}")
		data = json.loads(path.read_text(encoding="utf-8"))
		return Task(**data)

	def save(self, task: Task) -> None:
		"""Atomic write: temp file + os.replace, safe under concurrent claims."""
		self.tasks_dir.mkdir(parents=True, exist_ok=True)
		path = self.tasks_dir / f"{task.id}.json"
		tmp = path.with_suffix(".json.tmp")
		tmp.write_text(
			json.dumps(asdict(task), indent=2, ensure_ascii=False),
			encoding="utf-8",
		)
		os.replace(tmp, path)

	def list_all(self) -> list[Task]:
		if not self.tasks_dir.exists():
			return []
		tasks = []
		for p in sorted(self.tasks_dir.glob("*.json")):
			data = json.loads(p.read_text(encoding="utf-8"))
			tasks.append(Task(**data))
		return tasks

	# ── Name allocation ──

	def allocate_name(self) -> str | None:
		active = {t.owner for t in self.list_all() if t.owner}
		available = [n for n in AGENT_NAMES if n not in active]
		return available[0] if available else None

	# ── DAG validation ──

	def _has_cycle(self) -> bool:
		"""Kahn's algorithm: True if the dependency graph contains a cycle."""
		tasks = self.list_all()
		in_degree = {t.id: 0 for t in tasks}
		adj = {t.id: [] for t in tasks}
		for t in tasks:
			for dep in t.blockedBy:
				if dep in adj:
					adj[dep].append(t.id)
					in_degree[t.id] += 1
		queue = [tid for tid, d in in_degree.items() if d == 0]
		visited = 0
		while queue:
			node = queue.pop(0)
			visited += 1
			for neighbor in adj[node]:
				in_degree[neighbor] -= 1
				if in_degree[neighbor] == 0:
					queue.append(neighbor)
		return visited != len(tasks)

	def _validate_dependencies(
		self, task: Task, add_blocked_by: list[str]
	) -> str | None:
		if task.status != "pending":
			return f"Task {task.id} is {task.status}, cannot modify dependencies"
		if task.owner is not None:
			return f"Task {task.id} is already claimed by {task.owner}"
		for dep in add_blocked_by:
			if dep == task.id:
				return f"Task cannot depend on itself"
			try:
				self.load(dep)
			except FileNotFoundError:
				return f"Dependency task not found: {dep}"
		return None

	# ── Core operations ──

	def create(self, subject: str, description: str = "") -> Task:
		if not subject or not subject.strip():
			raise ValueError("Subject cannot be empty")
		tid = self._unique_id()
		task = Task(
			id=tid,
			subject=subject.strip(),
			description=description.strip() if description else "",
			status="pending",
			owner=None,
			blockedBy=[],
		)
		self.save(task)
		return task

	def update_dependencies(
		self, task_id: str, add_blocked_by: list[str]
	) -> Task:
		task = self.load(task_id)
		err = self._validate_dependencies(task, add_blocked_by)
		if err:
			raise ValueError(err)
		existing = set(task.blockedBy)
		for dep in add_blocked_by:
			if dep not in existing:
				task.blockedBy.append(dep)
		self.save(task)
		if self._has_cycle():
			task.blockedBy = [d for d in task.blockedBy if d not in add_blocked_by]
			self.save(task)
			raise ValueError(
				f"Adding dependencies {add_blocked_by} would create a cycle"
			)
		return task

	def can_start(self, task_id: str) -> bool:
		task = self.load(task_id)
		for dep_id in task.blockedBy:
			try:
				dep = self.load(dep_id)
				if dep.status != "completed":
					return False
			except FileNotFoundError:
				return False
		return True

	# ── Atomic claim / release (multi-agent) ──

	def claim(self, task_id: str, owner: str,
			  worktree_resolver=None) -> tuple[Task | None, str | None, str | None]:
		"""Atomically claim a task. Returns (task, cwd, error).

		Validation chain, all under the store lock:
			pending & unclaimed → owner has no in-progress task → dependencies
			ready → worktree (if bound) resolves to a valid directory.
		Only then are owner + in_progress written — concurrent claimers that
		saw the same snapshot lose atomically.
		"""
		with self._lock:
			try:
				task = self.load(task_id)
			except FileNotFoundError as e:
				return None, None, str(e)
			if task.status != "pending" or task.owner is not None:
				detail = f" (claimed by {task.owner})" if task.owner else ""
				return None, None, f"Task {task_id} is {task.status}{detail}, cannot claim"
			for t in self.list_all():
				if t.owner == owner and t.status == "in_progress":
					return None, None, (
						f"Owner {owner} must complete its current task {t.id} first"
					)
			if not self.can_start(task_id):
				incomplete = [
					d for d in task.blockedBy
					if self.load(d).status != "completed"
				]
				return None, None, f"Task {task_id} is blocked by: {incomplete}"
			cwd = None
			if task.worktree:
				if worktree_resolver is None:
					return None, None, (
						f"Task {task_id} is bound to worktree '{task.worktree}'; "
						"only teammates can claim worktree-bound tasks"
					)
				cwd, werr = worktree_resolver(task)
				if werr:
					return None, None, f"Cannot claim {task_id}: {werr}"
			task.owner = owner
			task.status = "in_progress"
			self.save(task)
			return task, cwd, None

	def release(self, task_id: str) -> tuple[Task | None, str | None]:
		"""Force-release an in_progress task back to pending (leader recovery)."""
		with self._lock:
			try:
				task = self.load(task_id)
			except FileNotFoundError as e:
				return None, str(e)
			if task.status != "in_progress":
				return None, f"Task {task_id} is {task.status}; only in_progress tasks can be released"
			task.owner = None
			task.status = "pending"
			self.save(task)
			return task, None

	def scan_ready_tasks(self) -> list[Task]:
		"""Snapshot of unclaimed tasks whose dependencies are all completed.
		Multiple idle teammates may see the same snapshot; claim() is the arbiter."""
		with self._lock:
			return [
				t for t in self.list_all()
				if t.status == "pending" and t.owner is None and self.can_start(t.id)
			]


TASKS = TaskStore()
