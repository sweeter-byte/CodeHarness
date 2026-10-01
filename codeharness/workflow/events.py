"""Workflow progress events and event bus.

Events are emitted by the Runtime during execution and consumed by
observers (CLI, TUI, async delivery handler). The bus is a simple
pub-sub with no external dependencies.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Event Types ───────────────────────────────────────────────


class EventType(str, Enum):
	WORKFLOW_STARTED = "workflow_started"
	WORKFLOW_COMPLETED = "workflow_completed"
	WORKFLOW_FAILED = "workflow_failed"
	PHASE_STARTED = "phase_started"
	PHASE_COMPLETED = "phase_completed"
	STEP_STARTED = "step_started"
	STEP_COMPLETED = "step_completed"
	STEP_RESUMED = "step_resumed"
	STEP_FAILED = "step_failed"
	STEP_SKIPPED = "step_skipped"
	LOG_ENTRY = "log_entry"


@dataclass
class ProgressEvent:
	"""One event emitted during Workflow execution."""

	event_type: str
	run_id: str
	timestamp: float = field(default_factory=time.time)
	# Common fields
	workflow_name: str = ""
	phase_name: str = ""
	step_label: str = ""
	# Payload
	data: dict[str, Any] = field(default_factory=dict)
	message: str = ""


# ── Event Bus ─────────────────────────────────────────────────

EventCallback = Callable[[ProgressEvent], None]


class WorkflowEventBus:
	"""Simple synchronous pub-sub for workflow progress events.

	Subscribers are called in registration order on the emitting thread.
	A subscriber exception is caught and logged (never breaks execution).
	"""

	def __init__(self) -> None:
		self._subscribers: list[EventCallback] = []
		self._lock = threading.Lock()
		self._history: dict[str, list[ProgressEvent]] = {}  # run_id → events

	def subscribe(self, callback: EventCallback) -> Callable[[], None]:
		"""Register a callback; returns an unsubscribe function."""
		with self._lock:
			self._subscribers.append(callback)

		def unsubscribe() -> None:
			with self._lock:
				try:
					self._subscribers.remove(callback)
				except ValueError:
					pass

		return unsubscribe

	def emit(self, event: ProgressEvent) -> None:
		"""Dispatch an event to all subscribers and record in history."""
		# Record in per-run history
		with self._lock:
			if event.run_id not in self._history:
				self._history[event.run_id] = []
			self._history[event.run_id].append(event)
			subscribers = list(self._subscribers)

		for callback in subscribers:
			try:
				callback(event)
			except Exception:  # noqa: BLE001
				pass  # never let a subscriber break the runtime

	def history(self, run_id: str) -> list[ProgressEvent]:
		"""Retrieve all events for a given run."""
		with self._lock:
			return list(self._history.get(run_id, []))

	def clear_history(self, run_id: str | None = None) -> None:
		"""Clear event history for one run or all runs."""
		with self._lock:
			if run_id is None:
				self._history.clear()
			else:
				self._history.pop(run_id, None)


# ── Convenience constructors ──────────────────────────────────


def workflow_started(run_id: str, workflow_name: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.WORKFLOW_STARTED,
		run_id=run_id,
		workflow_name=workflow_name,
		message=f"Workflow '{workflow_name}' started",
	)


def workflow_completed(run_id: str, workflow_name: str, data: dict | None = None) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.WORKFLOW_COMPLETED,
		run_id=run_id,
		workflow_name=workflow_name,
		data=data or {},
		message=f"Workflow '{workflow_name}' completed",
	)


def workflow_failed(run_id: str, workflow_name: str, error: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.WORKFLOW_FAILED,
		run_id=run_id,
		workflow_name=workflow_name,
		data={"error": error},
		message=f"Workflow '{workflow_name}' failed: {error}",
	)


def phase_started(run_id: str, phase_name: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.PHASE_STARTED,
		run_id=run_id,
		phase_name=phase_name,
		message=f"Phase '{phase_name}' started",
	)


def phase_completed(run_id: str, phase_name: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.PHASE_COMPLETED,
		run_id=run_id,
		phase_name=phase_name,
		message=f"Phase '{phase_name}' completed",
	)


def step_started(run_id: str, phase_name: str, step_label: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.STEP_STARTED,
		run_id=run_id,
		phase_name=phase_name,
		step_label=step_label,
		message=f"Step '{step_label}' started",
	)


def step_completed(
	run_id: str, phase_name: str, step_label: str,
	data: dict | None = None,
) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.STEP_COMPLETED,
		run_id=run_id,
		phase_name=phase_name,
		step_label=step_label,
		data=data or {},
		message=f"Step '{step_label}' completed",
	)


def step_resumed(run_id: str, phase_name: str, step_label: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.STEP_RESUMED,
		run_id=run_id,
		phase_name=phase_name,
		step_label=step_label,
		message=f"Step '{step_label}' resumed from journal",
	)


def step_failed(run_id: str, phase_name: str, step_label: str, error: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.STEP_FAILED,
		run_id=run_id,
		phase_name=phase_name,
		step_label=step_label,
		data={"error": error},
		message=f"Step '{step_label}' failed: {error}",
	)


def step_skipped(run_id: str, phase_name: str, step_label: str, reason: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.STEP_SKIPPED,
		run_id=run_id,
		phase_name=phase_name,
		step_label=step_label,
		data={"reason": reason},
		message=f"Step '{step_label}' skipped: {reason}",
	)


def log_entry(run_id: str, message: str) -> ProgressEvent:
	return ProgressEvent(
		event_type=EventType.LOG_ENTRY,
		run_id=run_id,
		message=message,
	)


# ── Module-level singleton ────────────────────────────────────

EVENT_BUS = WorkflowEventBus()
