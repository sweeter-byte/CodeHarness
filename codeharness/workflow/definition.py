"""Workflow definition data model — orchestration primitives.

A Workflow is a static execution plan composed of Phases, each containing
Steps. Steps are the atomic units of work:

  AgentStep     → LLM-driven intelligent node (probabilistic)
  ToolStep      → Direct tool handler call (deterministic, no LLM)
  ParallelStep  → Fork-join: branches execute concurrently
  PipelineStep  → Multiple items flow through the same stages independently
  WorkflowStep  → Nested invocation of another registered Workflow

The model is pure data (no execution logic). Runtime interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Step Types ────────────────────────────────────────────────


class StepKind(str, Enum):
	AGENT = "agent"
	TOOL = "tool"
	PARALLEL = "parallel"
	PIPELINE = "pipeline"
	WORKFLOW = "workflow"


@dataclass
class AgentStep:
	"""A single LLM-driven intelligent step within a Workflow."""

	label: str
	prompt_template: str
	output_key: str
	input_keys: list[str] = field(default_factory=list)
	output_schema: dict[str, Any] | None = None
	system_prompt: str | None = None       # custom system prompt (benchmark-agent)
	max_rounds: int = 10
	max_retries: int = 1                   # schema validation retry count
	tools: list[str] | None = None         # tool whitelist (None = base tools)
	timeout: int = 300
	condition: str | None = None           # skip condition expression

	@property
	def kind(self) -> StepKind:
		return StepKind.AGENT


@dataclass
class ToolStep:
	"""A deterministic step that calls a registered tool handler directly."""

	label: str
	tool_name: str
	args_template: dict[str, Any]
	output_key: str
	timeout: int = 120
	allow_failure: bool = False
	condition: str | None = None

	@property
	def kind(self) -> StepKind:
		return StepKind.TOOL


@dataclass
class ParallelStep:
	"""Fork-join: all branches execute concurrently, then merge."""

	label: str
	branches: list[AgentStep | ToolStep]
	output_key: str
	join_mode: str = "all"                 # "all" | "any"
	condition: str | None = None

	@property
	def kind(self) -> StepKind:
		return StepKind.PARALLEL


@dataclass
class PipelineStep:
	"""Multiple items flow through the same stages independently.

	Within one item, stages execute sequentially.
	Across items, execution is concurrent (up to max_item_concurrency).
	"""

	label: str
	items_key: str
	stages: list[AgentStep | ToolStep]
	output_key: str
	max_item_concurrency: int = 4
	condition: str | None = None

	@property
	def kind(self) -> StepKind:
		return StepKind.PIPELINE


@dataclass
class WorkflowStep:
	"""Nested invocation of another registered Workflow."""

	label: str
	workflow_name: str
	inputs_mapping: dict[str, Any]
	output_key: str
	condition: str | None = None

	@property
	def kind(self) -> StepKind:
		return StepKind.WORKFLOW


# Union type for any step
Step = AgentStep | ToolStep | ParallelStep | PipelineStep | WorkflowStep


# ── Phase ─────────────────────────────────────────────────────


@dataclass
class Phase:
	"""An ordered group of steps representing one logical stage."""

	name: str
	steps: list[Step] = field(default_factory=list)


# ── Workflow Config ───────────────────────────────────────────


@dataclass
class WorkflowConfig:
	"""Resource constraints for a single Workflow execution."""

	max_concurrency: int = 4
	max_agent_calls: int = 50
	max_token_budget: int = 500_000
	max_nesting_depth: int = 3
	run_timeout: int = 3600


# ── Workflow Definition ───────────────────────────────────────


@dataclass
class WorkflowDefinition:
	"""Complete static definition of a Workflow.

	Registered in the WorkflowRegistry; the Runtime interprets this
	structure to drive execution.
	"""

	name: str
	description: str
	phases: list[Phase]
	config: WorkflowConfig = field(default_factory=WorkflowConfig)
	inputs_schema: dict[str, Any] = field(default_factory=dict)

	def all_step_labels(self) -> list[str]:
		"""Collect every step label (recursive) for uniqueness checks."""
		labels: list[str] = []
		for phase in self.phases:
			for step in phase.steps:
				labels.extend(_collect_labels(step))
		return labels


def _collect_labels(step: Step) -> list[str]:
	"""Recursively collect labels from a step and its children."""
	labels = [step.label]
	if isinstance(step, ParallelStep):
		for branch in step.branches:
			labels.extend(_collect_labels(branch))
	elif isinstance(step, PipelineStep):
		for stage in step.stages:
			labels.extend(_collect_labels(stage))
	return labels
