"""Teammate runtime — persistent worker thread with WORK/IDLE lifecycle.

A teammate is one Agent instance running in a daemon thread with its own
conversation context (kept across tasks), its own TodoManager and
BackgroundManager, memory disabled, and non-interactive permissions.

Unlike a subagent (one-shot, discarded), a teammate stays alive:
  WORK → result + idle_notification → IDLE → (message | ready task) → WORK
...until a shutdown_request handshake completes.

All constraints live in the harness, never in LLM behaviour assumptions:
  - spawn only succeeds if the initial task was atomically claimed first
  - workspace tools resolve cwd from the current assignment, and are
    disabled when no task is assigned (never fall back to the repo root)
  - complete_task does not unbind the cwd; unbinding happens after the
    round ends (later tools in the same round may still need the directory)
  - plan gate blocks write tools until the leader approves a plan, and
    stale approvals are invalidated by work_version bumps
"""

import os
import threading
from dataclasses import dataclass, field

from codeharness.tasks import TASKS, TASK_TOOLS, TASK_HANDLERS
from codeharness.tools import build_base_registry, TodoManager
from codeharness.skills.tools import SKILL_LOADER
from codeharness.team.bus import BUS, LEADER
from codeharness.team import protocol
from codeharness.team.protocol import (
	GATE_NOT_REQUIRED, GATE_PENDING, GATE_REQUIRED, GATE_APPROVED,
	GATE_REJECTED, BLOCKING_GATES,
)

IDLE_SCAN_INTERVAL = 2.0     # seconds between shared-task-board scans
TEAMMATE_MAX_ROUNDS = 60

# Workspace tools get their cwd injected from the assignment; write tools
# are additionally gated by the plan state.
WORKSPACE_TOOLS = {"bash", "read_file", "write_file", "edit_file", "glob", "grep"}
WRITE_TOOLS = {"bash", "write_file", "edit_file"}

# ── Teammate tool set (built explicitly from the base registry) ──

# Task-board tools a teammate may use; graph mutation (create/update/
# release/reset) stays leader-only.
_TASK_BOARD_NAMES = {"claim_task", "complete_task", "list_task",
					 "get_task", "can_start"}
# Teammates don't schedule cron jobs.
_EXCLUDED_NAMES = {"cron_create", "cron_list", "cron_delete"}

SEND_MESSAGE_SCHEMA = {
	"type": "function",
	"function": {
		"name": "send_message",
		"description": (
			"Send a message to the leader ('lead') or another teammate. "
			"This is the ONLY way to reach other agents — your final answer "
			"alone is not delivered anywhere."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"to": {
					"type": "string",
					"description": "Recipient: 'lead' or a teammate name.",
				},
				"content": {
					"type": "string",
					"description": "Message text.",
				},
			},
			"required": ["to", "content"],
		},
	},
}

SUBMIT_PLAN_SCHEMA = {
	"type": "function",
	"function": {
		"name": "submit_plan",
		"description": (
			"Submit your step-by-step plan to the leader for approval. "
			"After submitting, you may only read/analyze until the plan is "
			"approved; bash/write_file/edit_file are blocked. If rejected, "
			"revise and resubmit."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"plan": {
					"type": "string",
					"description": "The complete plan: steps, files to touch, commands to run.",
				},
			},
			"required": ["plan"],
		},
	},
}

# The teammate schema snapshot preserves the legacy visible tool order.
_base_schemas = [
	schema for schema in build_base_registry().schemas
	if schema["function"]["name"] not in _EXCLUDED_NAMES
]
_task_board_schemas = [
	schema for schema in TASK_TOOLS
	if schema["function"]["name"] in _TASK_BOARD_NAMES
]
TEAMMATE_TOOLS = (
	_base_schemas
	+ _task_board_schemas
	+ [SEND_MESSAGE_SCHEMA, SUBMIT_PLAN_SCHEMA]
)


@dataclass
class TeammateState:
	name: str
	require_plan: bool
	messages: list = field(default_factory=list)
	plan_gate: str = GATE_NOT_REQUIRED
	assignment: dict | None = None       # {"task_id": str, "cwd": str}
	work_version: int = 0                # bumped on every claim/release
	todo: TodoManager = field(default_factory=TodoManager)
	agent: object = None
	thread: threading.Thread = None


# ── System prompt ─────────────────────────────────────────────


def teammate_system(state: TeammateState) -> str:
	plan_rules = ""
	if state.require_plan:
		plan_rules = (
			"\nPlan gate: you MUST call submit_plan with your step-by-step plan "
			"and WAIT for leader approval before using bash/write_file/edit_file. "
			"Read-only analysis (read_file, glob, grep) is allowed while waiting. "
			"If the plan is rejected, revise it and resubmit.\n"
		)
	return (
		f"You are teammate '{state.name}', a persistent member of an agent team. "
		f"The leader ('{LEADER}') assigns and coordinates work.\n"
		"You are NOT a one-shot subagent: your conversation context persists "
		"across tasks. After finishing a task you will go idle and may later "
		"receive direct messages or auto-claim ready tasks from the shared "
		"task board.\n\n"
		"Working rules:\n"
		"- You are bound to ONE task at a time. File tools resolve relative "
		"paths in that task's working directory automatically.\n"
		"- When a task is done, call complete_task(task_id, owner=\""
		f"{state.name}\") and end your turn with a clear, self-contained "
		"result summary — it is forwarded to the leader verbatim.\n"
		"- You may claim_task / complete_task / list_task / get_task / "
		"can_start, always with owner=\"" + state.name + "\". Never create, "
		"update, release or reset tasks — the leader owns the task graph.\n"
		"- There is NO interactive user. Operations requiring user approval "
		"are rejected — report blockers to the leader via send_message.\n"
		"- Use send_message(to, content) to reach the leader or another "
		"teammate. Your final answer alone does NOT reach the leader; "
		"complete_task + a clear summary does.\n"
		f"{plan_rules}\n"
		f"Skills available:\n{SKILL_LOADER.catalog()}\n\n"
		"Use load_skill to read the full instructions when a skill applies."
	)


# ── Handler factory ───────────────────────────────────────────


def make_teammate_handlers(state: TeammateState, background_manager) -> dict:
	"""Build the teammate's handler map from the base handlers.

	Workspace tools: assignment check → plan gate → cwd injection.
	claim_task/complete_task: owner is forced to the teammate's name (the
	model cannot claim on behalf of someone else). todo_write binds to the
	teammate's own TodoManager, same pattern as subagents.
	"""
	from codeharness.team.worktree import resolve_worktree_cwd

	base_registry = build_base_registry(
		todo_manager=state.todo,
		background_manager=background_manager,
	)
	handlers = {
		name: handler for name, handler in base_registry.handlers.items()
		if name not in _EXCLUDED_NAMES
	}
	handlers.update({
		name: handler for name, handler in TASK_HANDLERS.items()
		if name in _TASK_BOARD_NAMES
	})

	def _guard(tool_name: str, handler):
		def wrapped(**kwargs):
			assignment = state.assignment
			if assignment is None:
				return (
					"Error: No task assigned; workspace tools are unavailable. "
					"Claim a task first (claim_task) or wait for the leader's "
					"direction. Never assume the repository root."
				)
			if tool_name in WRITE_TOOLS and state.plan_gate in BLOCKING_GATES:
				return (
					f"Blocked: plan status is {state.plan_gate}. Submit a plan "
					"via submit_plan and wait for leader approval before "
					"modifying anything."
				)
			kwargs["cwd"] = assignment["cwd"]
			return handler(**kwargs)
		return wrapped

	for name in WORKSPACE_TOOLS:
		if name in handlers:
			handlers[name] = _guard(name, handlers[name])

	def _claim(task_id: str, owner: str = None) -> str:
		task, cwd, error = TASKS.claim(
			task_id, state.name, worktree_resolver=resolve_worktree_cwd)
		if error:
			return f"Error: {error}"
		_bind_task(state, task, cwd)
		return f"Claimed {task.id} ({task.subject})"

	def _complete(task_id: str, owner: str = None) -> str:
		# Assignment is NOT cleared here: later tools in this round may
		# still need the working directory. Unbinding happens at round end.
		return TASK_HANDLERS["complete_task"](task_id, owner=state.name)

	def _send(to: str, content: str) -> str:
		from codeharness.team.manager import TEAM
		if to == state.name:
			return "Error: cannot send a message to yourself"
		if to == LEADER or to in TEAM.teammate_names():
			BUS.send(state.name, to, content)
			return f"Sent to {to}"
		return f"Error: unknown recipient '{to}'"

	def _submit(plan: str) -> str:
		if state.assignment is None:
			return "Error: no task assigned; there is nothing to plan for"
		if not plan or not plan.strip():
			return "Error: plan cannot be empty"
		req = protocol.create_request(
			"plan_approval_request", state.name, LEADER, plan,
			task_id=state.assignment["task_id"],
			work_version=state.work_version,
		)
		state.plan_gate = GATE_PENDING
		BUS.send(state.name, LEADER, plan, "plan_approval_request",
				 metadata={"request_id": req.request_id,
						   "task_id": state.assignment["task_id"],
						   "work_version": state.work_version})
		return ("Plan submitted; waiting for leader approval. You may keep "
				"analyzing (read-only) but MUST NOT modify anything until "
				"the plan is approved.")

	handlers["claim_task"] = _claim
	handlers["complete_task"] = _complete
	handlers["send_message"] = _send
	handlers["submit_plan"] = _submit
	return handlers


def _bind_task(state: TeammateState, task, cwd: str | None) -> None:
	"""Record a freshly claimed task; a new task resets the plan gate."""
	state.assignment = {"task_id": task.id, "cwd": cwd or os.getcwd()}
	state.work_version += 1
	state.plan_gate = GATE_REQUIRED if state.require_plan else GATE_NOT_REQUIRED
	print(f"\033[35m[team:{state.name}] claimed {task.id} "
		  f"(cwd: {state.assignment['cwd']})\033[0m")


# ── Thread main loop ──────────────────────────────────────────


def teammate_main(state: TeammateState) -> None:
	"""WORK → IDLE loop; returns only on graceful shutdown or crash."""
	try:
		while True:
			# ── WORK phase ──
			result = state.agent.agent_loop(state.messages) or "(no summary)"
			_after_work(state, result)

			# ── IDLE phase ──
			action = _idle_loop(state)
			if action is None:  # shutdown handshake completed
				return
	except Exception as e:  # noqa: BLE001 — a teammate crash must not kill the leader
		print(f"\033[31m[team:{state.name}] thread crashed: {e}\033[0m")
		recovered = ""
		if state.assignment is not None:
			# Release the claimed task back to pending so another teammate
			# (or the leader) can pick it up — a crashed teammate never revives.
			task_id = state.assignment["task_id"]
			try:
				task, error = TASKS.release(task_id)
				if error:
					recovered = f" Its task {task_id} was NOT released: {error}."
				else:
					recovered = f" Its task {task_id} was released back to pending."
			except Exception as re:  # noqa: BLE001 — recovery must not mask the crash
				recovered = f" Releasing task {task_id} failed: {re}."
		BUS.send(state.name, LEADER,
				 f"Teammate {state.name} crashed: {e}.{recovered}", "message")
	finally:
		from codeharness.team.manager import TEAM
		TEAM._remove(state.name)
		print(f"\033[35m[team:{state.name}] exited\033[0m")


def _after_work(state: TeammateState, result: str) -> None:
	"""Post-round bookkeeping: auto-complete, unbind, report to the leader."""
	# A pending plan means the model ended its turn awaiting approval —
	# the task is NOT done, so no result/idle yet. The idle loop blocks on
	# the approval response.
	if state.plan_gate == GATE_PENDING:
		return

	note = ""
	if state.assignment is not None:
		task_id = state.assignment["task_id"]
		try:
			task = TASKS.load(task_id)
			if task.status == "in_progress" and task.owner == state.name:
				# The model forgot complete_task; keep the board consistent.
				task.status = "completed"
				TASKS.save(task)
				note = f"\n(auto-completed {task_id}: complete_task was not called)"
		except FileNotFoundError:
			pass
		# Unbind only now — the round is over, no more tool calls will
		# reference the directory. This also invalidates stale approvals.
		state.assignment = None
		state.work_version += 1

	BUS.send(state.name, LEADER, (result + note)[:4000], "result")
	BUS.send(state.name, LEADER, "Waiting for more work.", "idle_notification")


def _idle_loop(state: TeammateState) -> str | None:
	"""Wait until there is work again. Returns 'work' or None (shutdown).

	Messages (shutdown, direct instructions, plan responses) take priority
	over auto-discovered tasks. While still assigned (plan pending), only
	messages are considered — never auto-claim on top of an unfinished task.
	"""
	from codeharness.team.worktree import resolve_worktree_cwd

	while True:
		timeout = None if state.assignment is not None else IDLE_SCAN_INTERVAL
		inbox = BUS.wait_for_messages(state.name, timeout=timeout)

		if inbox:
			for m in inbox:
				mtype = m.get("type", "message")
				if mtype == "shutdown_request":
					_handle_shutdown(state, m)
					return None
				if mtype == "plan_approval_response":
					if _handle_plan_response(state, m):
						return "work"
					continue  # stale/mismatched response — keep waiting
				if mtype in ("message", "result", "idle_notification"):
					# A direct instruction interrupts idle; results from
					# other teammates arrive as plain context.
					state.messages.append({
						"role": "user",
						"content": f"[Message from {m['from']}]: {m['content']}",
					})
					return "work"
			continue

		# No messages → scan the shared board (only when unassigned).
		if state.assignment is None:
			for t in TASKS.scan_ready_tasks():
				task, cwd, error = TASKS.claim(
					t.id, state.name, worktree_resolver=resolve_worktree_cwd)
				if task is not None:
					_bind_task(state, task, cwd)
					state.messages.append({
						"role": "user",
						"content": (f"[Auto-claimed task {task.id}] {task.subject}\n"
									f"{task.description}"),
					})
					return "work"
				# Claim lost to a race or blocked — try the next candidate.


def _handle_shutdown(state: TeammateState, m: dict) -> None:
	"""Graceful shutdown: acknowledge the request and let the thread exit."""
	request_id = m.get("metadata", {}).get("request_id")
	if request_id:
		protocol.resolve(request_id, "approved")
	BUS.send(state.name, LEADER,
			 f"Teammate {state.name} finished its current step and shut down.",
			 "shutdown_response",
			 metadata={"request_id": request_id})


def _handle_plan_response(state: TeammateState, m: dict) -> bool:
	"""Apply a plan approval response; False if stale/mismatched.

	The approval is only valid if the teammate is still on the exact task
	and generation (work_version) the plan was submitted for. Claiming or
	releasing a task bumps work_version, invalidating old approvals.
	"""
	metadata = m.get("metadata", {})
	request_id = metadata.get("request_id")
	req = protocol.match_response(request_id, "plan_approval_request")
	if req is None:
		print(f"\033[33m[team:{state.name}] stale plan response "
			  f"{request_id} ignored\033[0m")
		return False

	current_task = (state.assignment or {}).get("task_id")
	if req.task_id != current_task or req.work_version != state.work_version:
		protocol.resolve(request_id, "rejected")
		print(f"\033[33m[team:{state.name}] plan response {request_id} "
			  "does not match current task/work_version; invalidated\033[0m")
		return False

	approved = metadata.get("approve") is True
	feedback = metadata.get("feedback", "")
	protocol.resolve(request_id, "approved" if approved else "rejected")
	if approved:
		state.plan_gate = GATE_APPROVED
		state.messages.append({
			"role": "user",
			"content": (f"[Plan approved by leader] Proceed with execution. "
						f"{feedback}").strip(),
		})
	else:
		state.plan_gate = GATE_REJECTED
		state.messages.append({
			"role": "user",
			"content": (f"[Plan rejected by leader] {feedback}\nRevise the "
						"plan and resubmit via submit_plan."),
		})
	return True
