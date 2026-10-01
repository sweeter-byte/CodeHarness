"""Built-in Workflow: pr-review

GitHub PR review via MCP: fetch PR metadata and diff, invoke the
review-changes sub-workflow, format the result as a PR comment,
and optionally post it.

Flow:
  pr-review
      │
      ├── MCP: get PR metadata
      ├── MCP: get PR diff
      │
      └── workflow("review-changes")
                  ↓
            Format PR Review
                  ↓
            (optional) Post Comment
"""

from codeharness.workflow.definition import (
	AgentStep,
	Phase,
	ToolStep,
	WorkflowConfig,
	WorkflowDefinition,
	WorkflowStep,
)

# ── Output Schemas ────────────────────────────────────────────

FORMAT_REVIEW_SCHEMA = {
	"type": "object",
	"properties": {
		"comment_body": {"type": "string"},
		"overall_assessment": {
			"type": "string",
			"enum": ["approve", "request_changes", "comment"],
		},
		"summary_line": {"type": "string"},
	},
	"required": ["comment_body", "overall_assessment", "summary_line"],
}

# ── Prompt Templates ──────────────────────────────────────────

FORMAT_PROMPT = """\
Format a code review result as a GitHub Pull Request comment.

PR metadata:
{pr_metadata}

Review findings:
{review_result}

Requirements:
1. Start with a one-line overall assessment
2. Group findings by severity (critical → low)
3. Use GitHub-flavored Markdown with collapsible sections for details
4. Include file:line references as clickable links where possible
5. End with a summary table of finding counts by severity

Assessment rules:
- "approve": no critical or high findings
- "request_changes": any critical or high finding exists
- "comment": only medium/low findings

Return ONLY a JSON object with:
- "comment_body": the full Markdown comment text
- "overall_assessment": approve|request_changes|comment
- "summary_line": one-line summary (e.g., "Found 2 critical, 3 high issues")"""


# ── Workflow Definition ───────────────────────────────────────


def pr_review_workflow() -> WorkflowDefinition:
	return WorkflowDefinition(
		name="pr-review",
		description=(
			"GitHub PR review: fetch PR metadata and diff via MCP, run the "
			"review-changes sub-workflow, format results as a PR comment, "
			"and optionally post it."
		),
		inputs_schema={
			"type": "object",
			"properties": {
				"pr_number": {
					"type": "integer",
					"description": "Pull request number",
				},
				"repo": {
					"type": "string",
					"description": "Repository in 'owner/repo' format",
				},
				"github_mcp_server": {
					"type": "string",
					"description": "MCP server name for GitHub (default: github)",
				},
				"post_comment": {
					"type": "boolean",
					"description": "Whether to post the review as a PR comment (default: false)",
				},
			},
			"required": ["pr_number", "repo"],
		},
		phases=[
			Phase(
				name="fetch",
				steps=[
					ToolStep(
						label="get_pr_metadata",
						tool_name="mcp__github__get_pull_request",
						args_template={
							"owner": "{repo_owner}",
							"repo": "{repo_name}",
							"pull_number": "{pr_number}",
						},
						output_key="pr_metadata",
						timeout=30,
					),
					ToolStep(
						label="get_pr_diff",
						tool_name="mcp__github__get_pull_request_diff",
						args_template={
							"owner": "{repo_owner}",
							"repo": "{repo_name}",
							"pull_number": "{pr_number}",
						},
						output_key="pr_diff",
						timeout=30,
					),
				],
			),
			Phase(
				name="review",
				steps=[
					WorkflowStep(
						label="invoke_review",
						workflow_name="review-changes",
						inputs_mapping={
							"diff": "{pr_diff}",
							"changed_files": "{pr_metadata.changed_files}",
							"project_context": "PR #{pr_number}: {pr_metadata.title} by {pr_metadata.author}",
						},
						output_key="review_result",
					),
				],
			),
			Phase(
				name="format",
				steps=[
					AgentStep(
						label="format_pr_review",
						prompt_template=FORMAT_PROMPT,
						output_key="format_pr_review",
						output_schema=FORMAT_REVIEW_SCHEMA,
						max_rounds=5,
						tools=[],
					),
				],
			),
			Phase(
				name="publish",
				steps=[
					ToolStep(
						label="post_pr_comment",
						tool_name="mcp__github__create_pull_request_comment",
						args_template={
							"owner": "{repo_owner}",
							"repo": "{repo_name}",
							"pull_number": "{pr_number}",
							"body": "{format_pr_review.comment_body}",
						},
						output_key="comment_result",
						timeout=30,
						allow_failure=True,
						condition="{post_comment} == true",
					),
				],
			),
		],
		config=WorkflowConfig(
			max_concurrency=4,
			max_agent_calls=20,
			max_token_budget=250_000,
			max_nesting_depth=3,
			run_timeout=900,
		),
	)
