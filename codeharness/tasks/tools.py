"""Tool schemas and handlers for the Task System."""

import json
from dataclasses import asdict

from codeharness.tasks.store import TASKS

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
	task, _cwd, error = TASKS.claim(task_id, owner)
	if error:
		return f"Error: {error}"
	return f"Claimed {task_id} ({task.subject})"


def run_release_task(task_id: str) -> str:
	"""Leader recovery path: force an in_progress task back to pending."""
	task, error = TASKS.release(task_id)
	if error:
		return f"Error: {error}"
	return f"Released {task_id} ({task.subject}) back to pending"


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
			"name": "release_task",
			"description": (
				"Force-release an in_progress task back to pending (owner cleared). "
			"Use for crash recovery or reassigning work. Leader only."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"task_id": {
						"type": "string",
						"description": "ID of the task to release.",
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
	"release_task": run_release_task,
	"list_task":    run_list_task,
	"get_task":     run_get_task,
	"reset_tasks":  run_reset_tasks,
}
