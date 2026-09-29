"""Task System — persistent, recoverable task graph for multi-agent collaboration.

Each task is a JSON file in .tasks/{id}.json.
Dependencies form a DAG via blockedBy edges.
Two-phase construction: create all nodes first, then add edges via update_task.
"""

import json
import os
import secrets
from dataclasses import dataclass, asdict, field
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────

TASKS_DIR = Path(".tasks")

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


# ── TaskStore ──────────────────────────────────────────────────


class TaskStore:
	"""File-backed task store with DAG dependency validation."""

	def __init__(self, tasks_dir: str | Path = TASKS_DIR):
		self.tasks_dir = Path(tasks_dir)

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
		self.tasks_dir.mkdir(parents=True, exist_ok=True)
		path = self.tasks_dir / f"{task.id}.json"
		path.write_text(
			json.dumps(asdict(task), indent=2, ensure_ascii=False),
			encoding="utf-8",
		)

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


TASKS = TaskStore(TASKS_DIR)

# ── Tool Handlers ──────────────────────────────────────────────


def run_create_task(subject: str, description: str = "") -> str:
	try:
		task = TASKS.create(subject, description)
		return f"Created {task.id}: {task.subject}"
	except ValueError as e:
		return f"Error: {e}"


def run_update_task(task_id: str, addBlockedBy: list | str) -> str:
	if isinstance(addBlockedBy, str):
		try:
			addBlockedBy = json.loads(addBlockedBy)
		except json.JSONDecodeError as e:
			return f"Error: invalid JSON for addBlockedBy: {e}"
	if not isinstance(addBlockedBy, list):
		return "Error: addBlockedBy must be a list"
	try:
		task = TASKS.update_dependencies(task_id, addBlockedBy)
		return f"Updated {task.id}: blockedBy={task.blockedBy}"
	except (FileNotFoundError, ValueError) as e:
		return f"Error: {e}"


def run_can_start(task_id: str) -> str:
	try:
		result = TASKS.can_start(task_id)
		return str(result).lower()
	except FileNotFoundError as e:
		return f"Error: {e}"


def run_claim_task(task_id: str, owner: str = "agent") -> str:
	try:
		task = TASKS.load(task_id)
	except FileNotFoundError as e:
		return f"Error: {e}"
	if task.status != "pending":
		return f"Task {task_id} is {task.status}, cannot claim"
	if not TASKS.can_start(task_id):
		incomplete = [
			d for d in task.blockedBy
			if TASKS.load(d).status != "completed"
		]
		return f"Blocked by: {incomplete}"
	task.owner = owner
	task.status = "in_progress"
	TASKS.save(task)
	return f"Claimed {task_id} ({task.subject})"


def run_complete_task(task_id: str, owner: str = "agent") -> str:
	try:
		task = TASKS.load(task_id)
	except FileNotFoundError as e:
		return f"Error: {e}"
	if task.status != "in_progress":
		return f"Task {task_id} is {task.status}, cannot complete"
	if task.owner != owner:
		return f"Task {task_id} is owned by {task.owner}, not {owner}"
	ready_before = {
		t.id for t in TASKS.list_all()
		if t.status == "pending" and t.blockedBy and TASKS.can_start(t.id)
	}
	task.status = "completed"
	TASKS.save(task)
	unblocked = [
		t.subject for t in TASKS.list_all()
		if t.status == "pending" and t.blockedBy
		and t.id not in ready_before
		and TASKS.can_start(t.id)
	]
	msg = f"Completed {task_id} ({task.subject})"
	if unblocked:
		msg += f"\nUnblocked: {', '.join(unblocked)}"
	return msg


def run_list_task() -> str:
	tasks = TASKS.list_all()
	if not tasks:
		return "(no tasks)"
	lines = []
	for t in tasks:
		owner = t.owner or "-"
		lines.append(f"{t.id} | {t.status:<12} | {owner:<10} | {t.subject}")
	return "\n".join(lines)


def run_get_task(task_id: str) -> str:
	try:
		task = TASKS.load(task_id)
		return json.dumps(asdict(task), indent=2)
	except FileNotFoundError as e:
		return f"Error: {e}"


def run_reset_tasks() -> str:
	tasks = TASKS.list_all()
	if not tasks:
		return "No tasks to reset"
	incomplete = [t for t in tasks if t.status != "completed"]
	if incomplete:
		return f"Cannot reset: {len(incomplete)} task(s) still incomplete"
	for p in TASKS.tasks_dir.glob("*.json"):
		p.unlink()
	return f"Reset: cleared {len(tasks)} completed task(s)"


# ── Tool Schemas ───────────────────────────────────────────────

TASK_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "create_task",
			"description": (
				"Create a new task with a subject and optional description. "
				"Returns the generated task ID. Use this to build the task graph nodes first."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"subject": {
						"type": "string",
						"description": "Short title of the task.",
					},
					"description": {
						"type": "string",
						"description": "Detailed description of the task (optional).",
					},
				},
				"required": ["subject"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "update_task",
			"description": (
				"Add dependency edges to a task. Call after all tasks are created "
				"to build the DAG. The target task must be pending and unclaimed."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to update.",
					},
					"addBlockedBy": {
						"type": "array",
						"items": {"type": "string"},
						"description": "List of task IDs that must complete before this task can start.",
					},
				},
				"required": ["task_id", "addBlockedBy"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "can_start",
			"description": (
				"Check if a task's dependencies are all completed and it can be started."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to check.",
					},
				},
				"required": ["task_id"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "claim_task",
			"description": (
				"Claim a task: set owner and change status from pending to in_progress. "
				"All dependencies must be completed first."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to claim.",
					},
					"owner": {
						"type": "string",
						"description": "Name of the agent claiming the task.",
					},
				},
				"required": ["task_id"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "complete_task",
			"description": (
				"Mark a task as completed. Verifies owner match. "
				"Returns list of newly unblocked downstream tasks."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to complete.",
					},
					"owner": {
						"type": "string",
						"description": "Name of the agent completing the task.",
					},
				},
				"required": ["task_id"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "list_task",
			"description": (
				"Show a summary table of all tasks: ID, status, owner, subject."
			),
			"parameters": {
				"type": "object",
				"properties": {},
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "get_task",
			"description": (
				"Get full details of a task including description and dependency list."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to retrieve.",
					},
				},
				"required": ["task_id"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "reset_tasks",
			"description": (
				"Clear all tasks. Only allowed when every task is completed. "
				"Use this to clean up after a project is done."
			),
			"parameters": {
				"type": "object",
				"properties": {},
			},
		},
	},
]

# ── Handler Map ────────────────────────────────────────────────

TASK_HANDLERS = {
	"create_task":  run_create_task,
	"update_task":  run_update_task,
	"can_start":    run_can_start,
	"claim_task":   run_claim_task,
	"complete_task": run_complete_task,
	"list_task":    run_list_task,
	"get_task":     run_get_task,
	"reset_tasks":  run_reset_tasks,
}
