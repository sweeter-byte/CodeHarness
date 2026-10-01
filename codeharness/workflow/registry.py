"""WorkflowRegistry — registration, validation and lookup of Workflow definitions.

Follows the same design philosophy as ToolRegistry: lightweight, insertion-ordered,
duplicate-name detection, Fail Fast validation. The registry is populated at
startup by the host (never by the model at runtime).
"""

from __future__ import annotations

import re
from typing import Any

from codeharness.workflow.definition import (
	AgentStep,
	ParallelStep,
	PipelineStep,
	Phase,
	ToolStep,
	WorkflowDefinition,
	WorkflowStep,
)

# Workflow names participate in file paths and run identifiers.
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class WorkflowValidationError(ValueError):
	"""Raised when a WorkflowDefinition fails pre-registration validation."""


class DuplicateWorkflowError(ValueError):
	"""Raised when a workflow name is registered twice."""


class WorkflowRegistry:
	"""Name → WorkflowDefinition lookup, insertion-ordered.

	Validates definitions eagerly on register() so configuration errors
	surface immediately (Fail Fast), never at execution time.
	"""

	def __init__(self) -> None:
		self._workflows: dict[str, WorkflowDefinition] = {}

	def register(self, definition: WorkflowDefinition) -> None:
		"""Validate and register a workflow. Raises on any problem."""
		validate_definition(definition)
		if definition.name in self._workflows:
			raise DuplicateWorkflowError(
				f"Workflow '{definition.name}' is already registered"
			)
		self._workflows[definition.name] = definition

	def get(self, name: str) -> WorkflowDefinition | None:
		"""Lookup by name; None if not found."""
		return self._workflows.get(name)

	def list_all(self) -> list[WorkflowDefinition]:
		"""All registered workflows in insertion order."""
		return list(self._workflows.values())

	def names(self) -> list[str]:
		return list(self._workflows.keys())

	def catalog(self) -> str:
		"""One-line-per-workflow summary for system prompt injection."""
		if not self._workflows:
			return "(no workflows available)"
		lines = [
			f"- {wf.name}: {wf.description}"
			for wf in self._workflows.values()
		]
		return "\n".join(lines)

	def __len__(self) -> int:
		return len(self._workflows)

	def __contains__(self, name: str) -> bool:
		return name in self._workflows


# ── Validation ────────────────────────────────────────────────


def validate_definition(definition: WorkflowDefinition) -> None:
	"""Pre-registration validation. Raises WorkflowValidationError."""
	_validate_name(definition.name)
	_validate_phases(definition)
	_validate_labels_unique(definition)
	_validate_config(definition.config)


def _validate_name(name: str) -> None:
	if not name:
		raise WorkflowValidationError("Workflow name cannot be empty")
	if not _SAFE_NAME_RE.match(name):
		raise WorkflowValidationError(
			f"Workflow name '{name}' is invalid: must match [a-z0-9_-], "
			"start with alphanumeric, max 64 chars"
		)


def _validate_phases(definition: WorkflowDefinition) -> None:
	if not definition.phases:
		raise WorkflowValidationError(
			f"Workflow '{definition.name}' must have at least one phase"
		)
	for phase in definition.phases:
		if not phase.name:
			raise WorkflowValidationError(
				f"Workflow '{definition.name}' has a phase with empty name"
			)
		if not phase.steps:
			raise WorkflowValidationError(
				f"Phase '{phase.name}' in workflow '{definition.name}' has no steps"
			)
		for step in phase.steps:
			_validate_step(step, definition.name, phase.name)


def _validate_step(step: Any, wf_name: str, phase_name: str) -> None:
	"""Validate a single step (recursive for composite steps)."""
	if not step.label:
		raise WorkflowValidationError(
			f"Step in phase '{phase_name}' of '{wf_name}' has empty label"
		)
	if isinstance(step, AgentStep):
		if not step.prompt_template:
			raise WorkflowValidationError(
				f"AgentStep '{step.label}' in '{wf_name}' has empty prompt_template"
			)
		if not step.output_key:
			raise WorkflowValidationError(
				f"AgentStep '{step.label}' in '{wf_name}' has empty output_key"
			)
		if step.max_retries < 0:
			raise WorkflowValidationError(
				f"AgentStep '{step.label}' in '{wf_name}': max_retries cannot be negative"
			)
	elif isinstance(step, ToolStep):
		if not step.tool_name:
			raise WorkflowValidationError(
				f"ToolStep '{step.label}' in '{wf_name}' has empty tool_name"
			)
		if not step.output_key:
			raise WorkflowValidationError(
				f"ToolStep '{step.label}' in '{wf_name}' has empty output_key"
			)
	elif isinstance(step, ParallelStep):
		if not step.branches:
			raise WorkflowValidationError(
				f"ParallelStep '{step.label}' in '{wf_name}' has no branches"
			)
		for branch in step.branches:
			_validate_step(branch, wf_name, phase_name)
		if step.join_mode not in ("all", "any"):
			raise WorkflowValidationError(
				f"ParallelStep '{step.label}' in '{wf_name}': "
				f"join_mode must be 'all' or 'any', got '{step.join_mode}'"
			)
	elif isinstance(step, PipelineStep):
		if not step.items_key:
			raise WorkflowValidationError(
				f"PipelineStep '{step.label}' in '{wf_name}' has empty items_key"
			)
		if not step.stages:
			raise WorkflowValidationError(
				f"PipelineStep '{step.label}' in '{wf_name}' has no stages"
			)
		for stage in step.stages:
			_validate_step(stage, wf_name, phase_name)
	elif isinstance(step, WorkflowStep):
		if not step.workflow_name:
			raise WorkflowValidationError(
				f"WorkflowStep '{step.label}' in '{wf_name}' has empty workflow_name"
			)


def _validate_labels_unique(definition: WorkflowDefinition) -> None:
	labels = definition.all_step_labels()
	seen: set[str] = set()
	for label in labels:
		if label in seen:
			raise WorkflowValidationError(
				f"Duplicate step label '{label}' in workflow '{definition.name}'"
			)
		seen.add(label)


def _validate_config(config: Any) -> None:
	if config.max_concurrency < 1:
		raise WorkflowValidationError("max_concurrency must be >= 1")
	if config.max_agent_calls < 1:
		raise WorkflowValidationError("max_agent_calls must be >= 1")
	if config.max_token_budget < 1:
		raise WorkflowValidationError("max_token_budget must be >= 1")
	if config.max_nesting_depth < 1:
		raise WorkflowValidationError("max_nesting_depth must be >= 1")
	if config.run_timeout < 1:
		raise WorkflowValidationError("run_timeout must be >= 1")
