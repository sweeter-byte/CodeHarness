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
	raw = f"{step_kind}|{label}|{item_id}|{resolved_prompt}|{schema_json}"
	return hashlib.sha256(raw.encode()).hexdigest()[:16]


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

	# ── Run Lock (file-based, cross-process) ──

	def acquire_run_lock(self, run_id: str, timeout: int = 3600) -> bool:
		"""Exclusive-create a lock file. Returns True if acquired."""
		run_dir = self._ensure_run_dir(run_id)
		lock_path = run_dir / ".lock"
		try:
			# O_CREAT | O_EXCL: fails if file already exists
			fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
			os.write(fd, json.dumps({
				"pid": os.getpid(),
				"acquired_at": time.time(),
				"timeout": timeout,
			}).encode())
			os.close(fd)
			return True
		except FileExistsError:
			# Check if the lock is stale (exceeded timeout)
			try:
				data = json.loads(lock_path.read_text(encoding="utf-8"))
				acquired_at = data.get("acquired_at", 0)
				lock_timeout = data.get("timeout", timeout)
				if time.time() - acquired_at > lock_timeout:
					# Stale lock — remove and retry once
					lock_path.unlink(missing_ok=True)
					return self.acquire_run_lock(run_id, timeout)
			except (json.JSONDecodeError, OSError):
				pass
			return False

	def release_run_lock(self, run_id: str) -> None:
		lock_path = self._run_dir(run_id) / ".lock"
		lock_path.unlink(missing_ok=True)
