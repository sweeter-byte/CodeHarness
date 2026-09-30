"""Leader wakeup thread — event-driven delivery of team events.

Watches the leader's mailbox and offers model-facing events to the
CodeHarness Runtime through a non-blocking delivery callback. The leader
never polls for teammate results — it ends its turn after spawn_teammate
and is woken here.

Protocol responses (*_response) update protocol state without waking the
LLM: they are runtime bookkeeping, not model input.
"""

import threading
from collections.abc import Callable

from codeharness.team.bus import BUS, LEADER
from codeharness.team import protocol

WAKEUP_POLL_INTERVAL = 0.5  # seconds; also the wait_for_messages timeout

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_status_handler: Callable[[str], None] | None = None


def _emit_status(message: str) -> None:
	if _status_handler is not None:
		_status_handler(message)


def start(
	delivery_handler: Callable[[str], bool],
	status_handler: Callable[[str], None] | None = None,
) -> None:
	"""Start the wakeup thread (called from CodeHarness.start)."""
	global _thread, _status_handler
	_status_handler = status_handler
	_stop_event.clear()
	_thread = threading.Thread(
		target=_wakeup_loop, args=(delivery_handler,),
		daemon=True, name="team-wakeup",
	)
	_thread.start()
	_emit_status("\033[36m[team] wakeup thread started\033[0m")


def stop() -> None:
	_stop_event.set()


def _wakeup_loop(delivery_handler: Callable[[str], bool]) -> None:
	buffered: list[dict] = []
	while not _stop_event.is_set():
		msgs = BUS.wait_for_messages(LEADER, timeout=WAKEUP_POLL_INTERVAL)
		if msgs:
			buffered.extend(msgs)
			_emit_status(f"\033[36m[team] {len(msgs)} event(s) arrived\033[0m")
		if not buffered:
			continue

		# Split: protocol responses are consumed by the runtime; everything
		# else (result / idle_notification / message / plan_approval_request)
		# is model-facing and must wake the leader.
		deliverable = []
		for m in buffered:
			if m.get("type", "").endswith("_response"):
				_handle_response(m)
			else:
				deliverable.append(m)
		buffered = deliverable if deliverable else []
		if not buffered:
			continue

		try:
			events_text = "\n".join(_format_event(m) for m in buffered)
			content = (
				f"[Team events]\n{events_text}\n"
				"Coordinate the team: check list_task / list_teammates, "
				"approve pending plans (approve_plan), spawn or shut "
				"down teammates as needed, then reply to the user."
			)
			accepted = delivery_handler(content)
		except Exception as exc:  # noqa: BLE001 — wakeup must survive bad turns
			_emit_status(
				f"\033[31m[team] leader turn failed: {exc}. Will retry.\033[0m"
			)
			continue

		if accepted:
			buffered.clear()


def _format_event(m: dict) -> str:
	mtype = m.get("type", "message")
	sender = m.get("from", "?")
	content = str(m.get("content", "")).strip()
	if mtype == "result":
		return f"[result from {sender}]\n{content}"
	if mtype == "idle_notification":
		return f"[{sender} is idle — waiting for more work]"
	if mtype == "plan_approval_request":
		meta = m.get("metadata", {})
		return (f"[plan_approval_request from {sender}] "
				f"request_id={meta.get('request_id')} "
				f"task_id={meta.get('task_id')}\n"
				f"Plan:\n{content}\n"
				"Respond with approve_plan(request_id, approve, feedback).")
	return f"[message from {sender}]\n{content}"


def _handle_response(m: dict) -> None:
	"""Runtime-side handling of protocol responses (no LLM turn needed)."""
	mtype = m.get("type", "")
	metadata = m.get("metadata", {})
	request_id = metadata.get("request_id")
	sender = m.get("from", "?")

	if mtype == "shutdown_response":
		req = protocol.match_response(request_id, "shutdown_request")
		if req is not None:
			protocol.resolve(request_id, "shutdown")
			_emit_status(
				f"\033[36m[team] {sender} shut down cleanly "
				f"({request_id})\033[0m"
			)
		else:
			_emit_status(
				f"\033[33m[team] stale shutdown_response from "
				f"{sender} ignored\033[0m"
			)
