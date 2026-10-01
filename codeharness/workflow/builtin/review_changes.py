"""Built-in Workflow: review-changes

Multi-dimensional code review with parallel analysis, verification,
deduplication and severity-sorted reporting.

Flow:
  Changed Code (diff)
       ↓
  ┌────────────┬────────────┬────────────┬────────────┐
  Correctness  Security     Performance  Maintainability
  └────────────┴────────────┴────────────┴────────────┘
       ↓
  Verify Findings
       ↓
  Deduplicate
       ↓
  Severity Sort → Review Report
"""

from codeharness.workflow.definition import (
	AgentStep,
	ParallelStep,
	Phase,
	WorkflowConfig,
	WorkflowDefinition,
)

# ── Output Schemas ────────────────────────────────────────────

FINDINGS_SCHEMA = {
	"type": "object",
	"properties": {
		"findings": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"file": {"type": "string"},
					"line": {"type": "integer"},
					"severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
					"title": {"type": "string"},
					"explanation": {"type": "string"},
				},
				"required": ["file", "severity", "title", "explanation"],
			},
		}
	},
	"required": ["findings"],
}

VERIFY_SCHEMA = {
	"type": "object",
	"properties": {
		"verified": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"file": {"type": "string"},
					"line": {"type": "integer"},
					"severity": {"type": "string"},
					"title": {"type": "string"},
					"explanation": {"type": "string"},
					"verified": {"type": "boolean"},
					"confidence": {"type": "number"},
					"false_positive_reason": {"type": ["string", "null"]},
				},
				"required": ["file", "severity", "title", "verified"],
			},
		},
		"summary": {
			"type": "object",
			"properties": {
				"total": {"type": "integer"},
				"verified_count": {"type": "integer"},
				"rejected_count": {"type": "integer"},
			},
			"required": ["total", "verified_count", "rejected_count"],
		},
	},
	"required": ["verified", "summary"],
}

DEDUP_SCHEMA = {
	"type": "object",
	"properties": {
		"deduplicated": {"type": "array"},
		"removed_count": {"type": "integer"},
	},
	"required": ["deduplicated", "removed_count"],
}

REPORT_SCHEMA = {
	"type": "object",
	"properties": {
		"report_markdown": {"type": "string"},
		"stats": {
			"type": "object",
			"properties": {
				"critical": {"type": "integer"},
				"high": {"type": "integer"},
				"medium": {"type": "integer"},
				"low": {"type": "integer"},
			},
			"required": ["critical", "high", "medium", "low"],
		},
		"top_risks": {"type": "array", "items": {"type": "string"}},
	},
	"required": ["report_markdown", "stats", "top_risks"],
}

# ── Prompt Templates ──────────────────────────────────────────

CORRECTNESS_PROMPT = """\
You are reviewing code changes for CORRECTNESS issues only.

Focus on:
- Logic errors, off-by-one bugs, wrong conditions
- Null/None safety, missing error handling
- Race conditions, incorrect state transitions
- Wrong algorithm or data structure usage

Changed files: {changed_files}

Diff:
{diff}

{project_context}

Return ONLY a JSON object with a "findings" array. Each finding must have:
file (string), line (integer or null), severity (critical|high|medium|low),
title (string), explanation (string).
If no issues found, return {{"findings": []}}."""

SECURITY_PROMPT = """\
You are reviewing code changes for SECURITY vulnerabilities only.

Focus on:
- Injection vulnerabilities (SQL, command, XSS)
- Hardcoded secrets, credentials, API keys
- Insecure deserialization, path traversal
- Permission bypasses, missing auth checks
- Unsafe cryptographic usage

Changed files: {changed_files}

Diff:
{diff}

{project_context}

Return ONLY a JSON object with a "findings" array. Each finding must have:
file (string), line (integer or null), severity (critical|high|medium|low),
title (string), explanation (string).
If no issues found, return {{"findings": []}}."""

PERFORMANCE_PROMPT = """\
You are reviewing code changes for PERFORMANCE issues only.

Focus on:
- N+1 queries, unnecessary database calls
- Unbounded loops, missing pagination
- Memory leaks, large allocations in hot paths
- Blocking calls in async contexts
- Missing caching opportunities

Changed files: {changed_files}

Diff:
{diff}

{project_context}

Return ONLY a JSON object with a "findings" array. Each finding must have:
file (string), line (integer or null), severity (critical|high|medium|low),
title (string), explanation (string).
If no issues found, return {{"findings": []}}."""

MAINTAINABILITY_PROMPT = """\
You are reviewing code changes for MAINTAINABILITY issues only.

Focus on:
- Unclear naming, missing documentation on public APIs
- Duplicated logic that should be extracted
- Over-coupling, violations of single responsibility
- Missing tests for new behavior
- Overly complex functions (>50 lines, deep nesting)

Changed files: {changed_files}

Diff:
{diff}

{project_context}

Return ONLY a JSON object with a "findings" array. Each finding must have:
file (string), line (integer or null), severity (critical|high|medium|low),
title (string), explanation (string).
If no issues found, return {{"findings": []}}."""

VERIFY_PROMPT = """\
You are verifying code review findings. For each finding below, determine if it
is a TRUE issue or a FALSE POSITIVE by examining the actual source code.

Use read_file and grep to check the actual code context around each finding.
Be skeptical: many automated findings are false positives.

Findings to verify:
{multi_dim_review}

Diff for reference:
{diff}

Return ONLY a JSON object with:
- "verified": array of findings with added fields: verified (bool), confidence (0-1),
  false_positive_reason (string or null)
- "summary": {{"total": N, "verified_count": N, "rejected_count": N}}"""

DEDUP_PROMPT = """\
You are deduplicating verified code review findings.
Merge findings that point to the same root cause or the same code location.

Verified findings:
{verify_findings}

Return ONLY a JSON object with:
- "deduplicated": array of unique findings (keep the highest severity when merging)
- "removed_count": number of duplicates removed"""

REPORT_PROMPT = """\
Generate the final code review report from deduplicated findings.
Sort by severity: critical > high > medium > low.
Within the same severity, sort by file path.

Deduplicated findings:
{deduplicate}

Return ONLY a JSON object with:
- "report_markdown": a well-formatted Markdown review report
- "stats": {{"critical": N, "high": N, "medium": N, "low": N}}
- "top_risks": array of the most important risk descriptions (max 5)"""


# ── Workflow Definition ───────────────────────────────────────


def review_changes_workflow() -> WorkflowDefinition:
	return WorkflowDefinition(
		name="review-changes",
		description=(
			"Multi-dimensional code review: parallel analysis across correctness, "
			"security, performance and maintainability, with verification, "
			"deduplication and severity-sorted reporting."
		),
		inputs_schema={
			"type": "object",
			"properties": {
				"diff": {"type": "string", "description": "Unified diff text"},
				"changed_files": {"type": "array", "items": {"type": "string"}},
				"project_context": {"type": "string", "description": "Optional project background"},
			},
			"required": ["diff", "changed_files"],
		},
		phases=[
			Phase(
				name="review",
				steps=[
					ParallelStep(
						label="multi_dim_review",
						output_key="multi_dim_review",
						join_mode="all",
						branches=[
							AgentStep(
								label="correctness_review",
								prompt_template=CORRECTNESS_PROMPT,
								output_key="correctness_review",
								output_schema=FINDINGS_SCHEMA,
								tools=["read_file", "grep", "glob"],
								max_rounds=8,
							),
							AgentStep(
								label="security_review",
								prompt_template=SECURITY_PROMPT,
								output_key="security_review",
								output_schema=FINDINGS_SCHEMA,
								tools=["read_file", "grep", "glob"],
								max_rounds=8,
							),
							AgentStep(
								label="performance_review",
								prompt_template=PERFORMANCE_PROMPT,
								output_key="performance_review",
								output_schema=FINDINGS_SCHEMA,
								tools=["read_file", "grep", "glob"],
								max_rounds=8,
							),
							AgentStep(
								label="maintainability_review",
								prompt_template=MAINTAINABILITY_PROMPT,
								output_key="maintainability_review",
								output_schema=FINDINGS_SCHEMA,
								tools=["read_file", "grep", "glob"],
								max_rounds=8,
							),
						],
					),
				],
			),
			Phase(
				name="verify",
				steps=[
					AgentStep(
						label="verify_findings",
						prompt_template=VERIFY_PROMPT,
						output_key="verify_findings",
						output_schema=VERIFY_SCHEMA,
						tools=["read_file", "grep", "glob"],
						max_rounds=15,
						timeout=600,
					),
				],
			),
			Phase(
				name="aggregate",
				steps=[
					AgentStep(
						label="deduplicate",
						prompt_template=DEDUP_PROMPT,
						output_key="deduplicate",
						output_schema=DEDUP_SCHEMA,
						max_rounds=5,
						tools=[],
					),
					AgentStep(
						label="severity_sort_and_report",
						prompt_template=REPORT_PROMPT,
						output_key="review_report",
						output_schema=REPORT_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
		],
		config=WorkflowConfig(
			max_concurrency=4,
			max_agent_calls=12,
			max_token_budget=200_000,
			run_timeout=600,
		),
	)
