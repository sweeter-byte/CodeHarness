"""Built-in Workflow: benchmark-agent

Load an evaluation dataset, run an Agent on each case in parallel (pipeline),
collect traces/results, evaluate quality, aggregate metrics, and generate report.

Flow:
  Load Dataset
       ↓
  Prepare Cases
       ↓
  Pipeline: [execute_case] per case (parallel)
       ↓
  Evaluate Results
       ↓
  Compute Metrics
       ↓
  Generate Report
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

PREPARE_CASES_SCHEMA = {
	"type": "object",
	"properties": {
		"cases": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"case_id": {"type": "string"},
					"prompt": {"type": "string"},
					"expected_output": {"type": ["string", "null"]},
					"evaluation_criteria": {"type": "string"},
					"tags": {"type": "array", "items": {"type": "string"}},
					"difficulty": {"type": "string"},
					"timeout": {"type": "integer"},
				},
				"required": ["case_id", "prompt"],
			},
		},
		"total": {"type": "integer"},
		"skipped": {"type": "integer"},
		"skip_reasons": {"type": "array", "items": {"type": "string"}},
	},
	"required": ["cases", "total"],
}

CASE_RESULT_SCHEMA = {
	"type": "object",
	"properties": {
		"case_id": {"type": "string"},
		"final_output": {"type": "string"},
		"rounds_used": {"type": "integer"},
		"tokens_used": {"type": "integer"},
		"tools_called": {"type": "array", "items": {"type": "string"}},
		"timed_out": {"type": "boolean"},
		"error": {"type": ["string", "null"]},
	},
	"required": ["case_id", "final_output", "rounds_used", "timed_out"],
}

EVALUATE_SCHEMA = {
	"type": "object",
	"properties": {
		"evaluations": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"case_id": {"type": "string"},
					"pass": {"type": "boolean"},
					"score": {"type": "number"},
					"reasoning": {"type": "string"},
					"output_quality": {
						"type": "string",
						"enum": ["correct", "partial", "wrong", "timeout"],
					},
				},
				"required": ["case_id", "pass", "score", "output_quality"],
			},
		},
		"pass_count": {"type": "integer"},
		"fail_count": {"type": "integer"},
		"timeout_count": {"type": "integer"},
	},
	"required": ["evaluations", "pass_count", "fail_count"],
}

BENCHMARK_REPORT_SCHEMA = {
	"type": "object",
	"properties": {
		"report_markdown": {"type": "string"},
		"headline_metrics": {
			"type": "object",
			"properties": {
				"pass_rate": {"type": "string"},
				"avg_score": {"type": "string"},
				"total_cases": {"type": "integer"},
			},
			"required": ["pass_rate", "total_cases"],
		},
		"by_difficulty": {"type": "object"},
		"by_tag": {"type": "object"},
		"worst_cases": {"type": "array"},
		"recommendations": {"type": "array", "items": {"type": "string"}},
	},
	"required": ["report_markdown", "headline_metrics"],
}

# ── Prompt Templates ──────────────────────────────────────────

PREPARE_PROMPT = """\
Parse the following evaluation dataset (JSONL format) and prepare test cases.

Dataset content:
{raw_dataset}

For each line, extract a case with:
- case_id: unique identifier
- prompt: the task prompt for the agent
- expected_output: what a correct result looks like (or null)
- evaluation_criteria: how to judge the result
- tags: category tags
- difficulty: easy|medium|hard
- timeout: seconds allowed (default 300)

Skip malformed lines and record reasons.

Return ONLY a JSON object with:
- "cases": array of prepared cases
- "total": number of valid cases
- "skipped": number of skipped lines
- "skip_reasons": array of skip descriptions"""

EXECUTE_CASE_PROMPT = """\
Complete the following task. Use the available tools as needed.
Be thorough but efficient. Your final message is the deliverable.

Task:
{item_prompt}

Evaluation criteria (for your awareness):
{item_criteria}"""

EVALUATE_PROMPT = """\
Evaluate the results of an agent benchmark run.

For each case, compare the agent's output against the expected result and
evaluation criteria. Score from 0.0 (completely wrong) to 1.0 (perfect).

Cases (with expected outputs):
{prepare_cases}

Agent results:
{run_cases}

Scoring guidelines:
- 1.0: Output fully satisfies criteria
- 0.7-0.9: Mostly correct with minor issues
- 0.4-0.6: Partially correct
- 0.1-0.3: Mostly wrong but some relevant content
- 0.0: Completely wrong, timed out, or errored

Return ONLY a JSON object with:
- "evaluations": array of {{case_id, pass, score, reasoning, output_quality}}
- "pass_count": number with score >= 0.7
- "fail_count": number with score < 0.7
- "timeout_count": number that timed out"""

REPORT_PROMPT = """\
Generate a comprehensive benchmark report.

Evaluations:
{evaluate_results}

Original cases:
{prepare_cases}

Agent results:
{run_cases}

Compute aggregate metrics and generate the report:
- pass_rate: percentage of cases with score >= 0.7
- Breakdown by difficulty (easy/medium/hard)
- Breakdown by tag
- Worst performing cases (score < 0.4)
- Actionable recommendations for improving the agent

Return ONLY a JSON object with:
- "report_markdown": full Markdown benchmark report
- "headline_metrics": {{pass_rate, avg_score, total_cases}}
- "by_difficulty": {{easy: {{pass_rate, count}}, medium: ..., hard: ...}}
- "by_tag": {{tag: {{pass_rate, count}}}}
- "worst_cases": array of {{case_id, reason}}
- "recommendations": array of improvement suggestions"""


# ── Workflow Definition ───────────────────────────────────────


def benchmark_agent_workflow() -> WorkflowDefinition:
	return WorkflowDefinition(
		name="benchmark-agent",
		description=(
			"Dataset-driven agent evaluation: load cases, run agent on each in "
			"parallel, evaluate quality, aggregate metrics and generate report."
		),
		inputs_schema={
			"type": "object",
			"properties": {
				"dataset_path": {
					"type": "string",
					"description": "Path to JSONL evaluation dataset",
				},
				"workspace": {"type": "string"},
				"agent_system_prompt": {
					"type": "string",
					"description": "System prompt for the agent under test",
				},
				"agent_tools": {
					"type": "array",
					"items": {"type": "string"},
					"description": "Tool whitelist for the agent under test",
				},
				"max_concurrent_cases": {
					"type": "integer",
					"description": "Max parallel case execution (default: 3)",
				},
				"timeout_per_case": {
					"type": "integer",
					"description": "Seconds per case (default: 300)",
				},
			},
			"required": ["dataset_path"],
		},
		phases=[
			Phase(
				name="prepare",
				steps=[
					ToolStep(
						label="load_dataset",
						tool_name="read_file",
						args_template={"path": "{dataset_path}"},
						output_key="raw_dataset",
						timeout=30,
					),
					AgentStep(
						label="prepare_cases",
						prompt_template=PREPARE_PROMPT,
						output_key="prepare_cases",
						output_schema=PREPARE_CASES_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
			Phase(
				name="execute",
				steps=[
					PipelineStep(
						label="run_cases",
						items_key="prepare_cases.cases",
						output_key="run_cases",
						max_item_concurrency=3,
						stages=[
							AgentStep(
								label="execute_case",
								prompt_template=EXECUTE_CASE_PROMPT,
								output_key="execute_case",
								output_schema=CASE_RESULT_SCHEMA,
								system_prompt="{agent_system_prompt}",
								max_rounds=20,
								timeout=300,
							),
						],
					),
				],
			),
			Phase(
				name="evaluate",
				steps=[
					AgentStep(
						label="evaluate_results",
						prompt_template=EVALUATE_PROMPT,
						output_key="evaluate_results",
						output_schema=EVALUATE_SCHEMA,
						max_rounds=8,
						tools=[],
					),
				],
			),
			Phase(
				name="report",
				steps=[
					AgentStep(
						label="generate_benchmark_report",
						prompt_template=REPORT_PROMPT,
						output_key="benchmark_report",
						output_schema=BENCHMARK_REPORT_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
		],
		config=WorkflowConfig(
			max_concurrency=3,
			max_agent_calls=60,
			max_token_budget=1_000_000,
			run_timeout=3600,
		),
	)
