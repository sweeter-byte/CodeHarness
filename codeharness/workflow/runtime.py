"""WorkflowRuntime — the execution engine that drives Workflow definitions.

Responsibilities:
  - Start / resume / cancel / query workflow runs
  - Execute phases and steps in order
  - Manage concurrency (Semaphore), budgets, timeouts
  - Persist state via WorkflowStateStore
  - Emit progress events via WorkflowEventBus
  - Delegate agent steps to Agent instances via agent_factory

The runtime executes each workflow in a daemon thread (non-blocking),
consistent with BackgroundManager and Team patterns in CodeHarness.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

from codeharness.tools.executor import ToolExecutor
from codeharness.workflow.definition import (
	AgentStep,
	ParallelStep,
	PipelineStep,
	Step,
	ToolStep,
	WorkflowConfig,
	WorkflowDefinition,
	WorkflowStep,
)
from codeharness.workflow.events import (
	WorkflowEventBus,
	log_entry,
	phase_completed,
	phase_started,
	step_completed,
	step_failed,
	step_resumed,
	step_skipped,
	step_started,
	workflow_completed,
	workflow_failed,
	workflow_started,
)
from codeharness.workflow.registry import WorkflowRegistry
from codeharness.workflow.state import (
	JournalEntry,
	RunSnapshot,
	RunStatus,
	WorkflowRunLock,
	WorkflowStateStore,
	canonical_json,
	compute_legacy_stable_key,
	compute_stable_key,
)


# ── Exceptions ────────────────────────────────────────────────


class WorkflowError(Exception):
	"""Base error for workflow runtime failures."""


class WorkflowBudgetExceeded(WorkflowError):
	"""Raised when agent call count or token budget is exhausted."""


class WorkflowCancelled(WorkflowError):
	"""Raised when a user or Runtime cancellation stops workflow execution."""


class WorkflowTimeout(WorkflowError):
	"""Raised when a step or the entire run exceeds its timeout."""


class WorkflowNotFoundError(WorkflowError):
	"""Raised when a workflow name is not in the registry."""


# ── Context ───────────────────────────────────────────────────


class WorkflowContext:
	"""Mutable execution context passed between steps.

	Holds inputs (immutable) and step outputs (accumulated).
	Supports template rendering with {key} substitution.
	"""

	def __init__(self, inputs: dict[str, Any]):
		self.inputs = dict(inputs)
		self.steps: dict[str, Any] = {}

	def resolve(self, key: str) -> Any:
		"""Lookup a key: steps first, then inputs. Supports dot notation."""
		parts = key.split(".", 1)
		base = parts[0]
		value = self.steps.get(base, self.inputs.get(base))
		if len(parts) == 2 and value is not None:
			# Navigate nested dict/list
			for attr in parts[1].split("."):
				if isinstance(value, dict):
					value = value.get(attr)
				elif isinstance(value, list) and attr.isdigit():
					idx = int(attr)
					value = value[idx] if idx < len(value) else None
				else:
					value = getattr(value, attr, None)
				if value is None:
					return None
		return value

	def render(self, template: str) -> str:
		"""Render {key} placeholders in a template string."""
		def _replace(match: re.Match) -> str:
			key = match.group(1)
			value = self.resolve(key)
			if value is None:
				return match.group(0)  # leave unresolved
			if isinstance(value, (dict, list)):
				return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
			return str(value)

		return re.sub(r"\{([^}]+)\}", _replace, template)

	def render_args(self, args_template: dict[str, Any]) -> dict[str, Any]:
		"""Render all string values in an args dict."""
		rendered = {}
		for key, value in args_template.items():
			if isinstance(value, str):
				rendered[key] = self.render(value)
			elif isinstance(value, dict):
				rendered[key] = self.render_args(value)
			elif isinstance(value, list):
				rendered[key] = [
					self.render(v) if isinstance(v, str) else v for v in value
				]
			else:
				rendered[key] = value
		return rendered

	def set(self, key: str, value: Any) -> None:
		self.steps[key] = value

	def update(self, data: dict[str, Any]) -> None:
		self.steps.update(data)


# ── Run Handle ────────────────────────────────────────────────


class RunHandle:
	"""Tracks a live workflow run thread."""

	def __init__(self, run_id: str, thread: threading.Thread,
				 snapshot: RunSnapshot,
				 run_lock: WorkflowRunLock | None = None, starting: bool = False):
		self.run_id = run_id
		self.thread = thread
		self.snapshot = snapshot
		self.run_lock = run_lock
		self.starting = starting
		self.cancelled = threading.Event()
		self.state_lock = threading.RLock()
		self.journal_condition = threading.Condition(self.state_lock)
		self.journal_claims: set[str] = set()


# ── Agent factory type ────────────────────────────────────────

AgentFactory = Callable[..., Any]
ToolResolver = Callable[[str], Callable[..., str] | None]
DeliveryHandler = Callable[[str], bool]


# ── Workflow Agent System Prompt ──────────────────────────────

WORKFLOW_AGENT_SYSTEM = (
	"You are an agent step within a deterministic Workflow. "
	"Complete the given task precisely and return your result. "
	"If a specific output format is requested, follow it exactly. "
	"Be concise and factual. Do not ask questions — work with what you have."
)


# ── Runtime ───────────────────────────────────────────────────


class WorkflowRuntime:
	"""Orchestrates workflow execution: start, resume, cancel, query.

	Owned by one CodeHarness runtime.
	"""

	def __init__(
		self,
		*,
		registry: WorkflowRegistry,
		state_store: WorkflowStateStore,
		event_bus: WorkflowEventBus,
	):
		self._registry = registry
		self._store = state_store
		self._bus = event_bus
		self._runs: dict[str, RunHandle] = {}
		self._lock = threading.Lock()
		self._agent_factory: AgentFactory | None = None
		self._tool_resolver: ToolResolver | None = None
		self._tool_registry = None
		self._tool_executor = ToolExecutor()
		self._delivery_handler: DeliveryHandler | None = None

	# ── Configuration ──

	def set_agent_factory(self, factory: AgentFactory | None) -> None:
		self._agent_factory = factory

	def set_tool_resolver(self, resolver: ToolResolver | None) -> None:
		"""Inject a callable that resolves tool_name → handler."""
		self._tool_registry = None
		self._tool_resolver = resolver

	def set_tool_registry(self, registry) -> None:
		"""Inject the Runtime-assembled schema/handler registry."""
		self._tool_registry = registry
		self._tool_resolver = registry.get if registry is not None else None

	def set_delivery_handler(self, handler: DeliveryHandler | None) -> None:
		"""Handler for async result delivery to the leader agent."""
		self._delivery_handler = handler

	# ── Live run state ──

	def _get_handle(self, run_id: str) -> RunHandle | None:
		with self._lock:
			return self._runs.get(run_id)

	def _prune_stopped_handles(self) -> None:
		"""Remove only handles whose worker threads have fully exited."""
		with self._lock:
			stopped = [
				(run_id, handle)
				for run_id, handle in self._runs.items()
				if not handle.starting and not handle.thread.is_alive()
			]
			for run_id, handle in stopped:
				if self._runs.get(run_id) is handle:
					self._runs.pop(run_id, None)

	def _persist_snapshot_locked(self, handle: RunHandle) -> None:
		"""Persist handle.snapshot while the caller holds state_lock.

		Lock ordering is always RunHandle.state_lock followed by the
		WorkflowStateStore's per-run RLock.
		"""
		self._store.save_snapshot(handle.snapshot)
		self._store.update_index(handle.snapshot)

	@staticmethod
	def _raise_if_cancelled(handle: RunHandle) -> None:
		if handle.cancelled.is_set():
			raise WorkflowCancelled("Workflow cancelled")

	def _claim_journal_key(
		self,
		handle: RunHandle,
		journal: dict[str, JournalEntry],
		stable_key: str,
		fallback_keys: tuple[str, ...] = (),
	) -> JournalEntry | None:
		"""Return a cache hit, or claim a miss for the current thread."""
		with handle.journal_condition:
			while True:
				self._raise_if_cancelled(handle)
				cached = None
				for lookup_key in (stable_key, *fallback_keys):
					cached = journal.get(lookup_key)
					if cached is not None:
						return cached
				if stable_key not in handle.journal_claims:
					handle.journal_claims.add(stable_key)
					return None
				handle.journal_condition.wait(timeout=0.1)

	@staticmethod
	def _release_journal_claim(handle: RunHandle, stable_key: str) -> None:
		"""Drop an in-flight key and wake waiters on every exit path."""
		with handle.journal_condition:
			handle.journal_claims.discard(stable_key)
			handle.journal_condition.notify_all()

	def _publish_journal_entry(
		self, handle: RunHandle, run_id: str,
		journal: dict[str, JournalEntry], entry: JournalEntry,
	) -> None:
		"""Append and publish one successful result under the run-state lock."""
		with handle.state_lock:
			self._raise_if_cancelled(handle)
			self._store.append_journal(run_id, entry)
			journal[entry.stable_key] = entry

	def _mark_cancelled_locked(self, handle: RunHandle, reason: str) -> None:
		handle.cancelled.set()
		handle.snapshot.status = RunStatus.CANCELLED.value
		handle.snapshot.error = reason
		self._persist_snapshot_locked(handle)

	# ── Public API ──

	def start(
		self,
		workflow_name: str,
		inputs: dict[str, Any],
		depth: int = 0,
		parent_run_id: str | None = None,
	) -> tuple[str | None, str | None]:
		"""Start a new workflow run. Returns (run_id, None) or (None, error)."""
		self._prune_stopped_handles()
		if self._registry is None:
			return None, "Error: workflow registry not configured"
		if self._agent_factory is None:
			return None, "Error: agent factory not configured"

		definition = self._registry.get(workflow_name)
		if definition is None:
			available = ", ".join(self._registry.names()) or "none"
			return None, f"Error: unknown workflow '{workflow_name}'. Available: {available}"

		if depth > definition.config.max_nesting_depth:
			return None, (
				f"Error: nesting depth {depth} exceeds maximum "
				f"{definition.config.max_nesting_depth}"
			)

		run_id = self._store.generate_run_id()
		snapshot = RunSnapshot(
			run_id=run_id,
			workflow_name=workflow_name,
			inputs=inputs,
			status=RunStatus.CREATED.value,
			depth=depth,
			parent_run_id=parent_run_id,
		)
		self._store.save_snapshot(snapshot)
		self._store.update_index(snapshot)

		# Acquire run lock
		run_lock = self._store.acquire_run_lock(run_id)
		if run_lock is None:
			return None, f"Error: cannot acquire lock for run {run_id}"

		# Start execution thread
		handle: RunHandle | None = None
		try:
			thread = threading.Thread(
				target=self._execute_workflow,
				args=(run_id, definition, inputs, depth),
				daemon=True,
				name=f"workflow-{run_id}",
			)
			handle = RunHandle(run_id, thread, snapshot, run_lock, starting=True)
			with self._lock:
				self._runs[run_id] = handle
			thread.start()
			with self._lock:
				if self._runs.get(run_id) is handle:
					handle.starting = False
		except Exception as exc:  # noqa: BLE001 -- pre-start cleanup boundary
			with self._lock:
				if self._runs.get(run_id) is handle:
					self._runs.pop(run_id, None)
			snapshot.status = RunStatus.FAILED.value
			snapshot.error = f"Worker failed to start: {exc}"
			try:
				self._store.save_snapshot(snapshot)
				self._store.update_index(snapshot)
			finally:
				run_lock.release()
			return None, (
				f"Error: failed to start workflow run {run_id}: {exc}"
			)

		print(f"\033[36m[workflow] ▶ {workflow_name} started ({run_id})\033[0m")
		return run_id, None

	def resume(self, run_id: str) -> tuple[str | None, str | None]:
		"""Resume a failed/interrupted run. Returns (run_id, None) or (None, error)."""
		self._prune_stopped_handles()
		if self._registry is None:
			return None, "Error: workflow registry not configured"
		if self._agent_factory is None:
			return None, "Error: agent factory not configured"

		snapshot = self._store.load_snapshot(run_id)
		if snapshot is None:
			return None, f"Error: run '{run_id}' not found"
		if snapshot.status == RunStatus.COMPLETED.value:
			return None, f"Error: run '{run_id}' is already completed"
		if snapshot.status == RunStatus.RUNNING.value:
			# Check if it's actually still running
			with self._lock:
				handle = self._runs.get(run_id)
			if handle and (handle.starting or handle.thread.is_alive()):
				return None, f"Error: run '{run_id}' is still running"

		# Ownership must be established before persisted state says RUNNING.
		run_lock = self._store.acquire_run_lock(run_id)
		if run_lock is None:
			return None, f"Error: cannot acquire lock for run {run_id}"

		# The snapshot may have changed while this process waited for ownership.
		try:
			snapshot = self._store.load_snapshot(run_id)
		except Exception as exc:  # noqa: BLE001 -- ownership cleanup boundary
			run_lock.release()
			return None, f"Error: failed to reload run '{run_id}': {exc}"
		if snapshot is None:
			run_lock.release()
			return None, f"Error: run '{run_id}' not found"
		if snapshot.status == RunStatus.COMPLETED.value:
			run_lock.release()
			return None, f"Error: run '{run_id}' is already completed"

		definition = self._registry.get(snapshot.workflow_name)
		if definition is None:
			run_lock.release()
			return None, f"Error: workflow '{snapshot.workflow_name}' no longer registered"

		previous_status = snapshot.status
		previous_error = snapshot.error
		handle: RunHandle | None = None
		try:
			thread = threading.Thread(
				target=self._execute_workflow,
				args=(run_id, definition, snapshot.inputs, snapshot.depth),
				daemon=True,
				name=f"workflow-resume-{run_id}",
			)
			handle = RunHandle(run_id, thread, snapshot, run_lock, starting=True)
			with handle.state_lock:
				snapshot.status = RunStatus.RUNNING.value
				snapshot.error = None
				self._persist_snapshot_locked(handle)
			with self._lock:
				self._runs[run_id] = handle
			thread.start()
			with self._lock:
				if self._runs.get(run_id) is handle:
					handle.starting = False
		except Exception as exc:  # noqa: BLE001 -- pre-start cleanup boundary
			with self._lock:
				if handle is not None and self._runs.get(run_id) is handle:
					self._runs.pop(run_id, None)
			snapshot.status = previous_status
			snapshot.error = previous_error
			try:
				self._store.save_snapshot(snapshot)
				self._store.update_index(snapshot)
			finally:
				run_lock.release()
			return None, (
				f"Error: failed to resume workflow run {run_id}: {exc}"
			)

		print(f"\033[36m[workflow] ▶ {snapshot.workflow_name} resumed ({run_id})\033[0m")
		return run_id, None

	def status(self, run_id: str | None = None) -> dict[str, Any]:
		"""Query run status. If run_id is None, list all runs."""
		self._prune_stopped_handles()
		if run_id is None:
			runs = self._store.list_runs()
			return {
				"runs": [
					{
						"run_id": r.run_id,
						"workflow": r.workflow_name,
						"status": r.status,
						"phase": r.current_phase,
						"agent_calls": r.agent_calls_used,
						"tokens": r.tokens_used,
					}
					for r in runs
				]
			}

		snapshot = self._store.load_snapshot(run_id)
		if snapshot is None:
			return {"error": f"Run '{run_id}' not found"}

		result = asdict(snapshot)
		# Attach output if completed
		if snapshot.status == RunStatus.COMPLETED.value:
			output = self._store.load_output(run_id)
			result["output"] = output
		return result

	def cancel(self, run_id: str) -> str:
		"""Request cancellation of a running workflow."""
		self._prune_stopped_handles()
		handle = self._get_handle(run_id)
		if handle is None:
			return f"Error: run '{run_id}' not found or not active"
		# Set the event before waiting for state_lock so a worker about to commit
		# a terminal result observes cancellation first.
		handle.cancelled.set()
		with handle.state_lock:
			if handle.snapshot.status in (
				RunStatus.COMPLETED.value,
				RunStatus.FAILED.value,
			):
				return f"Error: run '{run_id}' is already terminal"
			self._mark_cancelled_locked(handle, "Cancelled by user")
		return f"Run '{run_id}' cancellation requested"

	def shutdown_all(self, timeout: float = 5.0) -> bool:
		"""Cancel workflows, retaining live handles so shutdown is retryable."""
		self._prune_stopped_handles()
		with self._lock:
			handles = list(self._runs.values())

		for handle in handles:
			handle.cancelled.set()
		for handle in handles:
			with handle.state_lock:
				if handle.snapshot.status not in (
					RunStatus.COMPLETED.value,
					RunStatus.FAILED.value,
					RunStatus.CANCELLED.value,
				):
					self._mark_cancelled_locked(
						handle, "Cancelled by Runtime shutdown"
					)
				else:
					handle.cancelled.set()

		deadline = time.monotonic() + max(0.0, timeout)
		for handle in handles:
			remaining = max(0.0, deadline - time.monotonic())
			if handle.thread.is_alive():
				handle.thread.join(timeout=remaining)

		with self._lock:
			for handle in handles:
				if (
					not handle.starting
					and not handle.thread.is_alive()
					and self._runs.get(handle.run_id) is handle
				):
					self._runs.pop(handle.run_id, None)
			return not any(
				handle.starting or handle.thread.is_alive() for handle in handles
			)

	# ── Internal: Workflow Execution ──────────────────────────

	def _execute_workflow(
		self,
		run_id: str,
		definition: WorkflowDefinition,
		inputs: dict[str, Any],
		depth: int,
	) -> None:
		"""Main execution loop — runs in a daemon thread."""
		handle = self._get_handle(run_id)
		if handle is None:
			return
		# A live run has exactly one in-memory snapshot, owned by its handle.
		snapshot = handle.snapshot

		try:
			journal = self._store.load_journal(run_id)
			context = WorkflowContext(inputs)
			config = definition.config
			deadline = time.time() + config.run_timeout
			with handle.state_lock:
				self._raise_if_cancelled(handle)
				snapshot.status = RunStatus.RUNNING.value
				snapshot.error = None
				self._persist_snapshot_locked(handle)
			self._bus.emit(workflow_started(run_id, definition.name))

			for phase in definition.phases:
				self._raise_if_cancelled(handle)
				if time.time() > deadline:
					raise WorkflowTimeout(
						f"Workflow exceeded {config.run_timeout}s timeout"
					)

				with handle.state_lock:
					self._raise_if_cancelled(handle)
					snapshot.current_phase = phase.name
					self._persist_snapshot_locked(handle)
				self._bus.emit(phase_started(run_id, phase.name))
				print(f"\033[36m[workflow]   phase: {phase.name}\033[0m")

				for step in phase.steps:
					self._raise_if_cancelled(handle)
					if time.time() > deadline:
						raise WorkflowTimeout("Workflow timeout exceeded")

					self._execute_step(
						step=step,
						context=context,
						snapshot=snapshot,
						journal=journal,
						config=config,
						phase_name=phase.name,
						run_id=run_id,
						definition=definition,
						depth=depth,
						deadline=deadline,
					)
					self._raise_if_cancelled(handle)

				self._bus.emit(phase_completed(run_id, phase.name))

			# ── Completed ──
			# Final output is the last step's result or the full context
			final_output = self._build_final_output(context, definition)
			self._raise_if_cancelled(handle)
			self._store.save_output(run_id, final_output)
			with handle.state_lock:
				self._raise_if_cancelled(handle)
				snapshot.status = RunStatus.COMPLETED.value
				snapshot.error = None
				self._persist_snapshot_locked(handle)
			self._bus.emit(workflow_completed(run_id, definition.name, {
				"agent_calls": snapshot.agent_calls_used,
				"tokens": snapshot.tokens_used,
			}))
			print(
				f"\033[36m[workflow] ✓ {definition.name} completed "
				f"({run_id}, {snapshot.agent_calls_used} agent calls, "
				f"{snapshot.tokens_used} tokens)\033[0m"
			)

			# Async delivery to leader
			self._deliver_completion(run_id, definition.name, snapshot)

		except Exception as exc:  # noqa: BLE001 -- worker lifecycle boundary
			cancelled = (
				isinstance(exc, WorkflowCancelled)
				or handle.cancelled.is_set()
			)
			with handle.state_lock:
				if cancelled:
					snapshot.status = RunStatus.CANCELLED.value
					snapshot.error = snapshot.error or "Workflow cancelled"
				else:
					snapshot.status = RunStatus.FAILED.value
					snapshot.error = str(exc)
				self._persist_snapshot_locked(handle)
			if cancelled:
				print(
					f"\033[33m[workflow] ■ {definition.name} cancelled "
					f"({run_id})\033[0m"
				)
			else:
				self._bus.emit(workflow_failed(run_id, definition.name, str(exc)))
				print(
					f"\033[31m[workflow] ✗ {definition.name} failed "
					f"({run_id}): {exc}\033[0m"
				)

		finally:
			if handle.run_lock is not None:
				handle.run_lock.release()

	# ── Internal: Step Dispatch ───────────────────────────────

	def _execute_step(
		self,
		step: Step,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		journal: dict[str, JournalEntry],
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		definition: WorkflowDefinition,
		depth: int,
		deadline: float,
	) -> None:
		"""Dispatch a step by its type."""
		# Check condition
		if step.condition and not self._evaluate_condition(step.condition, context):
			self._bus.emit(step_skipped(run_id, phase_name, step.label, "condition=false"))
			context.set(step.output_key, None)
			return

		if isinstance(step, AgentStep):
			result = self._execute_agent_step(
				step, context, snapshot, journal, config, phase_name, run_id, deadline
			)
		elif isinstance(step, ToolStep):
			result = self._execute_tool_step(
				step, context, snapshot, phase_name, run_id, journal=journal
			)
		elif isinstance(step, ParallelStep):
			result = self._execute_parallel_step(
				step, context, snapshot, journal, config, phase_name, run_id,
				definition, depth, deadline
			)
		elif isinstance(step, PipelineStep):
			result = self._execute_pipeline_step(
				step, context, snapshot, journal, config, phase_name, run_id,
				definition, depth, deadline
			)
		elif isinstance(step, WorkflowStep):
			result = self._execute_workflow_step(
				step, context, snapshot, config, phase_name, run_id, depth, deadline,
				journal=journal
			)
		else:
			raise WorkflowError(f"Unknown step type: {type(step).__name__}")

		context.set(step.output_key, result)

	# ── AgentStep Execution ───────────────────────────────────

	def _execute_agent_step(
		self,
		step: AgentStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		journal: dict[str, JournalEntry],
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		deadline: float,
		item_id: str = "",
		legacy_item_id: str | None = None,
	) -> Any:
		"""Serialize identical Agent journal misses within one live run."""
		handle = self._get_handle(run_id)
		if handle is None or handle.snapshot is not snapshot:
			raise WorkflowError(f"Run '{run_id}' has no matching live handle")
		prompt = context.render(step.prompt_template)
		schema_json = canonical_json(step.output_schema) if step.output_schema else ""
		key = compute_stable_key("agent", step.label, prompt, schema_json, item_id)
		legacy_schema_json = (
			json.dumps(step.output_schema, sort_keys=True)
			if step.output_schema else ""
		)
		legacy_item_identity = item_id if legacy_item_id is None else legacy_item_id
		legacy_key = compute_legacy_stable_key(
			"agent", step.label, prompt, legacy_schema_json, legacy_item_identity
		)

		cached = self._claim_journal_key(
			handle, journal, key, fallback_keys=(legacy_key,)
		)
		if cached is not None:
			self._bus.emit(step_resumed(run_id, phase_name, step.label))
			print(f"\033[36m[workflow]     ↺ {step.label} (cached)\033[0m")
			return cached.result

		try:
			return self._execute_agent_step_attempt(
				step, context, snapshot, journal, config, phase_name, run_id, deadline,
				item_id=item_id,
			)
		finally:
			self._release_journal_claim(handle, key)

	def _execute_agent_step_attempt(
		self,
		step: AgentStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		journal: dict[str, JournalEntry],
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		deadline: float,
		item_id: str = "",
	) -> Any:
		"""Execute one agent step: check journal → budget → run agent → validate."""
		handle = self._get_handle(run_id)
		if handle is None or handle.snapshot is not snapshot:
			raise WorkflowError(f"Run '{run_id}' has no matching live handle")
		prompt = context.render(step.prompt_template)
		schema_json = canonical_json(step.output_schema) if step.output_schema else ""
		key = compute_stable_key("agent", step.label, prompt, schema_json, item_id)

		# Journal lookup and budget guards share the same run-state boundary.
		with handle.state_lock:
			self._raise_if_cancelled(handle)
			cached = journal.get(key)
			if cached is None:
				if snapshot.agent_calls_used >= config.max_agent_calls:
					raise WorkflowBudgetExceeded(
						f"Agent call limit reached ({config.max_agent_calls})"
					)
				# This is a usage guard, not a strict token reservation quota:
				# already-running Agents may collectively cross the limit.
				if snapshot.tokens_used >= config.max_token_budget:
					raise WorkflowBudgetExceeded(
						f"Token budget exhausted ({config.max_token_budget})"
					)
		if cached is not None:
			self._bus.emit(step_resumed(run_id, phase_name, step.label))
			print(f"\033[36m[workflow]     ↺ {step.label} (cached)\033[0m")
			return cached.result

		if time.time() > deadline:
			raise WorkflowTimeout(f"Step '{step.label}' skipped: workflow timeout")

		# Build agent
		system = step.system_prompt or WORKFLOW_AGENT_SYSTEM
		agent_kwargs: dict[str, Any] = {
			"system": system,
			"max_rounds": step.max_rounds,
			"interactive": False,
			"memory_manager": False,
		}

		# Tool filtering
		if step.tools is not None:
			if self._tool_registry is None:
				raise WorkflowError(
					f"AgentStep '{step.label}' cannot resolve its tool "
					"whitelist: Runtime ToolRegistry is not configured"
				)
			# Filter the already-assembled Runtime registry so workspace-bound
			# native handlers, MCP tools, and Runtime-owned state are retained.
			registry = self._tool_registry
			filtered_schemas = [
				s for s in registry.schemas
				if s["function"]["name"] in step.tools
			]
			filtered_handlers = {
				name: handler for name, handler in registry.handlers.items()
				if name in step.tools
			}
			agent_kwargs["tools"] = filtered_schemas
			agent_kwargs["handlers"] = filtered_handlers

		agent = self._agent_factory(**agent_kwargs)
		messages = [{"role": "user", "content": prompt}]

		# Recheck after Agent construction, then reserve immediately before the
		# attempt starts.  The lock is released before agent_loop/LLM/tool work.
		with handle.state_lock:
			self._raise_if_cancelled(handle)
			cached = journal.get(key)
			if cached is not None:
				self._bus.emit(step_resumed(run_id, phase_name, step.label))
				return cached.result
			if snapshot.agent_calls_used >= config.max_agent_calls:
				raise WorkflowBudgetExceeded(
					f"Agent call limit reached ({config.max_agent_calls})"
				)
			if snapshot.tokens_used >= config.max_token_budget:
				raise WorkflowBudgetExceeded(
					f"Token budget exhausted ({config.max_token_budget})"
				)
			if time.time() > deadline:
				raise WorkflowTimeout(
					f"Step '{step.label}' skipped: workflow timeout"
				)
			snapshot.agent_calls_used += 1
			self._persist_snapshot_locked(handle)

		self._bus.emit(step_started(run_id, phase_name, step.label))
		start_time = time.time()
		try:
			raw_result = agent.agent_loop(messages) or ""
			self._raise_if_cancelled(handle)

			# Structured output retries use the same Agent, so local_stats includes
			# every response generated by this AgentStep attempt.
			result = raw_result
			if step.output_schema:
				result = self._validate_output(
					raw_result, step.output_schema, step, agent, messages,
					run_id, phase_name
				)
			self._raise_if_cancelled(handle)
		except Exception as exc:
			tokens = getattr(agent, "local_stats", {}).get("total_tokens", 0)
			with handle.state_lock:
				snapshot.tokens_used += tokens
				self._persist_snapshot_locked(handle)
				cancelled = (
					isinstance(exc, WorkflowCancelled)
					or handle.cancelled.is_set()
				)
			if cancelled:
				raise WorkflowCancelled("Workflow cancelled") from exc
			self._bus.emit(step_failed(run_id, phase_name, step.label, str(exc)))
			raise WorkflowError(f"AgentStep '{step.label}' failed: {exc}") from exc

		elapsed = time.time() - start_time
		tokens = getattr(agent, "local_stats", {}).get("total_tokens", 0)

		entry = JournalEntry(
			stable_key=key,
			label=step.label,
			phase=phase_name,
			step_kind="agent",
			result=result,
			tokens_used=tokens,
		)
		with handle.state_lock:
			snapshot.tokens_used += tokens
			if handle.cancelled.is_set():
				self._persist_snapshot_locked(handle)
				raise WorkflowCancelled("Workflow cancelled")
			if step.label not in snapshot.completed_steps:
				snapshot.completed_steps.append(step.label)
			self._persist_snapshot_locked(handle)
			self._store.append_journal(run_id, entry)
			journal[key] = entry

		self._bus.emit(step_completed(run_id, phase_name, step.label, {
			"tokens": tokens, "elapsed": round(elapsed, 1)
		}))
		print(
			f"\033[36m[workflow]     ✓ {step.label} "
			f"({elapsed:.1f}s, {tokens} tokens)\033[0m"
		)
		return result

	# ── ToolStep Execution ────────────────────────────────────

	def _execute_tool_step(
		self,
		step: ToolStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		phase_name: str,
		run_id: str,
		journal: dict[str, JournalEntry] | None = None,
		item_id: str = "",
	) -> Any:
		"""Reuse or journal one successfully executed logical tool call."""
		args = context.render_args(step.args_template)
		try:
			canonical_args = canonical_json(args)
		except (TypeError, ValueError) as exc:
			raise WorkflowError(
				f"ToolStep '{step.label}' resolved args must be JSON-compatible"
			) from exc

		handle = self._get_handle(run_id)
		if journal is None:
			result, _executed = self._execute_tool_step_attempt(
				step, context, snapshot, phase_name, run_id
			)
			return result
		if handle is None or handle.snapshot is not snapshot:
			raise WorkflowError(f"Run '{run_id}' has no matching live handle")

		key = compute_stable_key(
			"tool",
			step.label,
			canonical_args,
			step.tool_name,
			item_id,
		)
		cached = self._claim_journal_key(handle, journal, key)
		if cached is not None:
			self._bus.emit(step_resumed(run_id, phase_name, step.label))
			print(f"\033[36m[workflow]     ↺ {step.label} (cached)\033[0m")
			return cached.result

		try:
			result, executed = self._execute_tool_step_attempt(
				step, context, snapshot, phase_name, run_id
			)
			if executed:
				entry = JournalEntry(
					stable_key=key,
					label=step.label,
					phase=phase_name,
					step_kind="tool",
					result=result,
				)
				self._publish_journal_entry(handle, run_id, journal, entry)
			return result
		finally:
			self._release_journal_claim(handle, key)

	def _execute_tool_step_attempt(
		self,
		step: ToolStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		phase_name: str,
		run_id: str,
	) -> tuple[Any, bool]:
		"""Execute a deterministic tool call (no LLM)."""
		self._bus.emit(step_started(run_id, phase_name, step.label))

		args = context.render_args(step.args_template)
		handler = None
		if self._tool_resolver:
			handler = self._tool_resolver(step.tool_name)

		if handler is None:
			error_msg = f"Tool '{step.tool_name}' not found"
			if step.allow_failure:
				self._bus.emit(step_completed(run_id, phase_name, step.label, {
					"error": error_msg, "allowed_failure": True
				}))
				return {"error": error_msg, "exit_code": -1}, False
			raise WorkflowError(f"ToolStep '{step.label}': {error_msg}")

		start_time = time.time()
		try:
			execution = self._tool_executor.execute(
				tool_name=step.tool_name,
				args=args,
				handler=handler,
				interactive=False,
				approval_context="workflow",
			)
		except Exception as exc:
			elapsed = time.time() - start_time
			if step.allow_failure:
				result = f"Error: {exc}"
				self._bus.emit(step_completed(run_id, phase_name, step.label, {
					"error": str(exc), "elapsed": round(elapsed, 1)
				}))
				print(
					f"\033[33m[workflow]     ⚠ {step.label} failed (allowed): "
					f"{exc}\033[0m"
				)
				return {"output": result, "error": str(exc), "exit_code": -1}, False
			raise WorkflowError(
				f"ToolStep '{step.label}' failed: {exc}"
			) from exc

		if execution.status == "error":
			elapsed = time.time() - start_time
			reason = (
				execution.reason
				if execution.reason is not None
				else str(execution.output)
			)
			if step.allow_failure:
				self._bus.emit(step_completed(run_id, phase_name, step.label, {
					"error": reason, "elapsed": round(elapsed, 1)
				}))
				print(
					f"\033[33m[workflow]     ⚠ {step.label} failed (allowed): "
					f"{reason}\033[0m"
				)
				return ({
					"output": execution.output,
					"error": reason,
					"exit_code": -1,
				}, False)
			raise WorkflowError(
				f"ToolStep '{step.label}' failed: {execution.output}"
			)

		if not execution.executed:
			error_msg = (
				f"ToolStep '{step.label}' blocked: {execution.output}"
			)
			if step.allow_failure:
				self._bus.emit(step_completed(run_id, phase_name, step.label, {
					"error": execution.output,
					"allowed_failure": True,
				}))
				return ({
					"output": execution.output,
					"error": execution.output,
					"exit_code": -1,
				}, False)
			raise WorkflowError(error_msg)

		result = execution.output

		elapsed = time.time() - start_time
		self._bus.emit(step_completed(run_id, phase_name, step.label, {
			"elapsed": round(elapsed, 1)
		}))
		print(f"\033[36m[workflow]     ✓ {step.label} ({elapsed:.1f}s)\033[0m")

		# Try to parse structured output from tool result
		parsed = self._try_parse_tool_result(result)
		return parsed, True

	# ── ParallelStep Execution ────────────────────────────────

	def _execute_parallel_step(
		self,
		step: ParallelStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		journal: dict[str, JournalEntry],
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		definition: WorkflowDefinition,
		depth: int,
		deadline: float,
	) -> dict[str, Any]:
		"""Execute branches concurrently with semaphore-bounded parallelism."""
		self._bus.emit(step_started(run_id, phase_name, step.label))
		results: dict[str, Any] = {}
		semaphore = threading.Semaphore(config.max_concurrency)

		def _run_branch(branch: AgentStep | ToolStep) -> tuple[str, Any]:
			with semaphore:
				if isinstance(branch, AgentStep):
					r = self._execute_agent_step(
						branch, context, snapshot, journal, config,
						phase_name, run_id, deadline
					)
				elif isinstance(branch, ToolStep):
					r = self._execute_tool_step(
						branch, context, snapshot, phase_name, run_id, journal=journal
					)
				else:
					r = None
				return branch.label, r

		with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
			futures = {
				pool.submit(_run_branch, branch): branch
				for branch in step.branches
			}
			for future in as_completed(futures):
				branch = futures[future]
				try:
					label, result = future.result()
					results[label] = result
				except Exception as exc:
					if isinstance(branch, ToolStep) and branch.allow_failure:
						results[branch.label] = {"error": str(exc)}
					else:
						raise

		# Sort by branch label for deterministic stable keys across resumes
		results = dict(sorted(results.items()))
		self._bus.emit(step_completed(run_id, phase_name, step.label, {
			"branches": len(results)
		}))
		return results

	# ── PipelineStep Execution ────────────────────────────────

	def _execute_pipeline_step(
		self,
		step: PipelineStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		journal: dict[str, JournalEntry],
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		definition: WorkflowDefinition,
		depth: int,
		deadline: float,
	) -> list[Any]:
		"""Execute items through stages: item-internal sequential, cross-item parallel."""
		self._bus.emit(step_started(run_id, phase_name, step.label))

		items = context.resolve(step.items_key)
		if items is None:
			items = []
		if not isinstance(items, list):
			items = [items]

		semaphore = threading.Semaphore(
			min(step.max_item_concurrency, config.max_concurrency)
		)

		def _process_item(idx: int, item: Any) -> dict[str, Any]:
			with semaphore:
				# Create per-item context overlay
				item_context = WorkflowContext(context.inputs)
				item_context.steps = dict(context.steps)
				item_context.set("item", item)
				item_context.set("item_index", idx)

				item_id = ""
				if isinstance(item, dict):
					item_id = str(
						item.get("case_id")
						or item.get("id")
						or item.get("group_id")
						or idx
					)
				else:
					item_id = str(idx)

				journal_business_id = item_id
				if isinstance(item, dict):
					for identity_key in ("case_id", "id", "group_id"):
						if identity_key in item and item[identity_key] is not None:
							journal_business_id = str(item[identity_key])
							break
				journal_item_id = f"{idx}:{journal_business_id}"
				item_results: dict[str, Any] = {"item_id": item_id, "item": item}
				for stage in step.stages:
					if time.time() > deadline:
						raise WorkflowTimeout(
							f"Pipeline '{step.label}' item {item_id} timeout"
						)
					if isinstance(stage, AgentStep):
						result = self._execute_agent_step(
							stage, item_context, snapshot, journal, config,
							phase_name, run_id, deadline, item_id=journal_item_id,
							legacy_item_id=item_id,
						)
					elif isinstance(stage, ToolStep):
						result = self._execute_tool_step(
							stage, item_context, snapshot, phase_name, run_id,
							journal=journal, item_id=journal_item_id
						)
					else:
						result = None
					item_context.set(stage.output_key, result)
					item_results[stage.output_key] = result
				return item_results

		results: list[Any] = []
		with ThreadPoolExecutor(max_workers=step.max_item_concurrency) as pool:
			futures = {
				pool.submit(_process_item, idx, item): idx
				for idx, item in enumerate(items)
			}
			# Collect in order
			ordered: dict[int, Any] = {}
			for future in as_completed(futures):
				idx = futures[future]
				try:
					ordered[idx] = future.result()
				except Exception as exc:
					ordered[idx] = {"error": str(exc), "item_index": idx}

			results = [ordered[i] for i in sorted(ordered.keys())]

		self._bus.emit(step_completed(run_id, phase_name, step.label, {
			"items_processed": len(results)
		}))
		return results

	# ── WorkflowStep (Nested) Execution ───────────────────────

	def _execute_workflow_step(
		self,
		step: WorkflowStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		depth: int,
		deadline: float,
		journal: dict[str, JournalEntry] | None = None,
		item_id: str = "",
	) -> Any:
		"""Reuse or journal one successfully completed nested workflow."""
		nested_inputs: dict[str, Any] = {}
		for key, template in step.inputs_mapping.items():
			if isinstance(template, str):
				rendered = context.render(template)
				try:
					nested_inputs[key] = json.loads(rendered)
				except (json.JSONDecodeError, TypeError):
					nested_inputs[key] = rendered
			else:
				nested_inputs[key] = template

		try:
			canonical_inputs = canonical_json(nested_inputs)
		except (TypeError, ValueError) as exc:
			raise WorkflowError(
				f"WorkflowStep '{step.label}' inputs must be JSON-compatible"
			) from exc

		handle = self._get_handle(run_id)
		if journal is None:
			return self._execute_workflow_step_attempt(
				step, context, snapshot, config, phase_name, run_id, depth, deadline
			)
		if handle is None or handle.snapshot is not snapshot:
			raise WorkflowError(f"Run '{run_id}' has no matching live handle")

		key = compute_stable_key(
			"workflow",
			step.label,
			canonical_inputs,
			step.workflow_name,
			item_id,
		)
		cached = self._claim_journal_key(handle, journal, key)
		if cached is not None:
			self._bus.emit(step_resumed(run_id, phase_name, step.label))
			print(f"\033[36m[workflow]     ↺ {step.label} (cached)\033[0m")
			return cached.result

		try:
			output = self._execute_workflow_step_attempt(
				step, context, snapshot, config, phase_name, run_id, depth, deadline
			)
			entry = JournalEntry(
				stable_key=key,
				label=step.label,
				phase=phase_name,
				step_kind="workflow",
				result=output,
			)
			self._publish_journal_entry(handle, run_id, journal, entry)
			return output
		finally:
			self._release_journal_claim(handle, key)

	def _execute_workflow_step_attempt(
		self,
		step: WorkflowStep,
		context: WorkflowContext,
		snapshot: RunSnapshot,
		config: WorkflowConfig,
		phase_name: str,
		run_id: str,
		depth: int,
		deadline: float,
	) -> Any:
		"""Invoke a nested workflow synchronously (blocks until complete)."""
		self._bus.emit(step_started(run_id, phase_name, step.label))

		# Resolve inputs mapping
		nested_inputs: dict[str, Any] = {}
		for key, template in step.inputs_mapping.items():
			if isinstance(template, str):
				rendered = context.render(template)
				# Try JSON parse for complex values
				try:
					nested_inputs[key] = json.loads(rendered)
				except (json.JSONDecodeError, TypeError):
					nested_inputs[key] = rendered
			else:
				nested_inputs[key] = template

		# Start nested workflow (synchronous wait)
		nested_run_id, error = self.start(
			step.workflow_name, nested_inputs,
			depth=depth + 1, parent_run_id=run_id,
		)
		if error:
			raise WorkflowError(
				f"Nested workflow '{step.workflow_name}' failed to start: {error}"
			)

		# Wait for nested run to complete
		while True:
			if time.time() > deadline:
				self.cancel(nested_run_id)
				raise WorkflowTimeout(
					f"Nested workflow '{step.workflow_name}' timed out"
				)
			nested_snapshot = self._store.load_snapshot(nested_run_id)
			if nested_snapshot is None:
				raise WorkflowError(f"Nested run '{nested_run_id}' disappeared")
			if nested_snapshot.status in (
				RunStatus.COMPLETED.value,
				RunStatus.FAILED.value,
				RunStatus.CANCELLED.value,
			):
				break
			time.sleep(0.5)

		if nested_snapshot.status != RunStatus.COMPLETED.value:
			raise WorkflowError(
				f"Nested workflow '{step.workflow_name}' ended with status "
				f"'{nested_snapshot.status}': {nested_snapshot.error}"
			)

		# Propagate nested resource usage
		handle = self._get_handle(run_id)
		if handle is None or handle.snapshot is not snapshot:
			raise WorkflowError(f"Run '{run_id}' has no matching live handle")
		with handle.state_lock:
			self._raise_if_cancelled(handle)
			snapshot.agent_calls_used += nested_snapshot.agent_calls_used
			snapshot.tokens_used += nested_snapshot.tokens_used
			self._persist_snapshot_locked(handle)

		output = self._store.load_output(nested_run_id)
		self._bus.emit(step_completed(run_id, phase_name, step.label, {
			"nested_run_id": nested_run_id
		}))
		print(
			f"\033[36m[workflow]     ✓ {step.label} "
			f"(nested {step.workflow_name} → {nested_run_id})\033[0m"
		)
		return output

	# ── Output Validation ─────────────────────────────────────

	def _validate_output(
		self,
		raw: str,
		schema: dict,
		step: AgentStep,
		agent: Any,
		messages: list,
		run_id: str,
		phase_name: str,
	) -> Any:
		"""Parse and validate structured output; retry once on failure."""
		for attempt in range(1 + step.max_retries):
			parsed = self._try_parse_json(raw)
			if parsed is not None and self._schema_check(parsed, schema):
				return parsed

			if attempt < step.max_retries:
				# Retry: ask the agent to fix its output
				self._bus.emit(log_entry(run_id,
					f"Step '{step.label}' output validation failed, retrying..."
				))
				messages.append({
					"role": "user",
					"content": (
						"Your previous output did not match the required JSON schema. "
						"Please re-output ONLY valid JSON matching the schema. "
						"No markdown fences, no explanation."
					),
				})
				try:
					raw = agent.agent_loop(messages) or ""
				except Exception:
					break

		# Final fallback: return raw text
		self._bus.emit(log_entry(run_id,
			f"Step '{step.label}': schema validation failed after retries, "
			"returning raw text"
		))
		return raw

	# ── Condition Evaluation ──────────────────────────────────

	def _evaluate_condition(self, condition: str, context: WorkflowContext) -> bool:
		"""Evaluate a simple condition expression against the context.

		Supported patterns:
		  "{key} == value"    "{key} != value"
		  "{key} > number"    "{key} < number"
		  "{key} is_none"     "{key} is_not_none"
		"""
		condition = condition.strip()

		# is_none / is_not_none
		if condition.endswith("is_none"):
			key = condition[:-len("is_none")].strip().strip("{}")
			return context.resolve(key) is None
		if condition.endswith("is_not_none"):
			key = condition[:-len("is_not_none")].strip().strip("{}")
			return context.resolve(key) is not None

		# == / !=
		for op in ("==", "!="):
			if op in condition:
				left_str, right_str = condition.split(op, 1)
				left_key = left_str.strip().strip("{}")
				right_val = right_str.strip().strip("'\"")
				left_val = context.resolve(left_key)
				if op == "==":
					return str(left_val) == right_val
				else:
					return str(left_val) != right_val

		# > / <
		for op in (">=", "<=", ">", "<"):
			if op in condition:
				left_str, right_str = condition.split(op, 1)
				left_key = left_str.strip().strip("{}")
				left_val = context.resolve(left_key)
				try:
					right_num = float(right_str.strip())
					left_num = float(left_val) if left_val is not None else 0
				except (ValueError, TypeError):
					return True  # can't compare → don't skip
				if op == ">":
					return left_num > right_num
				elif op == "<":
					return left_num < right_num
				elif op == ">=":
					return left_num >= right_num
				elif op == "<=":
					return left_num <= right_num

		# Fallback: treat as truthy check
		key = condition.strip("{}")
		value = context.resolve(key)
		return bool(value)

	# ── Helpers ───────────────────────────────────────────────

	@staticmethod
	def _try_parse_json(text: str) -> Any | None:
		"""Try to extract JSON from text (handles markdown fences)."""
		text = text.strip()
		# Strip markdown code fences
		if text.startswith("```"):
			lines = text.split("\n")
			# Remove first and last fence lines
			lines = [l for l in lines if not l.strip().startswith("```")]
			text = "\n".join(lines)
		try:
			return json.loads(text)
		except (json.JSONDecodeError, TypeError):
			# Try to find JSON object in text
			start = text.find("{")
			end = text.rfind("}")
			if start != -1 and end > start:
				try:
					return json.loads(text[start:end + 1])
				except (json.JSONDecodeError, TypeError):
					pass
			# Try JSON array
			start = text.find("[")
			end = text.rfind("]")
			if start != -1 and end > start:
				try:
					return json.loads(text[start:end + 1])
				except (json.JSONDecodeError, TypeError):
					pass
		return None

	@staticmethod
	def _schema_check(data: Any, schema: dict) -> bool:
		"""Lightweight schema validation (type + required fields).

		Not a full JSON Schema validator — checks the top-level structure
		is sufficient for workflow output validation.
		"""
		if not isinstance(schema, dict):
			return True
		expected_type = schema.get("type")
		if expected_type == "object":
			if not isinstance(data, dict):
				return False
			required = schema.get("required", [])
			for field_name in required:
				if field_name not in data:
					return False
		elif expected_type == "array":
			if not isinstance(data, list):
				return False
		return True

	@staticmethod
	def _try_parse_tool_result(result: str) -> Any:
		"""Try to parse tool output as structured data."""
		if not isinstance(result, str):
			return result
		# Try JSON
		try:
			return json.loads(result)
		except (json.JSONDecodeError, TypeError):
			pass
		# Return as dict with output field
		return {"output": result}

	def _build_final_output(
		self, context: WorkflowContext, definition: WorkflowDefinition
	) -> Any:
		"""Build the final output from the last phase's last step."""
		if definition.phases:
			last_phase = definition.phases[-1]
			if last_phase.steps:
				last_step = last_phase.steps[-1]
				result = context.steps.get(last_step.output_key)
				if result is not None:
					return result
		# Fallback: return all step outputs
		return dict(context.steps)

	def _deliver_completion(
		self, run_id: str, workflow_name: str, snapshot: RunSnapshot
	) -> None:
		"""Deliver completion notification to the leader agent."""
		if self._delivery_handler is None:
			return
		message = (
			f"[Workflow Completed] {workflow_name} (run_id={run_id})\n"
			f"Agent calls: {snapshot.agent_calls_used}, "
			f"Tokens: {snapshot.tokens_used}\n"
			f"Use workflow_status(run_id=\"{run_id}\") for the full result."
		)
		try:
			self._delivery_handler(message)
		except Exception:  # noqa: BLE001
			pass
