"""Workflow run state persistence — Snapshot (run-level) + Journal (step-level).

Storage layout:
    .codeharness/workflows/{run_id}/
    ├── snapshot.json       # Run-level state (atomic write)
    ├── journal.jsonl       # Step-level results (append-only)
    └── output.json         # Final output (atomic write)

Design follows the same patterns as TaskStore (atomic write via temp + os.replace)
and TranscriptStore (JSONL append).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ── Run Status ────────────────────────────────────────────────


class RunStatus(str, Enum):
	CREATED = "created"
	RUNNING = "running"
	COMPLETED = "completed"
	FAILED = "failed"
	CANCELLED = "cancelled"


# ── Snapshot ──────────────────────────────────────────────────


@dataclass
class RunSnapshot:
	"""Run-level state for one Workflow execution."""

	run_id: str
	workflow_name: str
	inputs: dict[str, Any]
	status: str = RunStatus.CREATED.value
	current_phase: str | None = None
	completed_steps: list[str] = field(default_factory=list)
	agent_calls_used: int = 0
	tokens_used: int = 0
	started_at: float = field(default_factory=time.time)
	updated_at: float = field(default_factory=time.time)
	error: str | None = None
	depth: int = 0                         # nesting depth (0 = top-level)
	parent_run_id: str | None = None       # if nested


# ── Journal Entry ─────────────────────────────────────────────


@dataclass
class JournalEntry:
	"""One completed step's result, used for Resume cache hits."""

	stable_key: str
	label: str
	phase: str
	step_kind: str
	result: Any                            # structured output or text
	tokens_used: int = 0
	completed_at: float = field(default_factory=time.time)
	resumed: bool = False                  # True if loaded from cache


# ── Stable Key ────────────────────────────────────────────────


def canonical_json(value: Any) -> str:
	"""Serialize a JSON-compatible value deterministically for step identity.

	Non-JSON-compatible values intentionally raise TypeError or ValueError rather than
	falling back to an unstable process-specific representation.
	"""
	def _validate_object_keys(item: Any) -> None:
		if isinstance(item, dict):
			for key, child in item.items():
				if not isinstance(key, str):
					raise TypeError("JSON object keys must be strings")
				_validate_object_keys(child)
		elif isinstance(item, (list, tuple)):
			for child in item:
				_validate_object_keys(child)

	_validate_object_keys(value)
	return json.dumps(
		value,
		sort_keys=True,
		separators=(",", ":"),
		ensure_ascii=False,
		allow_nan=False,
	)


def compute_stable_key(
	step_kind: str,
	label: str,
	resolved_prompt: str,
	schema_json: str = "",
	item_id: str = "",
) -> str:
	"""Generate a deterministic key for journal cache lookup.

	The same semantic call (same kind + label + prompt + schema + item)
	produces the same key across runs, enabling Resume to skip completed work.
	"""
	raw = canonical_json([
		step_kind, label, item_id, resolved_prompt, schema_json,
	])
	return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_legacy_stable_key(
	step_kind: str,
	label: str,
	resolved_prompt: str,
	schema_json: str = "",
	item_id: str = "",
) -> str:
	"""Reproduce the pre-framing key for persisted Agent journal lookup."""
	raw = f"{step_kind}|{label}|{item_id}|{resolved_prompt}|{schema_json}"
	return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Run Lock ─────────────────────────────────────────────────


class WorkflowRunLock:
	"""Ownership token for one process-held Workflow run lock.

	The open file description is the ownership identity. The on-disk file is
	only a stable carrier for ``flock`` and is deliberately never unlinked.
	"""

	def __init__(self, run_id: str, lock_file: Any):
		self.run_id = run_id
		self._lock_file = lock_file
		self._released = False
		self._release_lock = threading.Lock()

	@property
	def released(self) -> bool:
		with self._release_lock:
			return self._released

	def release(self) -> None:
		"""Release this token's fd exactly once."""
		with self._release_lock:
			if self._released:
				return
			try:
				fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
			finally:
				try:
					self._lock_file.close()
				finally:
					self._released = True


# ── State Store ───────────────────────────────────────────────


class WorkflowStateStore:
	"""File-backed persistence for Workflow run state.

	Thread-safe: a per-run re-entrant lock protects snapshot writes and
	journal appends; snapshots additionally use atomic file replacement.
	"""

	def __init__(self, base_dir: Path):
		self.base_dir = Path(base_dir)
		self.base_dir.mkdir(parents=True, exist_ok=True)
		self._locks: dict[str, threading.RLock] = {}
		self._meta_lock = threading.Lock()

	# ── Run ID generation ──

	@staticmethod
	def generate_run_id() -> str:
		return f"run_{secrets.token_hex(4)}"

	# ── Directory helpers ──

	def _run_dir(self, run_id: str) -> Path:
		return self.base_dir / run_id

	def _ensure_run_dir(self, run_id: str) -> Path:
		d = self._run_dir(run_id)
		d.mkdir(parents=True, exist_ok=True)
		return d

	def _get_lock(self, run_id: str) -> threading.RLock:
		with self._meta_lock:
			if run_id not in self._locks:
				self._locks[run_id] = threading.RLock()
			return self._locks[run_id]

	# ── Snapshot CRUD ──

	def save_snapshot(self, snapshot: RunSnapshot) -> None:
		"""Serialize same-run writes, then atomically replace the snapshot."""
		lock = self._get_lock(snapshot.run_id)
		with lock:
			run_dir = self._ensure_run_dir(snapshot.run_id)
			path = run_dir / "snapshot.json"
			snapshot.updated_at = time.time()
			data = asdict(snapshot)
			tmp = path.with_suffix(".json.tmp")
			tmp.write_text(
				json.dumps(data, indent=2, ensure_ascii=False),
				encoding="utf-8",
			)
			os.replace(tmp, path)

	def load_snapshot(self, run_id: str) -> RunSnapshot | None:
		path = self._run_dir(run_id) / "snapshot.json"
		if not path.exists():
			return None
		data = json.loads(path.read_text(encoding="utf-8"))
		return RunSnapshot(**data)

	def list_runs(self) -> list[RunSnapshot]:
		"""List all runs from the index file."""
		index_path = self.base_dir / "index.json"
		if not index_path.exists():
			return []
		try:
			data = json.loads(index_path.read_text(encoding="utf-8"))
			return [
				RunSnapshot(**entry) for entry in data.get("runs", [])
			]
		except (json.JSONDecodeError, TypeError):
			return []

	def update_index(self, snapshot: RunSnapshot) -> None:
		"""Update the lightweight index with current snapshot state."""
		index_path = self.base_dir / "index.json"
		with self._meta_lock:
			entries: list[dict] = []
			if index_path.exists():
				try:
					data = json.loads(index_path.read_text(encoding="utf-8"))
					entries = data.get("runs", [])
				except (json.JSONDecodeError, TypeError):
					entries = []

			# Update or append
			found = False
			for i, entry in enumerate(entries):
				if entry.get("run_id") == snapshot.run_id:
					entries[i] = asdict(snapshot)
					found = True
					break
			if not found:
				entries.append(asdict(snapshot))

			tmp = index_path.with_suffix(".json.tmp")
			tmp.write_text(
				json.dumps({"runs": entries}, indent=2, ensure_ascii=False),
				encoding="utf-8",
			)
			os.replace(tmp, index_path)

	# ── Journal ──

	def append_journal(self, run_id: str, entry: JournalEntry) -> None:
		"""Append one entry to the journal (JSONL, thread-safe)."""
		run_dir = self._ensure_run_dir(run_id)
		path = run_dir / "journal.jsonl"
		line = json.dumps(asdict(entry), ensure_ascii=False)
		lock = self._get_lock(run_id)
		with lock:
			with open(path, "a", encoding="utf-8") as f:
				f.write(line + "\n")

	def load_journal(self, run_id: str) -> dict[str, JournalEntry]:
		"""Load all journal entries keyed by stable_key."""
		path = self._run_dir(run_id) / "journal.jsonl"
		if not path.exists():
			return {}
		entries: dict[str, JournalEntry] = {}
		with open(path, encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					data = json.loads(line)
					entry = JournalEntry(**data)
					entries[entry.stable_key] = entry
				except (json.JSONDecodeError, TypeError):
					continue
		return entries

	# ── Output ──

	def save_output(self, run_id: str, output: Any) -> None:
		"""Atomic write of the final workflow output."""
		run_dir = self._ensure_run_dir(run_id)
		path = run_dir / "output.json"
		tmp = path.with_suffix(".json.tmp")
		tmp.write_text(
			json.dumps(output, indent=2, ensure_ascii=False, default=str),
			encoding="utf-8",
		)
		os.replace(tmp, path)

	def load_output(self, run_id: str) -> Any | None:
		path = self._run_dir(run_id) / "output.json"
		if not path.exists():
			return None
		try:
			return json.loads(path.read_text(encoding="utf-8"))
		except (json.JSONDecodeError, TypeError):
			return None

	# ── Run Lock (POSIX flock, cross-process) ──

	def acquire_run_lock(self, run_id: str) -> WorkflowRunLock | None:
		"""Return an ownership token, or ``None`` while another owner holds it."""
		run_dir = self._ensure_run_dir(run_id)
		lock_path = run_dir / ".lock"
		lock_file = open(lock_path, "a+b")  # noqa: SIM115 -- token owns fd
		try:
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			lock_file.close()
			return None
		run_lock = WorkflowRunLock(run_id, lock_file)

		# Diagnostic metadata is advisory only. Mutual exclusion is provided by
		# the held fd, never by this content or by deleting the carrier file.
		try:
			lock_file.seek(0)
			lock_file.truncate()
			lock_file.write(json.dumps({
				"pid": os.getpid(),
				"acquired_at": time.time(),
			}).encode())
			lock_file.flush()
		except Exception:
			run_lock.release()
			raise
		return run_lock
