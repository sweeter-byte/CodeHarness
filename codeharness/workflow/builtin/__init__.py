"""Built-in workflow definitions.

Each workflow is defined as a function returning a WorkflowDefinition.
register_builtins() populates a WorkflowRegistry with all built-in workflows.
"""

from codeharness.workflow.registry import WorkflowRegistry

from codeharness.workflow.builtin.review_changes import review_changes_workflow
from codeharness.workflow.builtin.validate_changes import validate_changes_workflow
from codeharness.workflow.builtin.test_triage import test_triage_workflow
from codeharness.workflow.builtin.benchmark_agent import benchmark_agent_workflow
from codeharness.workflow.builtin.pr_review import pr_review_workflow


def register_builtins(registry: WorkflowRegistry) -> None:
	"""Register all built-in workflows into the given registry."""
	registry.register(review_changes_workflow())
	registry.register(validate_changes_workflow())
	registry.register(test_triage_workflow())
	registry.register(benchmark_agent_workflow())
	registry.register(pr_review_workflow())
