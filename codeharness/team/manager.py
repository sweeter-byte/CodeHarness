"""TeamManager — teammate lifecycle orchestration.

Holds the in-process registry name → TeammateState. Spawn enforces the
"claim before start" rule: the initial task is atomically claimed for the
teammate FIRST; only a successful claim starts the thread, so a teammate
never runs without a bound task. Shutdown goes through the structured
handshake — the thread is never killed.
"""

import threading

from codeharness.tasks import TASKS, AGENT_NAMES
from codeharness.background import BackgroundManager
from codeharness.team.bus import BUS, LEADER
from codeharness.team import protocol
from codeharness.team.teammate import (
	TeammateState, TEAMMATE_MAX_ROUNDS, TEAMMATE_TOOLS,
	teammate_system, make_teammate_handlers, teammate_main,
)


class TeamManager:
	def __init__(self):
		self._states: dict[str, TeammateState] = {}
		self._lock = threading.Lock()
		self._agent_factory = None

	def set_agent_factory(self, factory) -> None:
		"""Configure the Runtime-owned factory used for teammate agents."""
		self._agent_factory = factory

	# ── registry helpers ──

	def teammate_names(self) -> list[str]:
		with self._lock:
			return list(self._states.keys())

	def list_states(self) -> list[TeammateState]:
		with self._lock:
			return list(self._states.values())

	def get_state(self, name: str) -> TeammateState | None:
		with self._lock:
			return self._states.get(name)

	def _remove(self, name: str) -> None:
		"""Called by the teammate thread itself when it exits."""
		with self._lock:
			self._states.pop(name, None)

	def _allocate_name(self) -> str | None:
		"""Pick an unused pool name; avoid live teammates AND owners of
		in-progress tasks (a crashed teammate's task may still hold the name)."""
		busy = set(self.teammate_names())
		busy.update(t.owner for t in TASKS.list_all()
					if t.owner and t.status == "in_progress")
		for name in AGENT_NAMES:
			if name not in busy and name != LEADER:
				return name
		return None

	# ── lifecycle ──

	def spawn(self, task_id: str, name: str | None = None,
			  require_plan: bool = False) -> str:
		"""Claim the initial task, then start the teammate thread."""
		if self._agent_factory is None:
			return "Error: agent factory is not configured; start CodeHarness first"

		if name is not None:
			name = name.strip()
			if not name:
				return "Error: teammate name cannot be empty"
			if name == LEADER:
				return f"Error: '{LEADER}' is the reserved leader mailbox"
			if not name.replace("_", "").replace("-", "").isalnum():
				return f"Error: invalid teammate name '{name}'"
		else:
			name = self._allocate_name()
			if name is None:
				return "Error: no free teammate names (pool exhausted)"

		with self._lock:
			if name in self._states:
				return f"Error: teammate '{name}' already exists"

		# Claim BEFORE starting the thread — a failed claim must not spawn
		# a task-less teammate.
		from codeharness.team.worktree import resolve_worktree_cwd
		task, cwd, error = TASKS.claim(
			task_id, name, worktree_resolver=resolve_worktree_cwd)
		if error:
			return f"Error: cannot spawn '{name}': {error}"

		state = TeammateState(name=name, require_plan=require_plan)
		from codeharness.team.teammate import _bind_task
		_bind_task(state, task, cwd)
		state.messages.append({
			"role": "user",
			"content": (f"[Assigned task {task.id}] {task.subject}\n"
						f"{task.description}"),
		})

		teammate_bg = BackgroundManager()
		state.agent = self._agent_factory(
			system=teammate_system(state),
			tools=list(TEAMMATE_TOOLS),
			handlers=make_teammate_handlers(state, teammate_bg),
			max_rounds=TEAMMATE_MAX_ROUNDS,
			todo_manager=state.todo,
			memory_manager=False,          # leader owns cross-session memory
			background_manager=teammate_bg,
			interactive=False,             # never prompt input() off-thread
		)

		state.thread = threading.Thread(
			target=teammate_main, args=(state,),
			daemon=True, name=f"teammate-{name}",
		)
		with self._lock:
			self._states[name] = state
		state.thread.start()
		print(f"\033[35m[team] spawned {name} on {task.id}\033[0m")
		return (f"Spawned teammate {name} on task {task.id} "
				f"(cwd: {state.assignment['cwd']}, "
				f"require_plan={require_plan})")

	def shutdown(self, name: str) -> str:
		"""Send a graceful shutdown request; the teammate responds after
		finishing its current step (handshake, never a thread kill)."""
		state = self.get_state(name)
		if state is None:
			return f"Error: no such teammate '{name}'"
		req = protocol.create_request(
			"shutdown_request", LEADER, name, "shutdown requested by leader")
		BUS.send(LEADER, name,
				 "Leader requested shutdown. Finish your current step and exit.",
				 "shutdown_request",
				 metadata={"request_id": req.request_id})
		return (f"Shutdown request sent to {name} "
				f"(request_id={req.request_id}); it will exit after finishing "
				"its current step and respond via shutdown_response.")

	def status(self) -> str:
		"""Human-readable table of live teammates."""
		states = self.list_states()
		if not states:
			return "(no teammates)"
		lines = []
		for s in states:
			task = s.assignment["task_id"] if s.assignment else "-"
			cwd = s.assignment["cwd"] if s.assignment else "-"
			lines.append(
				f"{s.name:<10} | gate={s.plan_gate:<12} | task={task:<15} "
				f"| cwd={cwd}")
		return "\n".join(lines)


# Module-level singleton: leader tools, teammates and the wakeup thread
# all coordinate through this one registry.
TEAM = TeamManager()
