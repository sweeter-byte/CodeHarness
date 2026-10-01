"""Goal Loop state definitions — data structures and enums.

Goal Loop adds an exit-gate to the Agent Loop: when the model stops
calling tools, an independent Evaluator checks whether the user-defined
Completion Condition has been satisfied before allowing the loop to exit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class GoalStatus(str, Enum):
	"""Lifecycle status of a session-scoped Goal."""

	ACTIVE = "active"       # Goal is being worked on
	ACHIEVED = "achieved"   # Evaluator confirmed completion
	FAILED = "failed"       # Evaluator determined goal is impossible
	CLEARED = "cleared"     # User explicitly cleared the goal


class StopDecision(str, Enum):
	"""Result of the Goal Gate evaluation — tells agent_loop what to do."""

	ALLOW = "allow"         # No active goal; normal exit
	ACHIEVED = "achieved"   # Goal completed; safe to exit
	BLOCK = "block"         # Goal not yet met; continue working
	FAILED = "failed"       # Goal impossible; exit with report
	DEFER = "defer"         # Background tasks running; postpone evaluation
	ERROR = "error"         # Evaluator itself failed; fail-closed exit
	LIMIT = "limit"         # Safety limit reached; stop auto-continuation


@dataclass
class EvalResult:
	"""Structured output from the Goal Evaluator LLM call."""

	ok: bool
	reason: str
	impossible: bool = False


@dataclass
class GoalState:
	"""Session-scoped goal state maintained by GoalController."""

	condition: str                          # User's completion condition
	created_at: float = field(default_factory=time.time)
	status: GoalStatus = GoalStatus.ACTIVE
	evaluations: int = 0                    # Total evaluator calls
	consecutive_blocks: int = 0             # Consecutive "not done" results
	last_reason: str = ""                   # Most recent evaluator reason
	last_decision: StopDecision | None = None

	def to_dict(self) -> dict:
		"""Serialize for status queries."""
		return {
			"condition": self.condition,
			"status": self.status.value,
			"created_at": self.created_at,
			"evaluations": self.evaluations,
			"consecutive_blocks": self.consecutive_blocks,
			"last_reason": self.last_reason,
			"last_decision": self.last_decision.value if self.last_decision else None,
		}
