"""Workflow Runtime — deterministic orchestration for multi-step agent processes.

Public API:
  WORKFLOWS          — WorkflowRegistry singleton (register/lookup definitions)
  WORKFLOW_RUNTIME   — WorkflowRuntime singleton (start/resume/status/cancel)
  EVENT_BUS          — WorkflowEventBus singleton (subscribe to progress events)
  WORKFLOW_TOOLS     — OpenAI tool schemas for the leader agent
  WORKFLOW_HANDLERS  — Tool handler map

Integration (in app.py):
  from codeharness.workflow import WORKFLOWS, WORKFLOW_TOOLS, WORKFLOW_HANDLERS
  from codeharness.workflow.builtin import register_builtins
  register_builtins(WORKFLOWS)
  registry.extend(WORKFLOW_TOOLS, WORKFLOW_HANDLERS)
"""

from codeharness.workflow.definition import (
	AgentStep,
	ParallelStep,
	PipelineStep,
	Phase,
	Step,
	StepKind,
	ToolStep,
	WorkflowConfig,
	WorkflowDefinition,
	WorkflowStep,
)
from codeharness.workflow.events import EVENT_BUS, WorkflowEventBus
from codeharness.workflow.registry import (
	DuplicateWorkflowError,
	WorkflowRegistry,
	WorkflowValidationError,
)
from codeharness.workflow.runtime import (
	WORKFLOW_RUNTIME,
	WorkflowBudgetExceeded,
	WorkflowError,
	WorkflowRuntime,
	WorkflowTimeout,
)
from codeharness.workflow.state import (
	JournalEntry,
	RunSnapshot,
	RunStatus,
	STATE_STORE,
	WorkflowStateStore,
)
from codeharness.workflow.tools import WORKFLOW_HANDLERS, WORKFLOW_TOOLS

# Module-level singletons
WORKFLOWS = WorkflowRegistry()

__all__ = [
	# Singletons
	"WORKFLOWS",
	"WORKFLOW_RUNTIME",
	"EVENT_BUS",
	"STATE_STORE",
	"WORKFLOW_TOOLS",
	"WORKFLOW_HANDLERS",
	# Definition model
	"AgentStep",
	"ToolStep",
	"ParallelStep",
	"PipelineStep",
	"WorkflowStep",
	"Step",
	"StepKind",
	"Phase",
	"WorkflowConfig",
	"WorkflowDefinition",
	# Registry
	"WorkflowRegistry",
	"WorkflowValidationError",
	"DuplicateWorkflowError",
	# Runtime
	"WorkflowRuntime",
	"WorkflowError",
	"WorkflowBudgetExceeded",
	"WorkflowTimeout",
	# State
	"WorkflowStateStore",
	"RunSnapshot",
	"RunStatus",
	"JournalEntry",
	# Events
	"WorkflowEventBus",
]
