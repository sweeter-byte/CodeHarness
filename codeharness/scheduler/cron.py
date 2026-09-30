"""Cron Scheduler — time-based task triggering for the Agent system.

Provides CronScheduler to schedule recurring or one-shot tasks that are
delivered to the Agent Loop when it is idle, enabling time-driven automation.
"""

import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────

SCHEDULED_TASKS_FILE = Path(".scheduled_tasks.json")
POLL_INTERVAL = 1.0       # Scheduler polls every 1 second
QUEUE_POLL_INTERVAL = 0.2 # Queue processor checks every 200ms
STOP_JOIN_TIMEOUT = 2.0   # Bound for joining scheduler/processor threads on stop

# Global stop event for graceful shutdown.
RUNTIME_STOP = threading.Event()

_delivery_handler: Callable[[str], bool] | None = None
_status_handler: Callable[[str], None] | None = None


def _emit_status(message: str) -> None:
	if _status_handler is not None:
		_status_handler(message)

# ── Data Model ─────────────────────────────────────────────────


@dataclass
class CronJob:
	id: str
	cron: str
	prompt: str
	recurring: bool
	durable: bool
	pending_delivery: bool = False
	last_fired: str | None = None


# ── Cron Expression Parsing ────────────────────────────────────

# Field ranges: (min, max)
_CRON_FIELDS = [
	(0, 59),   # minute
	(0, 23),   # hour
	(1, 31),   # day of month
	(1, 12),   # month
	(0, 6),    # day of week (0=Monday)
]


def _parse_field(token: str, lo: int, hi: int) -> set[int] | None:
	"""Parse a single cron field token into a set of matching integers.
	Returns None if the token is invalid."""
	values = set()
	for part in token.split(","):
		part = part.strip()
		if not part:
			return None
		# */N
		if part.startswith("*/"):
			try:
				step = int(part[2:])
			except ValueError:
				return None
			if step <= 0:
				return None
			values.update(range(lo, hi + 1, step))
		# N-M
		elif "-" in part:
			bounds = part.split("-", 1)
			try:
				a, b = int(bounds[0]), int(bounds[1])
			except ValueError:
				return None
			if a < lo or b > hi or a > b:
				return None
			values.update(range(a, b + 1))
		# *
		elif part == "*":
			values.update(range(lo, hi + 1))
		# N
		else:
			try:
				n = int(part)
			except ValueError:
				return None
			if n < lo or n > hi:
				return None
			values.add(n)
	return values


def validate_cron(expr: str) -> str | None:
	"""Validate a 5-field cron expression.
	Returns None if valid, or an error message string if invalid."""
	tokens = expr.strip().split()
	if len(tokens) != 5:
		return f"Expected 5 fields, got {len(tokens)}"
	for token, (lo, hi) in zip(tokens, _CRON_FIELDS):
		if _parse_field(token, lo, hi) is None:
			return f"Invalid cron field '{token}' (range {lo}-{hi})"
	return None


def cron_matches(cron: str, moment: datetime) -> bool:
	"""Check if a cron expression matches the given datetime."""
	tokens = cron.strip().split()
	# datetime fields: minute, hour, day, month, weekday (0=Monday)
	values = [
		moment.minute,
		moment.hour,
		moment.day,
		moment.month,
		moment.weekday(),
	]
	for token, val, (lo, hi) in zip(tokens, values, _CRON_FIELDS):
		allowed = _parse_field(token, lo, hi)
		if allowed is None or val not in allowed:
			return False
	return True


# ── CronStore: Persistence ─────────────────────────────────────


class CronStore:
	"""File-backed store for durable cron jobs with atomic writes."""

	def __init__(self, path: str | Path = SCHEDULED_TASKS_FILE):
		self.path = Path(path)
		self.jobs: dict[str, CronJob] = {}

	def load(self) -> None:
		"""Load durable jobs from disk. Reports errors but does not crash."""
		if not self.path.exists():
			return
		try:
			data = json.loads(self.path.read_text(encoding="utf-8"))
			for item in data:
				job = CronJob(**item)
				# Reset pending_delivery on reload (in-flight deliveries are lost)
				job.pending_delivery = False
				self.jobs[job.id] = job
			_emit_status(
				f"\033[36m[Cron] Loaded {len(self.jobs)} durable job(s) "
				f"from {self.path}\033[0m"
			)
		except (json.JSONDecodeError, KeyError, TypeError) as e:
			_emit_status(
				f"\033[31m[Cron] Error loading {self.path}: {e}. "
				"Starting with empty state.\033[0m"
			)

	def save(self) -> None:
		"""Atomically write all durable jobs to disk."""
		durable_jobs = [asdict(j) for j in self.jobs.values() if j.durable]
		tmp_path = self.path.with_suffix(".tmp")
		try:
			tmp_path.write_text(
				json.dumps(durable_jobs, indent=2, ensure_ascii=False),
				encoding="utf-8",
			)
			os.replace(tmp_path, self.path)
		except OSError as e:
			_emit_status(f"\033[31m[Cron] Failed to save: {e}\033[0m")
			raise

	@staticmethod
	def _generate_id() -> str:
		return f"cron_{secrets.token_hex(4)}"

	def unique_id(self) -> str:
		for _ in range(100):
			jid = self._generate_id()
			if jid not in self.jobs:
				return jid
		raise RuntimeError("Failed to generate unique cron job ID after 100 attempts")

	def add(self, cron: str, prompt: str, recurring: bool, durable: bool) -> CronJob:
		jid = self.unique_id()
		job = CronJob(
			id=jid,
			cron=cron,
			prompt=prompt,
			recurring=recurring,
			durable=durable,
		)
		self.jobs[jid] = job
		if durable:
			self.save()
		return job

	def remove(self, job_id: str) -> bool:
		if job_id not in self.jobs:
			return False
		job = self.jobs.pop(job_id)
		if job.durable:
			self.save()
		return True

	def list_all(self) -> list[CronJob]:
		return list(self.jobs.values())


# ── Delivery Queue ─────────────────────────────────────────────

_delivery_queue: list[CronJob] = []

# 保护的是 _delivery_queue 共享列表
# Scheduler 线程：调用 _enqueue_due_job() 往队列里写入到期任务
# Queue Processor 线程：调用 consume_delivery_queue() 从队列里读取并清空
_delivery_lock = threading.Lock()


def _enqueue_due_job(job: CronJob) -> None:
	"""Add a job to the delivery queue (called by scheduler thread)."""
	with _delivery_lock:
		_delivery_queue.append(job)


def consume_delivery_queue() -> list[CronJob]:
	"""Drain and return all pending deliveries (called by queue processor)."""
	with _delivery_lock:
		jobs = list(_delivery_queue)
		_delivery_queue.clear()
	return jobs


def _restore_delivery_queue(jobs: list[CronJob]) -> None:
	"""Put an unaccepted batch back ahead of newly queued jobs."""
	with _delivery_lock:
		_delivery_queue[0:0] = jobs


def has_delivery_queue() -> bool:
	with _delivery_lock:
		return len(_delivery_queue) > 0


# ── Scheduler Thread ───────────────────────────────────────────

_cron_store: CronStore | None = None


def _scheduler_loop(stop_event: threading.Event) -> None:
	"""Poll local time every second; enqueue jobs that match cron expressions."""
	while not stop_event.is_set():
		moment = datetime.now()
		minute_marker = moment.strftime("%Y-%m-%d %H:%M")
		if _cron_store is not None:
			for job in list(_cron_store.jobs.values()):
				if job.pending_delivery or job.last_fired == minute_marker:
					continue
				if cron_matches(job.cron, moment):
					# Mark as pending before persisting
					job.pending_delivery = True
					job.last_fired = minute_marker
					try:
						if job.durable:
							_cron_store.save()
					except OSError:
						# Rollback on persistence failure
						job.pending_delivery = False
						job.last_fired = None
						continue
					_enqueue_due_job(job)
		stop_event.wait(POLL_INTERVAL)


def _queue_processor_loop(stop_event: threading.Event) -> None:
	"""Offer queued jobs to the Runtime and retain unaccepted deliveries."""
	while not stop_event.is_set():
		if not has_delivery_queue() or _delivery_handler is None:
			stop_event.wait(QUEUE_POLL_INTERVAL)
			continue

		fired = consume_delivery_queue()
		content = "\n".join(f"[Scheduled] {job.prompt}" for job in fired)
		for job in fired:
			_emit_status(
				f"\033[36m[Cron] Delivering {job.id}: {job.prompt}\033[0m"
			)

		try:
			accepted = _delivery_handler(content)
		except Exception as exc:
			accepted = False
			_emit_status(
				f"\033[31m[Cron] Delivery failed: {exc}. Will retry.\033[0m"
			)

		if not accepted:
			_restore_delivery_queue(fired)
			stop_event.wait(QUEUE_POLL_INTERVAL)
			continue

		for job in fired:
			if not job.recurring:
				_cron_store.remove(job.id)
			else:
				job.pending_delivery = False
				if job.durable:
					_cron_store.save()
		stop_event.wait(QUEUE_POLL_INTERVAL)


# ── Public API ─────────────────────────────────────────────────

_scheduler_thread: threading.Thread | None = None
_processor_thread: threading.Thread | None = None


def start(
	delivery_handler: Callable[[str], bool],
	status_handler: Callable[[str], None] | None = None,
) -> None:
	"""Initialize and start the cron scheduler threads.

	Refuses to start while a previous generation of threads is still alive:
	clearing RUNTIME_STOP here would resurrect the old runtime. Dead thread
	references are treated as stale and cleaned up before starting fresh.
	"""
	global _cron_store, _scheduler_thread, _processor_thread
	global _delivery_handler, _status_handler

	for thread in (_scheduler_thread, _processor_thread):
		if thread is not None and thread.is_alive():
			raise RuntimeError("previous cron runtime is still stopping")
	_scheduler_thread = None
	_processor_thread = None

	_delivery_handler = delivery_handler
	_status_handler = status_handler

	_cron_store = CronStore()
	_cron_store.load()

	RUNTIME_STOP.clear()

	_scheduler_thread = threading.Thread(
		target=_scheduler_loop,
		args=(RUNTIME_STOP,),
		daemon=True,
	)
	_processor_thread = threading.Thread(
		target=_queue_processor_loop,
		args=(RUNTIME_STOP,),
		daemon=True,
	)
	_scheduler_thread.start()
	_processor_thread.start()
	_emit_status("\033[36m[Cron] Scheduler started\033[0m")


def stop() -> None:
	"""Stop scheduler threads and release Runtime-owned references.

	Signals the stop event and joins both threads with a bounded timeout
	(never waits forever — a delivery in progress may outlive the join).
	join() returning does NOT mean the thread exited, so state is only
	released once both threads are verified dead; a thread still running
	may be mid-delivery and needs _cron_store / _delivery_handler to
	finish its current batch (one-shot remove, recurring reset, save).
	In that case the references and RUNTIME_STOP are kept as-is and a
	later stop() call completes the cleanup (stop is idempotent).
	Durable jobs stay on disk and are reloaded by the next start().
	"""
	global _scheduler_thread, _processor_thread, _cron_store
	global _delivery_handler, _status_handler

	RUNTIME_STOP.set()

	current = threading.current_thread()
	still_alive = False
	for thread in (_scheduler_thread, _processor_thread):
		if thread is not None and thread is not current:
			thread.join(timeout=STOP_JOIN_TIMEOUT)
		if thread is not None and thread.is_alive():
			still_alive = True

	if still_alive:
		# Honest state: keep thread refs, handlers, store and the stop
		# signal so the running thread can finish its current delivery.
		_emit_status(
			"\033[33m[Cron] Cron thread is still stopping; "
			"state retained until it exits\033[0m"
		)
		return

	_scheduler_thread = None
	_processor_thread = None

	# In-memory queue is transient; durable jobs are already persisted.
	with _delivery_lock:
		_delivery_queue.clear()

	_emit_status("\033[36m[Cron] Scheduler stopped\033[0m")

	_delivery_handler = None
	_status_handler = None
	_cron_store = None


def get_store() -> CronStore | None:
	"""Return the cron store (for tool handlers to access)."""
	return _cron_store


# ── Cron Tools (schema + handler, owned by the cron module) ──
# Migrated from the legacy root tools.py (Phase 2); the scheduling
# mechanism itself is unchanged. When the scheduler moves into
# codeharness/ in a later phase, these tools move with it.


def run_cron_create(cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
	store = get_store()
	if store is None:
		return "Error: Cron scheduler not initialized"
	err = validate_cron(cron)
	if err:
		return f"Error: Invalid cron expression - {err}"
	if not prompt or not prompt.strip():
		return "Error: Prompt cannot be empty"
	job = store.add(cron, prompt.strip(), recurring, durable)
	return f"Created {job.id}: cron='{job.cron}', recurring={job.recurring}, durable={job.durable}"


def run_cron_list() -> str:
	store = get_store()
	if store is None:
		return "(cron scheduler not initialized)"
	jobs = store.list_all()
	if not jobs:
		return "(no scheduled tasks)"
	lines = []
	for j in jobs:
		rec = "recurring" if j.recurring else "one-shot"
		dur = "durable" if j.durable else "memory"
		lines.append(f"{j.id} | {j.cron:<15} | {rec:<10} | {dur:<8} | {j.prompt}")
	return "\n".join(lines)


def run_cron_delete(job_id: str) -> str:
	store = get_store()
	if store is None:
		return "Error: Cron scheduler not initialized"
	if store.remove(job_id):
		return f"Deleted {job_id}"
	return f"Error: Job not found: {job_id}"


CRON_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "cron_create",
			"description": (
				"Schedule a recurring or one-shot task. The prompt will be delivered "
				"to the agent at the specified time. Use cron expressions to define timing."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"cron": {
						"type": "string",
						"description": "5-field cron expression (minute hour day month weekday).",
					},
					"prompt": {
						"type": "string",
						"description": "Task description delivered to the agent when triggered.",
					},
					"recurring": {
						"type": "boolean",
						"description": "True for recurring, False for one-shot. Default True.",
					},
					"durable": {
						"type": "boolean",
						"description": "True to persist across restarts. Default True.",
					},
				},
				"required": ["cron", "prompt"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "cron_list",
			"description": "Show all scheduled tasks: ID, cron expression, prompt, recurring status.",
			"parameters": {
				"type": "object",
				"properties": {},
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "cron_delete",
			"description": "Remove a scheduled task by its ID.",
			"parameters": {
				"type": "object",
				"properties": {
					"job_id": {
						"type": "string",
						"description": "ID of the scheduled job to remove.",
					},
				},
				"required": ["job_id"],
			},
		},
	},
]

CRON_HANDLERS = {
	"cron_create": run_cron_create,
	"cron_list":   run_cron_list,
	"cron_delete": run_cron_delete,
}
