"""Tool schemas and handlers for the Goal Loop.

Provides goal_set, goal_clear, and goal_status tools that let the model
(or user via CLI) manage the session completion condition.

Like todo_write, the handlers are bound to a GoalController instance
at registry construction time (not process-global).
"""

from __future__ import annotations

import json
from typing import Callable

from codeharness.goal.controller import GoalController


# ── Tool Schemas ─────────────────────────────────────────────

GOAL_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "goal_set",
			"description": (
				"Set a session-level completion condition (Goal). "
				"The agent loop will NOT exit until an independent evaluator "
				"confirms the goal is satisfied based on observable evidence "
				"in the transcript (e.g. test exit code = 0, lint passes). "
				"Use this when the user asks for a result-driven task like "
				"'fix until tests pass' or 'refactor and verify typecheck'."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"condition": {
						"type": "string",
						"description": (
							"The completion condition. Must be specific and verifiable, "
							"e.g. 'pytest tests/ exits with code 0' rather than 'make tests pass'."
						),
					},
				},
				"required": ["condition"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "goal_clear",
			"description": (
				"Clear the active goal, allowing the agent loop to exit normally. "
				"Use when the user explicitly cancels the completion condition or "
				"when continuing toward the goal is no longer meaningful."
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
			"name": "goal_status",
			"description": (
				"Query the current goal state: condition, status, evaluation count, "
				"and last evaluator feedback."
			),
			"parameters": {
				"type": "object",
				"properties": {},
			},
		},
	},
]


# ── Handler Factory ──────────────────────────────────────────

def make_goal_handlers(controller: GoalController) -> dict[str, Callable]:
	"""Build handler functions bound to a specific GoalController."""

	def goal_set(condition: str) -> str:
		return controller.set_goal(condition)

	def goal_clear() -> str:
		return controller.clear_goal()

	def goal_status() -> str:
		status = controller.get_status()
		return json.dumps(status, ensure_ascii=False, indent=2)

	return {
		"goal_set": goal_set,
		"goal_clear": goal_clear,
		"goal_status": goal_status,
	}
