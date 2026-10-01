"""GoalController — the Goal Gate orchestrator.

Sits between the Agent Loop's exit point and the actual return.
When the model stops calling tools, the controller decides whether
the session goal has been met, needs more work, or cannot proceed.

Lifecycle:
  set_goal() → active
  evaluate_after_turn() → StopDecision
  clear_goal() → cleared
"""

from __future__ import annotations

from typing import Any

from codeharness.goal.evaluator import EvaluatorError, GoalEvaluator
from codeharness.goal.state import EvalResult, GoalState, GoalStatus, StopDecision


# ── Safety Defaults ──────────────────────────────────────────

DEFAULT_MAX_CONSECUTIVE_BLOCKS = 5
DEFAULT_MAX_EVALUATIONS = 10


class GoalController:
	"""Manages the session-scoped Goal lifecycle and Gate decisions.

	Only the Leader Agent holds a real GoalController instance.
	SubAgents / Teammates / Workflow agents use GoalController.null()
	which always returns ALLOW (no goal gate).
	"""

	def __init__(
		self,
		evaluator: GoalEvaluator | None = None,
		max_blocks: int = DEFAULT_MAX_CONSECUTIVE_BLOCKS,
		max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
		status_handler: Any = None,
	):
		self._evaluator = evaluator
		self._goal: GoalState | None = None
		self._max_blocks = max_blocks
		self._max_evaluations = max_evaluations
		self._status_handler = status_handler

	@classmethod
	def null(cls) -> "GoalController":
		"""Create a no-op controller that always allows exit.

		Used by SubAgent, Teammate, and Workflow agent instances
		where Goal Loop should not interfere.
		"""
		controller = cls(evaluator=None)
		controller._null_mode = True
		return controller

	# ── Goal Lifecycle ───────────────────────────────────────

	def set_goal(self, condition: str) -> str:
		"""Set or replace the active goal. Returns status message."""
		if not condition or not condition.strip():
			return "Error: Goal condition cannot be empty."

		self._goal = GoalState(condition=condition.strip())
		self._emit(
			f"\033[32m[Goal] 🎯 set: {condition.strip()}\033[0m"
		)
		return f"Goal set: {condition.strip()}"

	def clear_goal(self) -> str:
		"""Clear the active goal (user-initiated)."""
		if self._goal is None:
			return "No active goal to clear."

		old_condition = self._goal.condition
		self._goal.status = GoalStatus.CLEARED
		self._goal = None
		self._emit(f"\033[33m[Goal] ✗ cleared: {old_condition}\033[0m")
		return f"Goal cleared: {old_condition}"

	def get_status(self) -> dict:
		"""Return current goal state for queries."""
		if self._goal is None:
			return {"active": False, "message": "No active goal."}
		result = self._goal.to_dict()
		result["active"] = self._goal.status == GoalStatus.ACTIVE
		return result

	@property
	def has_active_goal(self) -> bool:
		"""Whether there is a goal in ACTIVE status."""
		return (
			self._goal is not None
			and self._goal.status == GoalStatus.ACTIVE
		)

	@property
	def goal_condition(self) -> str | None:
		"""Current goal condition text, or None."""
		return self._goal.condition if self._goal else None

	# ── Goal Gate ────────────────────────────────────────────

	def evaluate_after_turn(
		self,
		messages: list[dict],
		background_manager: Any = None,
	) -> StopDecision:
		"""Core method: called when the model wants to stop.

		Returns a StopDecision telling agent_loop what to do next.
		"""
		# Null mode or no active goal → always allow
		if getattr(self, "_null_mode", False) or self._goal is None:
			return StopDecision.ALLOW
		if self._goal.status != GoalStatus.ACTIVE:
			return StopDecision.ALLOW

		# Check if background tasks are still running → defer
		if background_manager is not None and self._has_running_background(background_manager):
			self._emit(
				"\033[33m[Goal] ⏸ deferred: background tasks still running\033[0m"
			)
			self._goal.last_decision = StopDecision.DEFER
			return StopDecision.DEFER

		# Safety limits
		if self._goal.consecutive_blocks >= self._max_blocks:
			self._emit(
				f"\033[31m[Goal] ⚠ limit: {self._goal.consecutive_blocks} "
				f"consecutive blocks reached\033[0m"
			)
			self._goal.last_decision = StopDecision.LIMIT
			return StopDecision.LIMIT

		if self._goal.evaluations >= self._max_evaluations:
			self._emit(
				f"\033[31m[Goal] ⚠ limit: {self._goal.evaluations} "
				f"evaluations reached\033[0m"
			)
			self._goal.last_decision = StopDecision.LIMIT
			return StopDecision.LIMIT

		# No evaluator configured → allow (fail-open for misconfiguration)
		if self._evaluator is None:
			return StopDecision.ALLOW

		# Run evaluation
		self._goal.evaluations += 1
		try:
			result: EvalResult = self._evaluator.evaluate(
				self._goal.condition, messages
			)
		except EvaluatorError as exc:
			# Fail closed: evaluator error → stop auto-continuation
			self._emit(
				f"\033[31m[Goal] ⚠ evaluator error: {exc}\033[0m"
			)
			self._goal.last_reason = f"Evaluator error: {exc}"
			self._goal.last_decision = StopDecision.ERROR
			return StopDecision.ERROR
		except Exception as exc:
			self._emit(
				f"\033[31m[Goal] ⚠ unexpected evaluator error: {exc}\033[0m"
			)
			self._goal.last_reason = f"Unexpected error: {exc}"
			self._goal.last_decision = StopDecision.ERROR
			return StopDecision.ERROR

		self._goal.last_reason = result.reason

		# Goal achieved
		if result.ok:
			self._goal.status = GoalStatus.ACHIEVED
			self._goal.consecutive_blocks = 0
			self._goal.last_decision = StopDecision.ACHIEVED
			self._emit(
				f"\033[32m[Goal] ✓ achieved: {result.reason}\033[0m"
			)
			return StopDecision.ACHIEVED

		# Goal impossible
		if result.impossible:
			self._goal.status = GoalStatus.FAILED
			self._goal.consecutive_blocks = 0
			self._goal.last_decision = StopDecision.FAILED
			self._emit(
				f"\033[31m[Goal] ✗ impossible: {result.reason}\033[0m"
			)
			return StopDecision.FAILED

		# Goal not yet met → block and continue
		self._goal.consecutive_blocks += 1
		self._goal.last_decision = StopDecision.BLOCK
		self._emit(
			f"\033[33m[Goal] ✗ not yet: {result.reason} "
			f"({self._goal.consecutive_blocks}/{self._max_blocks})\033[0m"
		)
		return StopDecision.BLOCK

	# ── Feedback Message ─────────────────────────────────────

	def build_feedback_message(self) -> str:
		"""Construct the message injected when BLOCK forces continuation.

		Tells the worker what is still missing so it can take action.
		"""
		if self._goal is None:
			return ""

		parts = [
			"<goal_gate>",
			f"Goal NOT achieved yet.",
			f"Completion condition: {self._goal.condition}",
			f"Evaluator feedback: {self._goal.last_reason}",
			"",
			"Continue working to satisfy the completion condition.",
			"Run verification commands (tests, lint, typecheck, etc.) "
			"to produce observable evidence of completion.",
			"</goal_gate>",
		]
		return "\n".join(parts)

	# ── Internal Helpers ─────────────────────────────────────

	@staticmethod
	def _has_running_background(background_manager: Any) -> bool:
		"""Check if BackgroundManager has any tasks still running."""
		try:
			tasks = background_manager.tasks
			return any(
				t.get("status") == "running" for t in tasks.values()
			)
		except (AttributeError, TypeError):
			return False

	def _emit(self, message: str) -> None:
		"""Output a status message via handler or print."""
		if self._status_handler is not None:
			self._status_handler(message)
		else:
			print(message)
