"""Leader-side team tools: lifecycle, worktree management, messaging, approval.

Schema + handler pairs follow the codeharness/tasks/tools.py convention; CodeHarness Runtime's
__main__ registers TEAM_TOOLS with a runtime-bound handler map into the leader's
ToolRegistry. Teammates never see these — their tool set is built from the
base tool registry in teammate.py.
"""

from codeharness.skills import SkillRegistry
from codeharness.team import protocol
from codeharness.team.bus import BUS, LEADER
from codeharness.team.manager import TEAM
from codeharness.team.worktree import create_worktree, remove_worktree

# ── Handlers ──────────────────────────────────────────────────


def run_shutdown_teammate(name: str) -> str:
	return TEAM.shutdown(name)


def run_list_teammates() -> str:
	return TEAM.status()


def run_create_worktree(name: str, task_id: str) -> str:
	path, error = create_worktree(name, task_id)
	if error:
		return f"Error: {error}"
	return (f"Created worktree {path} on branch wt/{name.strip()} "
			f"and bound it to {task_id}")


def run_remove_worktree(name: str, force: bool = False) -> str:
	ok, msg = remove_worktree(name, force=force)
	return msg if ok else f"Error: {msg}"


def run_send_message(to: str, content: str) -> str:
	if to == LEADER:
		return "Error: you are the leader; send_message reaches teammates"
	if to in TEAM.teammate_names():
		BUS.send(LEADER, to, content)
		return f"Sent to {to}"
	return f"Error: unknown teammate '{to}' (check list_teammates)"


def run_approve_plan(request_id: str, approve: bool,
					 feedback: str = "") -> str:
	req = protocol.get_request(request_id)
	if req is None or req.type != "plan_approval_request" \
			or req.status != "pending":
		return f"Error: no pending plan approval request '{request_id}'"
	BUS.send(LEADER, req.sender,
			 feedback or ("Approved." if approve else "Rejected."),
			 "plan_approval_response",
			 metadata={"request_id": request_id,
					   "approve": approve,
					   "feedback": feedback})
	action = "approval" if approve else "rejection"
	return (f"Sent {action} to {req.sender} for {request_id} "
			f"(task {req.task_id})")


# ── Schemas ───────────────────────────────────────────────────


def _schema(name: str, description: str, properties: dict,
			required: list) -> dict:
	return {
		"type": "function",
		"function": {
			"name": name,
			"description": description,
			"parameters": {
				"type": "object",
				"properties": properties,
				"required": required,
			},
		},
	}


TEAM_TOOLS = [
	_schema(
		"spawn_teammate",
		"Start a persistent teammate agent on a task. The task is atomically "
		"claimed for the teammate FIRST; if it is unavailable the spawn fails "
		"and nothing starts. Propose the team split to the user and WAIT for "
		"confirmation before spawning. The teammate reports results back "
		"automatically — do not poll.",
		{
			"task_id": {"type": "string",
						"description": "ID of the initial task (must be pending & unclaimed)."},
			"name": {"type": "string",
					 "description": "Optional teammate name; auto-allocated from the pool when omitted."},
			"require_plan": {"type": "boolean",
							 "description": "If true, the teammate must submit a plan and wait for your "
											"approval (approve_plan) before modifying anything."},
		},
		["task_id"],
	),
	_schema(
		"shutdown_teammate",
		"Gracefully shut down a teammate. It finishes its current step, "
		"responds with shutdown_response, and exits — the thread is never killed.",
		{
			"name": {"type": "string", "description": "Name of the teammate."},
		},
		["name"],
	),
	_schema(
		"list_teammates",
		"List live teammates: name, plan-gate state, current task, working directory.",
		{},
		[],
	),
	_schema(
		"create_worktree",
		"Create an isolated git worktree (.worktrees/<name>, branch wt/<name>) "
		"and bind it to a pending, unclaimed task. Use when teammates would "
		"otherwise modify the same files. Worktrees isolate directories, NOT security.",
		{
			"name": {"type": "string", "description": "Worktree name (letters, digits, '.', '_', '-')."},
			"task_id": {"type": "string", "description": "Task to bind the worktree to."},
		},
		["name", "task_id"],
	),
	_schema(
		"remove_worktree",
		"Safely remove a worktree directory (the wt/<name> branch is KEPT). "
		"Refuses while an unfinished task or a teammate still uses it, or when "
		"there are uncommitted changes (pass force=true to discard).",
		{
			"name": {"type": "string", "description": "Worktree name."},
			"force": {"type": "boolean",
					  "description": "Discard uncommitted changes (user-approved)."},
		},
		["name"],
	),
	_schema(
		"send_message",
		"Send a message to a teammate. Teammates' results and status changes "
		"arrive automatically as [Team events]; use this for direct instructions.",
		{
			"to": {"type": "string", "description": "Teammate name (see list_teammates)."},
			"content": {"type": "string", "description": "Message text."},
		},
		["to", "content"],
	),
	_schema(
		"approve_plan",
		"Approve or reject a teammate's submitted plan (matched by request_id "
		"from the plan_approval_request team event). The approval only takes "
		"effect if the teammate is still on the same task and work generation.",
		{
			"request_id": {"type": "string", "description": "request_id from the plan_approval_request event."},
			"approve": {"type": "boolean", "description": "True to approve, False to reject."},
			"feedback": {"type": "string",
						 "description": "Optional guidance; required context when rejecting."},
		},
		["request_id", "approve"],
	),
]

def make_team_handlers(
	skill_registry: SkillRegistry,
	*,
	agent_factory=None,
	workspace_backend=None,
	team_manager=TEAM,
) -> dict:
	"""Bind team spawning to one Runtime's skills and agent dependencies."""

	def spawn_teammate(task_id: str, name: str | None = None,
					   require_plan: bool = False) -> str:
		return team_manager.spawn(
			task_id,
			name=name,
			require_plan=require_plan,
			skill_registry=skill_registry,
			agent_factory=agent_factory,
			workspace_backend=workspace_backend,
		)

	return {
		"spawn_teammate": spawn_teammate,
		"shutdown_teammate": run_shutdown_teammate,
		"list_teammates": run_list_teammates,
		"create_worktree": run_create_worktree,
		"remove_worktree": run_remove_worktree,
		"send_message": run_send_message,
		"approve_plan": run_approve_plan,
	}
