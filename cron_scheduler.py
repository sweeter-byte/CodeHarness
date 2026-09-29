"""Cron Scheduler — time-based task triggering for the Agent system.

Provides CronScheduler to schedule recurring or one-shot tasks that are
delivered to the Agent Loop when it is idle, enabling time-driven automation.
"""

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────

SCHEDULED_TASKS_FILE = Path(".scheduled_tasks.json")
POLL_INTERVAL = 1.0       # Scheduler polls every 1 second
QUEUE_POLL_INTERVAL = 0.2 # Queue processor checks every 200ms

# ── Shared State ───────────────────────────────────────────────

# Lock to prevent concurrent modification of history by user input and cron delivery.
agent_lock = threading.Lock()

# Flag indicating the Agent is currently executing a scheduled (cron) turn.
# Permission hook checks this to reject interactive approvals.
CRON_TURN = False

# Flag indicating the main thread is at input() waiting for user input.
# Queue Processor defers its print() calls while this is True to avoid
# interleaving output with the terminal's input line.
UI_BUSY = False

# Global stop event for graceful shutdown.
RUNTIME_STOP = threading.Event()

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
			print(f"\033[36m[Cron] Loaded {len(self.jobs)} durable job(s) from {self.path}\033[0m")
		except (json.JSONDecodeError, KeyError, TypeError) as e:
			print(f"\033[31m[Cron] Error loading {self.path}: {e}. Starting with empty state.\033[0m")

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
			print(f"\033[31m[Cron] Failed to save: {e}\033[0m")
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


# ── Queue Processor Thread ─────────────────────────────────────

_agent_ref = None  # Set during start() to avoid circular import
_history_ref: list | None = None  # Reference to the shared history list


def _queue_processor_loop(stop_event: threading.Event) -> None:
	"""Check delivery queue every 200ms; when Agent is idle, deliver jobs."""
	global CRON_TURN
	while not stop_event.is_set():
		# Don't compete for the lock or start delivery while the user is
		# at input() — their typed input and our print() would interleave
		# on the shared terminal, corrupting the display.
		if UI_BUSY or not has_delivery_queue() or not agent_lock.acquire(blocking=False):
			stop_event.wait(QUEUE_POLL_INTERVAL)
			continue
		try:
			if has_delivery_queue() and _agent_ref is not None and _history_ref is not None:
				CRON_TURN = True
				fired = consume_delivery_queue()
				# Inject scheduled messages into history
				for job in fired:
					_history_ref.append({
						"role": "user",
						"content": f"[Scheduled] {job.prompt}",
					})
					print(f"\033[36m[Cron] Delivering {job.id}: {job.prompt}\033[0m")
				# Run agent loop with the injected messages
				try:
					result = _agent_ref.agent_loop(_history_ref)
					# Show the final response (the main loop won't print it —
					# it's still blocked at input()).
					if result:
						print(f"\n{result}\n")
					# Success: remove completed one-shot jobs, reset recurring
					for job in fired:
						if not job.recurring:
							_cron_store.remove(job.id)
						else:
							job.pending_delivery = False
							if job.durable:
								_cron_store.save()
				except Exception as e:
					# Failure: rollback — remove injected messages, keep pending
					print(f"\033[31m[Cron] Delivery failed: {e}. Will retry.\033[0m")
					for job in fired:
						job.pending_delivery = True
						# Remove the injected message
						for i in range(len(_history_ref) - 1, -1, -1):
							if _history_ref[i].get("content") == f"[Scheduled] {job.prompt}":
								_history_ref.pop(i)
								break
		finally:
			CRON_TURN = False
			agent_lock.release()
			# Re-print the input prompt so the user knows they can type again.
			# The main thread is blocked at input() whose prompt was already
			# printed — our output above moved the cursor, so re-show it.
			print("\033[36m>> \033[0m", end="", flush=True)
		stop_event.wait(QUEUE_POLL_INTERVAL)


# ── Public API ─────────────────────────────────────────────────

_scheduler_thread: threading.Thread | None = None
_processor_thread: threading.Thread | None = None


def start(agent=None, history: list | None = None) -> None:
	"""Initialize and start the cron scheduler threads."""
	global _cron_store, _scheduler_thread, _processor_thread, _agent_ref, _history_ref

	_cron_store = CronStore()
	_cron_store.load()

	_agent_ref = agent
	_history_ref = history

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
	print("\033[36m[Cron] Scheduler started\033[0m")


def stop() -> None:
	"""Signal scheduler threads to stop."""
	RUNTIME_STOP.set()
	print("\033[36m[Cron] Scheduler stopped\033[0m")


def get_store() -> CronStore | None:
	"""Return the cron store (for tool handlers to access)."""
	return _cron_store


def set_history_ref(history: list) -> None:
	"""Update the history reference (called when history is created/reset)."""
	global _history_ref
	_history_ref = history
