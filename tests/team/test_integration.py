"""Integration test for the team runtime with a FakeAgent stand-in.

Run: python3 tests/team/test_integration.py

Covers, without any LLM calls:
  1. spawn: claim-before-start; spawn on a claimed task refused
  2. work phase: cwd injection via wrapper, explicit complete_task,
     result + idle_notification delivery
  3. auto-complete fallback when the model forgets complete_task
  4. auto-claim of a ready task while idle
  5. plan gate: submit_plan → leader approve → write tools unblocked
  6. crash recovery: task released back to pending, registry cleaned up
  7. graceful shutdown handshake + registry cleanup

Threading note: scripted actions must be appended BEFORE the work phase
that consumes them (spawn starts the thread immediately); threading.Event
gates hold a work phase open while the main thread asserts intermediate
state. wait_msg buffers non-matching inbox messages instead of dropping
them (result and idle_notification arrive back-to-back).
"""

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Repo root on sys.path (this script lives inside tests/team/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Isolated workspace BEFORE importing codeharness.tasks/codeharness.team (singletons use
# relative paths resolved against the process cwd).
TMP = tempfile.mkdtemp(prefix="team_test_")
os.chdir(TMP)

# Stub the openai SDK when it is not installed in this environment — the
# FakeAgent below never talks to an LLM, and Agent's real constructor is
# never reached (the fake is injected through TeamManager's agent factory).
try:
	import openai  # noqa: F401
except ImportError:
	import types
	_openai_stub = types.ModuleType("openai")
	class _OpenAIStub:
		def __init__(self, **kwargs):
			pass
	_openai_stub.OpenAI = _OpenAIStub
	sys.modules["openai"] = _openai_stub


# ── FakeAgent: drives the teammate's handlers per a scripted action list ──

SCRIPT = []          # callables(fake_agent, messages) -> str, FIFO across teammates


class FakeAgent:
	def __init__(self, system=None, tools=None, handlers=None,
				 max_rounds=None, todo_manager=None, memory_manager=None,
				 background_manager=None, interactive=True):
		self.system = system
		self.tools = tools
		self.handlers = handlers
		self.interactive = interactive
		assert memory_manager is False, "teammate memory must be disabled"
		assert interactive is False, "teammate must be non-interactive"

	def agent_loop(self, messages):
		action = SCRIPT.pop(0) if SCRIPT else (lambda agent, msgs: "idle turn")
		return action(self, messages)


def extract_task_id(messages):
	"""Pull the assigned task id from an [Assigned/Auto-claimed task] message."""
	for m in reversed(messages):
		mm = re.search(r"\[(?:Assigned|Auto-claimed) task (task_[0-9a-f]+)\]",
					   str(m.get("content", "")))
		if mm:
			return mm.group(1)
	raise AssertionError("no assigned task found in messages")


def wait_for(predicate, timeout=10.0, what="condition"):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.05)
	raise AssertionError(f"timeout waiting for {what}")


def main():
	import codeharness.team  # noqa: F401  (registers everything)
	from codeharness.team import TEAM, BUS, LEADER
	from codeharness.tasks import TASKS
	from codeharness.team.worktree import configure_worktrees
	from codeharness.team import tools as team_tools

	TASKS.set_directory(Path(TMP) / ".codeharness/state/tasks")
	BUS.configure(Path(TMP) / ".codeharness/runtime/team/mailboxes")
	configure_worktrees(Path(TMP), Path(TMP) / ".codeharness/worktrees")
	TEAM.set_agent_factory(FakeAgent)

	# Buffer for inbox messages not matching the waited type yet.
	pending: list[dict] = []

	def wait_msg(msg_type, timeout=10.0):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			pending.extend(BUS.read_inbox(LEADER))
			for i, m in enumerate(pending):
				if m["type"] == msg_type:
					return pending.pop(i)
			time.sleep(0.05)
		raise AssertionError(f"timeout waiting for {msg_type}")

	try:
		# ── 1+2. spawn + first work phase (gated) ────────────────
		release_work_one = threading.Event()

		def work_one(agent, messages):
			# Hold this work phase open until the main thread has asserted
			# the intermediate in_progress state.
			assert release_work_one.wait(timeout=10)
			# bash goes through the wrapper: cwd injected from the assignment
			out = agent.handlers["bash"](command="pwd")
			assert TMP in out, out
			# explicit completion (no auto-complete expected)
			tid = extract_task_id(messages)
			out = agent.handlers["complete_task"](task_id=tid, owner="Alice")
			assert out.startswith(f"Completed {tid}"), out
			return "all done with task one"

		SCRIPT.append(work_one)  # BEFORE spawn: the thread starts immediately

		t1 = TASKS.create("task one", "first job")
		result = team_tools.run_spawn_teammate(t1.id, name="Alice")
		assert "Spawned teammate Alice" in result, result
		state = TEAM.get_state("Alice")
		assert state.assignment["task_id"] == t1.id
		assert state.assignment["cwd"] == os.getcwd()
		task = TASKS.load(t1.id)
		assert task.status == "in_progress" and task.owner == "Alice"
		print("1. spawn claim-before-start OK")

		# spawn on an already-claimed task must fail and spawn nothing
		result = team_tools.run_spawn_teammate(t1.id, name="Bob")
		assert result.startswith("Error:"), result
		assert TEAM.get_state("Bob") is None
		print("   spawn-on-claimed-task refused OK")

		release_work_one.set()
		result_msg = wait_msg("result")
		assert "all done with task one" in result_msg["content"]
		assert "auto-completed" not in result_msg["content"]
		assert result_msg["from"] == "Alice"
		idle_msg = wait_msg("idle_notification")
		assert idle_msg["from"] == "Alice"
		assert TASKS.load(t1.id).status == "completed"
		print("2. cwd injection + result/idle/explicit-complete OK")

		# ── 3. auto-complete fallback ────────────────────────────
		SCRIPT.append(lambda agent, messages: "finished but forgot complete_task")
		t1b = TASKS.create("task one-b", "forgot to complete")
		team_tools.run_spawn_teammate(t1b.id, name="Carol")
		result_msg = wait_msg("result")
		assert "auto-completed" in result_msg["content"], result_msg["content"]
		wait_msg("idle_notification")
		assert TASKS.load(t1b.id).status == "completed"
		team_tools.run_shutdown_teammate("Carol")
		wait_for(lambda: TEAM.get_state("Carol") is None, what="Carol exit")
		print("3. auto-complete fallback OK")

		# ── 4. auto-claim while idle ─────────────────────────────
		# The gated action holds the work phase open so the main thread can
		# observe the (otherwise milliseconds-short) assignment window.
		release_work_two = threading.Event()

		def work_two(agent, messages):
			assert release_work_two.wait(timeout=10)
			return "task two done"

		SCRIPT.append(work_two)
		t2 = TASKS.create("task two", "second job")
		wait_for(lambda: (st := TEAM.get_state("Alice")) is not None
					 and st.assignment is not None,
				 what="auto-claim of task two")
		assert TEAM.get_state("Alice").assignment["task_id"] == t2.id
		assert "[Auto-claimed task" in \
			str(TEAM.get_state("Alice").messages[-1]["content"])
		release_work_two.set()
		result_msg = wait_msg("result")
		assert "task two done" in result_msg["content"]
		wait_msg("idle_notification")
		assert TASKS.load(t2.id).status == "completed"
		print("4. auto-claim OK")

		# ── 5. plan gate lifecycle ───────────────────────────────
		def try_bash_and_submit(agent, messages):
			blocked = agent.handlers["bash"](command="echo hi")
			assert blocked.startswith("Blocked: plan status is required"), blocked
			ro = agent.handlers["read_file"](path="no_such_file.txt")
			assert ro.startswith("Error: File not found"), ro  # reads allowed
			out = agent.handlers["submit_plan"](plan="1. read 2. edit 3. test")
			assert "waiting for leader approval" in out
			return "plan submitted"

		SCRIPT.append(try_bash_and_submit)
		t3 = TASKS.create("task three", "risky refactor")
		team_tools.run_spawn_teammate(t3.id, name="Bob", require_plan=True)
		bob = TEAM.get_state("Bob")
		# (gate == required is proven inside try_bash_and_submit: the bash
		# call returns 'Blocked: plan status is required')
		plan_msg = wait_msg("plan_approval_request")
		assert plan_msg["from"] == "Bob"
		request_id = plan_msg["metadata"]["request_id"]
		wait_for(lambda: bob.plan_gate == "pending", what="gate pending")

		def bash_after_approval(agent, messages):
			out = agent.handlers["bash"](command="echo working")
			assert "working" in out, out  # gate approved → wrapper passes
			agent.handlers["complete_task"](task_id=extract_task_id(messages),
											owner="Bob")
			return "refactor done"

		SCRIPT.append(bash_after_approval)
		approval = team_tools.run_approve_plan(request_id, approve=True,
											   feedback="go ahead")
		assert approval.startswith("Sent approval"), approval
		wait_for(lambda: bob.plan_gate == "approved", what="gate approval")
		result_msg = wait_msg("result")
		assert "refactor done" in result_msg["content"]
		assert "auto-completed" not in result_msg["content"]
		wait_msg("idle_notification")
		assert TASKS.load(t3.id).status == "completed"
		print("5. plan gate lifecycle OK")

		# ── 6. crash recovery: claimed task released, teammate removed ─
		# Alice is idle here and would instantly auto-claim the released task,
		# racing the pending-state assertion — shut her down first.
		team_tools.run_shutdown_teammate("Alice")
		wait_for(lambda: TEAM.get_state("Alice") is None, what="Alice exit")

		def boom(agent, messages):
			raise RuntimeError("simulated crash mid-task")

		SCRIPT.append(boom)
		t4 = TASKS.create("task four", "will crash")
		team_tools.run_spawn_teammate(t4.id, name="Dave")
		crash_msg = wait_msg("message")
		assert crash_msg["from"] == "Dave"
		assert "crashed: simulated crash mid-task" in crash_msg["content"]
		assert f"released back to pending" in crash_msg["content"], crash_msg["content"]
		task = TASKS.load(t4.id)
		assert task.status == "pending" and task.owner is None, (task.status, task.owner)
		wait_for(lambda: TEAM.get_state("Dave") is None, what="Dave exit")
		print("6. crash recovery OK")

		# ── 7. graceful shutdown handshake ───────────────────────
		assert "Shutdown request sent" in team_tools.run_shutdown_teammate("Bob")
		wait_for(lambda: TEAM.get_state("Alice") is None, what="Alice exit")
		wait_for(lambda: TEAM.get_state("Bob") is None, what="Bob exit")
		wait_for(lambda: not any(t.is_alive() for t in threading.enumerate()
								  if t.name.startswith("teammate-")),
				 what="teammate threads exit")
		assert team_tools.run_list_teammates() == "(no teammates)"
		assert team_tools.run_send_message("Alice", "hi").startswith("Error")
		print("7. graceful shutdown OK")

		print("\nALL TEAM RUNTIME CHECKS PASSED")
	finally:
		# Detach any leftover teammate threads before removing the temp dir.
		for s in TEAM.list_states():
			BUS.send(LEADER, s.name, "stop", "shutdown_request",
					 metadata={"request_id": "cleanup"})
		time.sleep(0.3)
		shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
	main()
