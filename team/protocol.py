"""Structured control protocol — shutdown handshake and plan approval.

Free-form text is fine for ordinary collaboration, but control operations
(shutdown, plan approval) must not rely on guessing message intent. Both use
ProtocolState with a request_id so responses match requests exactly, `type`
prevents mismatched replies from mutating state, and `status` blocks the
same reply from taking effect twice.

Two handshakes, in opposite directions:
  shutdown:    leader → teammate (leader requests, teammate responds)
  plan approval: teammate → leader (teammate requests, leader responds)

In-memory only: unfinished transactions die with the team on crash.
"""

import secrets
import threading
from dataclasses import dataclass

# ── Plan gate states ──────────────────────────────────────────

GATE_NOT_REQUIRED = "not_required"   # no plan needed, work directly
GATE_REQUIRED = "required"           # plan required before any modification
GATE_PENDING = "pending"             # plan submitted, waiting for approval
GATE_APPROVED = "approved"           # approved, may modify
GATE_REJECTED = "rejected"           # rejected, revise and resubmit

# Gates under which write tools (bash/write_file/edit_file) are blocked.
BLOCKING_GATES = {GATE_REQUIRED, GATE_PENDING, GATE_REJECTED}

# ── Protocol state ────────────────────────────────────────────


@dataclass
class ProtocolState:
	request_id: str
	type: str        # shutdown_request | plan_approval_request
	sender: str      # who created the request
	target: str      # who is expected to respond
	status: str      # pending | approved | rejected
	payload: str     # plan text (plan approval) or reason (shutdown)
	task_id: str | None = None
	work_version: int | None = None


# request_id → state. Guarded by the registry lock (teammate threads submit,
# leader thread approves; wakeup thread matches responses).
_REQUESTS_LOCK = threading.Lock()
PENDING_REQUESTS: dict[str, ProtocolState] = {}


def _new_request_id() -> str:
	return f"req_{secrets.token_hex(4)}"


def create_request(req_type: str, sender: str, target: str, payload: str,
				   task_id: str | None = None,
				   work_version: int | None = None) -> ProtocolState:
	"""Register a new pending request and return its state."""
	req = ProtocolState(
		request_id=_new_request_id(),
		type=req_type,
		sender=sender,
		target=target,
		status="pending",
		payload=payload,
		task_id=task_id,
		work_version=work_version,
	)
	with _REQUESTS_LOCK:
		PENDING_REQUESTS[req.request_id] = req
	return req


def match_response(request_id: str, expected_type: str) -> ProtocolState | None:
	"""Find the original request for a response.

	Returns None when the request is unknown, already resolved (status not
	pending), or of an unexpected type — a stale or duplicated reply must
	never mutate protocol state twice.
	"""
	with _REQUESTS_LOCK:
		req = PENDING_REQUESTS.get(request_id)
		if req is None or req.type != expected_type or req.status != "pending":
			return None
		return req


def resolve(request_id: str, status: str) -> ProtocolState | None:
	"""Mark a pending request as resolved (approved/rejected/shutdown)."""
	with _REQUESTS_LOCK:
		req = PENDING_REQUESTS.get(request_id)
		if req is None or req.status != "pending":
			return None
		req.status = status
		return req


def get_request(request_id: str) -> ProtocolState | None:
	with _REQUESTS_LOCK:
		return PENDING_REQUESTS.get(request_id)
