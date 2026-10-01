"""Goal Loop — exit-gate mechanism for the Agent Loop.

Adds an independent completion evaluation layer: when the model stops
calling tools, a Goal Evaluator judges whether the user-defined
Completion Condition has been satisfied before allowing the loop to exit.

Public API:
    GoalController   — orchestrates the Goal Gate (set/clear/evaluate)
    GoalEvaluator    — independent LLM-based completion judge
    GoalState        — session-scoped goal data
    GoalStatus       — goal lifecycle enum
    StopDecision     — gate result enum
    EvalResult       — evaluator structured output
"""

from codeharness.goal.state import EvalResult, GoalState, GoalStatus, StopDecision
from codeharness.goal.evaluator import EvaluatorError, GoalEvaluator
from codeharness.goal.controller import GoalController
from codeharness.goal.tools import GOAL_TOOLS, make_goal_handlers

__all__ = [
	"EvalResult",
	"EvaluatorError",
	"GOAL_TOOLS",
	"GoalController",
	"GoalEvaluator",
	"GoalState",
	"GoalStatus",
	"StopDecision",
	"make_goal_handlers",
]
