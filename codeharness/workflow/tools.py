"""Tool schemas and handlers for the Workflow system.

Exposes three tools to the Leader Agent:
  - start_workflow:   Launch a registered workflow (non-blocking)
  - resume_workflow:  Resume a failed/interrupted run
  - workflow_status:  Query run status or list all runs

Follows the same pattern as tasks/tools.py: schemas + handler map,
registered by the leader Runtime via registry.extend().
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from codeharness.workflow.runtime import WorkflowRuntime


# ── Tool Handlers ─────────────────────────────────────────────


def _run_start_workflow(
	runtime: WorkflowRuntime, name: str, inputs: str = "{}"
) -> str:
	"""Start a new workflow run."""
	# Parse inputs (may come as JSON string from the model)
	if isinstance(inputs, str):
		try:
			parsed_inputs = json.loads(inputs)
		except json.JSONDecodeError as e:
			return f"Error: invalid JSON for inputs: {e}"
	else:
		parsed_inputs = inputs if isinstance(inputs, dict) else {}

	run_id, error = runtime.start(name, parsed_inputs)
	if error:
		return error
	return (
		f"Workflow '{name}' started (run_id={run_id}). "
		"Use workflow_status to check progress."
	)


def _run_resume_workflow(runtime: WorkflowRuntime, run_id: str) -> str:
	"""Resume a failed or interrupted workflow run."""
	resumed_id, error = runtime.resume(run_id)
	if error:
		return error
	return (
		f"Workflow run '{resumed_id}' resumed. "
		"Previously completed steps will be reused from journal. "
		"Use workflow_status to check progress."
	)


def _run_workflow_status(runtime: WorkflowRuntime, run_id: str = "") -> str:
	"""Query workflow run status."""
	target = run_id.strip() if run_id else None
	result = runtime.status(target)

	if "error" in result:
		return f"Error: {result['error']}"

	# Format for single run
	if target:
		status = result.get("status", "unknown")
		workflow = result.get("workflow_name", "?")
		lines = [
			f"Run: {target}",
			f"Workflow: {workflow}",
			f"Status: {status}",
			f"Phase: {result.get('current_phase', '-')}",
			f"Agent calls: {result.get('agent_calls_used', 0)}",
			f"Tokens: {result.get('tokens_used', 0)}",
			f"Completed steps: {len(result.get('completed_steps', []))}",
		]
		if result.get("error"):
			lines.append(f"Error: {result['error']}")
		if result.get("output"):
			output = result["output"]
			if isinstance(output, dict):
				lines.append(f"Output: {json.dumps(output, ensure_ascii=False, indent=2)[:3000]}")
			else:
				lines.append(f"Output: {str(output)[:3000]}")
		return "\n".join(lines)

	# Format for run list
	runs = result.get("runs", [])
	if not runs:
		return "(no workflow runs)"
	lines = ["run_id | workflow | status | phase | agents | tokens"]
	lines.append("-" * 70)
	for r in runs:
		lines.append(
			f"{r['run_id']:<12} | {r['workflow']:<20} | "
			f"{r['status']:<10} | {r.get('phase', '-'):<12} | "
			f"{r.get('agent_calls', 0):<6} | {r.get('tokens', 0)}"
		)
	return "\n".join(lines)


# ── Tool Schemas ──────────────────────────────────────────────

WORKFLOW_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "start_workflow",
			"description": (
				"Start a registered workflow by name. The workflow runs asynchronously "
				"in a background thread. Returns a run_id for tracking. "
				"Available workflows are listed in the system prompt."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"name": {
						"type": "string",
						"description": "Workflow name (must be pre-registered).",
					},
					"inputs": {
						"type": "string",
						"description": (
							"JSON object of workflow inputs. Keys depend on the "
							"specific workflow (see system prompt for details)."
						),
					},
				},
				"required": ["name"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "resume_workflow",
			"description": (
				"Resume a previously failed or interrupted workflow run. "
				"Completed steps are reused from the journal (no redundant LLM calls). "
				"The workflow re-executes its control flow from the beginning but "
				"skips any step whose stable key is found in the journal."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"run_id": {
						"type": "string",
						"description": "The run_id of the workflow to resume.",
					},
				},
				"required": ["run_id"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "workflow_status",
			"description": (
				"Check the status of a workflow run. If run_id is omitted, "
				"lists all runs with their current state. If run_id is provided, "
				"shows detailed status including phase, completed steps, resource "
				"usage, and final output (when completed)."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"run_id": {
						"type": "string",
						"description": "Optional run_id to query. Omit to list all runs.",
					},
				},
			},
		},
	},
]

# ── Runtime-bound Handler Map ─────────────────────────────────


def make_workflow_handlers(
	runtime: WorkflowRuntime,
) -> dict[str, Callable[..., str]]:
	"""Bind Workflow tool handlers to one CodeHarness-owned runtime."""

	def start_workflow(name: str, inputs: str = "{}") -> str:
		return _run_start_workflow(runtime, name, inputs)

	def resume_workflow(run_id: str) -> str:
		return _run_resume_workflow(runtime, run_id)

	def workflow_status(run_id: str = "") -> str:
		return _run_workflow_status(runtime, run_id)

	return {
		"start_workflow": start_workflow,
		"resume_workflow": resume_workflow,
		"workflow_status": workflow_status,
	}
