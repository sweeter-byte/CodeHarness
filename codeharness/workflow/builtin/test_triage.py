"""Built-in Workflow: test-triage

Run tests, collect failures, group by probable root cause, then
analyze each group in parallel (pipeline) and generate a triage report.

Flow:
  Run Tests
      ↓
  Parse Failures
      ↓
  Group Failures
      ↓
  Pipeline: [analyze → suggest_fix] per group
      ↓
  Triage Report
"""

from codeharness.workflow.definition import (
	AgentStep,
	PipelineStep,
	Phase,
	ToolStep,
	WorkflowConfig,
	WorkflowDefinition,
)

# ── Output Schemas ────────────────────────────────────────────

PARSE_FAILURES_SCHEMA = {
	"type": "object",
	"properties": {
		"failures": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"test_id": {"type": "string"},
					"error_type": {"type": "string"},
					"error_message": {"type": "string"},
					"traceback_summary": {"type": "string"},
					"file": {"type": "string"},
					"line": {"type": ["integer", "null"]},
				},
				"required": ["test_id", "error_type", "error_message"],
			},
		},
		"total_tests": {"type": "integer"},
		"passed": {"type": "integer"},
		"failed": {"type": "integer"},
		"errors": {"type": "integer"},
	},
	"required": ["failures", "total_tests", "passed", "failed"],
}

GROUP_SCHEMA = {
	"type": "object",
	"properties": {
		"groups": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"group_id": {"type": "string"},
					"probable_cause": {"type": "string"},
					"test_ids": {"type": "array", "items": {"type": "string"}},
					"related_source_files": {"type": "array", "items": {"type": "string"}},
					"confidence": {"type": "number"},
				},
				"required": ["group_id", "probable_cause", "test_ids"],
			},
		},
		"ungrouped": {"type": "array", "items": {"type": "string"}},
	},
	"required": ["groups", "ungrouped"],
}

ANALYZE_SCHEMA = {
	"type": "object",
	"properties": {
		"root_cause": {"type": "string"},
		"evidence": {"type": "array", "items": {"type": "string"}},
		"affected_code": {"type": "array"},
		"is_regression": {"type": "boolean"},
		"introduced_by": {"type": ["string", "null"]},
	},
	"required": ["root_cause", "evidence", "is_regression"],
}

SUGGEST_FIX_SCHEMA = {
	"type": "object",
	"properties": {
		"fix_strategy": {"type": "string"},
		"code_suggestion": {"type": ["string", "null"]},
		"effort": {"type": "string", "enum": ["trivial", "small", "medium", "large"]},
		"risk": {"type": "string"},
	},
	"required": ["fix_strategy", "effort", "risk"],
}

TRIAGE_REPORT_SCHEMA = {
	"type": "object",
	"properties": {
		"report_markdown": {"type": "string"},
		"priority_order": {"type": "array"},
		"quick_wins": {"type": "array", "items": {"type": "string"}},
		"estimated_total_effort": {"type": "string"},
	},
	"required": ["report_markdown", "priority_order", "quick_wins"],
}

# ── Prompt Templates ──────────────────────────────────────────

PARSE_PROMPT = """\
Parse the following test output and extract structured failure information.

Test output:
{test_output}

Return ONLY a JSON object with:
- "failures": array of {{test_id, error_type, error_message, traceback_summary, file, line}}
- "total_tests": integer (total collected)
- "passed": integer
- "failed": integer
- "errors": integer (collection errors, distinct from test failures)

test_id format: "path/to/file.py::TestClass::test_method"
If no failures, return empty array for "failures"."""

GROUP_PROMPT = """\
Group the following test failures by probable common root cause.
Failures caused by the same underlying issue should be in the same group.

Use read_file and grep to examine the failing test files and related source
code to determine relationships.

Failures:
{parse_failures}

Return ONLY a JSON object with:
- "groups": array of {{group_id, probable_cause, test_ids, related_source_files, confidence}}
- "ungrouped": array of test_ids that don't fit any group

group_id format: "grp_1", "grp_2", etc.
confidence: 0.0-1.0 (how sure you are about the grouping)"""

ANALYZE_PROMPT = """\
Deeply analyze this group of related test failures to determine the root cause.

Failure group:
{item}

Use read_file and grep to examine the source code, understand the failure
mechanism, and determine if this is a regression.

Return ONLY a JSON object with:
- "root_cause": concise description of the underlying problem
- "evidence": array of evidence strings (file:line references, error patterns)
- "affected_code": array of {{file, line_range, snippet}}
- "is_regression": boolean (was this working before?)
- "introduced_by": what change likely introduced this (or null)"""

SUGGEST_FIX_PROMPT = """\
Based on the root cause analysis, suggest a fix strategy for this failure group.

Failure group: {item}
Root cause analysis: {analyze_failure_group}

Return ONLY a JSON object with:
- "fix_strategy": description of how to fix
- "code_suggestion": specific code change suggestion (or null if too complex)
- "effort": trivial|small|medium|large
- "risk": description of risk if this fix is applied"""

TRIAGE_REPORT_PROMPT = """\
Generate a test triage report from the analysis results.

Test summary: {parse_failures}
Failure groups: {group_failures}
Analysis results: {parallel_analysis}

Create a prioritized triage report:
1. Order groups by urgency (regression > high-confidence > many-tests-affected)
2. Highlight quick wins (effort=trivial)
3. Estimate total effort

Return ONLY a JSON object with:
- "report_markdown": well-formatted Markdown triage report
- "priority_order": array of {{group_id, urgency, reason}}
- "quick_wins": array of group_ids with effort=trivial
- "estimated_total_effort": overall effort estimate string"""


# ── Workflow Definition ───────────────────────────────────────


def test_triage_workflow() -> WorkflowDefinition:
	return WorkflowDefinition(
		name="test-triage",
		description=(
			"Run tests, parse failures, group by root cause, analyze each group "
			"in parallel, and generate a prioritized triage report with fix suggestions."
		),
		inputs_schema={
			"type": "object",
			"properties": {
				"workspace": {"type": "string"},
				"test_command": {
					"type": "string",
					"description": "Test command (default: pytest --tb=short -q)",
				},
				"test_paths": {
					"type": "array",
					"items": {"type": "string"},
					"description": "Optional: limit test scope to these paths",
				},
				"max_failures_analyze": {
					"type": "integer",
					"description": "Max failure groups to analyze (default: 20)",
				},
			},
		},
		phases=[
			Phase(
				name="execute",
				steps=[
					ToolStep(
						label="run_tests",
						tool_name="bash",
						args_template={
							"command": "{test_command}",
						},
						output_key="test_output",
						timeout=180,
						allow_failure=True,
					),
				],
			),
			Phase(
				name="collect",
				steps=[
					AgentStep(
						label="parse_failures",
						prompt_template=PARSE_PROMPT,
						output_key="parse_failures",
						output_schema=PARSE_FAILURES_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
			Phase(
				name="group",
				steps=[
					AgentStep(
						label="group_failures",
						prompt_template=GROUP_PROMPT,
						output_key="group_failures",
						output_schema=GROUP_SCHEMA,
						max_rounds=10,
						tools=["read_file", "grep", "glob"],
						condition="{parse_failures.failed} > 0",
					),
				],
			),
			Phase(
				name="analyze",
				steps=[
					PipelineStep(
						label="parallel_analysis",
						items_key="group_failures.groups",
						output_key="parallel_analysis",
						max_item_concurrency=4,
						stages=[
							AgentStep(
								label="analyze_failure_group",
								prompt_template=ANALYZE_PROMPT,
								output_key="analyze_failure_group",
								output_schema=ANALYZE_SCHEMA,
								max_rounds=10,
								tools=["read_file", "grep", "glob"],
							),
							AgentStep(
								label="suggest_fix",
								prompt_template=SUGGEST_FIX_PROMPT,
								output_key="suggest_fix",
								output_schema=SUGGEST_FIX_SCHEMA,
								max_rounds=5,
								tools=["read_file"],
							),
						],
						condition="{parse_failures.failed} > 0",
					),
				],
			),
			Phase(
				name="report",
				steps=[
					AgentStep(
						label="generate_triage_report",
						prompt_template=TRIAGE_REPORT_PROMPT,
						output_key="triage_report",
						output_schema=TRIAGE_REPORT_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
		],
		config=WorkflowConfig(
			max_concurrency=4,
			max_agent_calls=30,
			max_token_budget=300_000,
			run_timeout=900,
		),
	)
