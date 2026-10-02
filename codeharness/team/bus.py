"""MessageBus — file-backed inbox for agent-to-agent communication.

Each agent owns one JSONL mailbox in the configured runtime directory; 'lead' is the
reserved leader mailbox. Lifecycle matches the team's. A Condition makes
message arrival wake waiting teammates without polling at full speed.
"""

import json
import threading
import time
from pathlib import Path


LEADER = "lead"  # reserved mailbox name; teammates must never use it


class MessageBus:
	"""Per-agent JSONL inboxes with condition-variable notification."""

	def __init__(self, mailbox_dir: str | Path | None = None):
		self._mailbox_dir = (
			Path(mailbox_dir) if mailbox_dir is not None else None
		)
		self._cond = threading.Condition()

	@property
	def mailbox_dir(self) -> Path:
		if self._mailbox_dir is None:
			raise RuntimeError(
				"message bus is not configured; start CodeHarness first"
			)
		return self._mailbox_dir

	def configure(self, mailbox_dir: str | Path) -> None:
		"""Point this bus at an explicit runtime-owned mailbox directory."""
		with self._cond:
			self._mailbox_dir = Path(mailbox_dir)

	# ── internals ──

	def _path(self, agent: str) -> Path:
		return self.mailbox_dir / f"{agent}.jsonl"

	def _read_unlocked(self, agent: str) -> list[dict]:
		"""Consume and return all messages in the inbox (read-and-delete)."""
		path = self._path(agent)
		if not path.exists():
			return []
		messages = []
		for line in path.read_text(encoding="utf-8").splitlines():
			line = line.strip()
			if not line:
				continue
			try:
				messages.append(json.loads(line))
			except json.JSONDecodeError:
				continue  # tolerate torn writes; audit the file manually
		path.unlink(missing_ok=True)
		return messages

	# ── public API ──

	def send(self, from_agent: str, to_agent: str, content: str,
			 msg_type: str = "message", metadata: dict | None = None) -> None:
		"""Append a message to the recipient's inbox and wake waiters."""
		msg = {
			"from": from_agent,
			"to": to_agent,
			"content": content,
			"type": msg_type,
			"metadata": metadata or {},
		}
		with self._cond:
			self.mailbox_dir.mkdir(parents=True, exist_ok=True)
			with self._path(to_agent).open("a", encoding="utf-8") as handle:
				handle.write(json.dumps(msg, ensure_ascii=False) + "\n")
			self._cond.notify_all()

	def peek(self, agent: str) -> bool:
		"""True if the inbox has at least one message (non-consuming)."""
		with self._cond:
			path = self._path(agent)
			return path.exists() and path.stat().st_size > 0

	def read_inbox(self, agent: str) -> list[dict]:
		"""Consume all pending messages (single-consumer semantics)."""
		with self._cond:
			return self._read_unlocked(agent)

	def wait_for_messages(self, agent: str,
						  timeout: float | None = None) -> list[dict]:
		"""Block until the inbox is non-empty or timeout elapses.

		Used by idle teammates (short timeout → task-board scan between
		waits) and by the leader wakeup thread (event-driven delivery).
		"""
		deadline = None if timeout is None else time.monotonic() + timeout
		with self._cond:
			while not self.peek(agent):
				if deadline is not None:
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						return []
					self._cond.wait(remaining)
				else:
					self._cond.wait()
			return self._read_unlocked(agent)


# Module-level singleton shared by leader, teammates and wakeup thread.
BUS = MessageBus()
