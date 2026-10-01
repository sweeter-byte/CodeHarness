"""Built-in Workflow: validate-changes

Parallel execution of lint/typecheck/test/build validators, followed by
intelligent classification and reporting.

Flow:
  Changed Files
       ↓
  ┌─────────┬───────────┬──────────┬─────────┐
   Lint     TypeCheck    Tests      Build
  └─────────┴───────────┴──────────┴─────────┘
       ↓
  Collect & Classify
       ↓
  Validation Report
"""

from codeharness.workflow.definition import (
	AgentStep,
	ParallelStep,
	Phase,
	ToolStep,
	WorkflowConfig,
	WorkflowDefinition,
)

# ── Output Schemas ────────────────────────────────────────────

CLASSIFY_SCHEMA = {
	"type": "object",
	"properties": {
		"classifications": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"source": {"type": "string"},
					"file": {"type": "string"},
					"line": {"type": ["integer", "null"]},
					"severity": {"type": "string", "enum": ["error", "warning", "info"]},
					"message": {"type": "string"},
					"category": {"type": "string"},
				},
				"required": ["source", "file", "severity", "message"],
			},
		},
		"summary": {
			"type": "object",
			"properties": {
				"total_errors": {"type": "integer"},
				"total_warnings": {"type": "integer"},
				"by_source": {"type": "object"},
			},
			"required": ["total_errors", "total_warnings"],
		},
	},
	"required": ["classifications", "summary"],
}

REPORT_SCHEMA = {
	"type": "object",
	"properties": {
		"report_markdown": {"type": "string"},
		"pass": {"type": "boolean"},
		"blocking_issues": {"type": "array"},
		"suggestions": {"type": "array"},
	},
	"required": ["report_markdown", "pass", "blocking_issues", "suggestions"],
}

# ── Prompt Templates ──────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are analyzing the output of code validation tools (lint, typecheck, test, build).
Parse each tool's output and classify every error/warning found.

Tool outputs:
{run_validators}

Changed files: {changed_files}

For each issue found, classify it with:
- source: which tool found it (lint|typecheck|test|build)
- file: the file path
- line: line number (or null)
- severity: error|warning|info
- message: concise description
- category: type_error|style|test_failure|build_error|security|logic|other

Return ONLY a JSON object with:
- "classifications": array of classified issues
- "summary": {{"total_errors": N, "total_warnings": N, "by_source": {{...}}}}"""

REPORT_PROMPT = """\
Generate a validation report from the classified issues.

Classifications:
{collect_and_classify}

Rules:
- "pass" is true only if total_errors == 0
- "blocking_issues" are errors that must be fixed before merge
- "suggestions" are warnings worth fixing but not blocking

Return ONLY a JSON object with:
- "report_markdown": formatted Markdown report with sections per tool
- "pass": boolean
- "blocking_issues": array of issue descriptions
- "suggestions": array of suggestion strings"""


# ── Workflow Definition ───────────────────────────────────────


def validate_changes_workflow() -> WorkflowDefinition:
	return WorkflowDefinition(
		name="validate-changes",
		description=(
			"Parallel lint/typecheck/test/build validation with intelligent "
			"error classification and pass/fail reporting."
		),
		inputs_schema={
			"type": "object",
			"properties": {
				"changed_files": {
					"type": "array",
					"items": {"type": "string"},
					"description": "List of changed file paths",
				},
				"workspace": {
					"type": "string",
					"description": "Working directory path",
				},
				"lint_command": {
					"type": "string",
					"description": "Lint command (default: ruff check)",
				},
				"typecheck_command": {
					"type": "string",
					"description": "Type check command (default: mypy)",
				},
				"test_command": {
					"type": "string",
					"description": "Test command (default: pytest --tb=short -q)",
				},
				"build_command": {
					"type": "string",
					"description": "Build/compile check command",
				},
			},
			"required": ["changed_files"],
		},
		phases=[
			Phase(
				name="validate",
				steps=[
					ParallelStep(
						label="run_validators",
						output_key="run_validators",
						join_mode="all",
						branches=[
							ToolStep(
								label="lint",
								tool_name="bash",
								args_template={
									"command": "{lint_command}",
								},
								output_key="lint_result",
								timeout=120,
								allow_failure=True,
							),
							ToolStep(
								label="typecheck",
								tool_name="bash",
								args_template={
									"command": "{typecheck_command}",
								},
								output_key="typecheck_result",
								timeout=120,
								allow_failure=True,
							),
							ToolStep(
								label="test",
								tool_name="bash",
								args_template={
									"command": "{test_command}",
								},
								output_key="test_result",
								timeout=180,
								allow_failure=True,
							),
							ToolStep(
								label="build",
								tool_name="bash",
								args_template={
									"command": "{build_command}",
								},
								output_key="build_result",
								timeout=120,
								allow_failure=True,
							),
						],
					),
				],
			),
			Phase(
				name="analyze",
				steps=[
					AgentStep(
						label="collect_and_classify",
						prompt_template=CLASSIFY_PROMPT,
						output_key="collect_and_classify",
						output_schema=CLASSIFY_SCHEMA,
						max_rounds=5,
						tools=[],
					),
					AgentStep(
						label="generate_validation_report",
						prompt_template=REPORT_PROMPT,
						output_key="validation_report",
						output_schema=REPORT_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
		],
		config=WorkflowConfig(
			max_concurrency=4,
			max_agent_calls=4,
			max_token_budget=80_000,
			run_timeout=300,
		),
	)
