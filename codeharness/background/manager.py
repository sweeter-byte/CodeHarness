"""Background task management for long-running commands.

Provides BackgroundManager to run shell commands in daemon threads,
collecting results asynchronously so the Agent Loop is never blocked.
"""

import os
import signal
import subprocess
import threading
import time


def _format_bash_result(stdout: str, stderr: str, exit_code: int) -> str:
	"""Format command output; large output is externalised via ArtifactStore."""
	out = (stdout + stderr).strip()
	if not out:
		return "(no output)"
	# Large output: structured view + artifact reference
	if len(out) > 30000:
		from codeharness.context.artifact_store import ARTIFACT_STORE
		aid = ARTIFACT_STORE.save(out, prefix="bg_bash")
		head = out[:2000]
		tail = out[-500:] if len(out) > 2500 else ""
		parts = [
			f"exit_code: {exit_code}",
			f"output_size: {len(out)} chars (truncated)",
			f"preview_head:\n{head}",
		]
		if tail:
			parts.append(f"preview_tail:\n...{tail}")
		parts.append(f"full_output: artifact://{aid}")
		return "\n".join(parts)
	return out[:50000]


class BackgroundManager:
	"""Manage background shell tasks: start, track, collect, cancel.

	Each Agent instance owns its own BackgroundManager so that results
	are never routed across different conversation contexts.
	"""

	def __init__(self, max_concurrent: int = 3):
		self.max_concurrent = max_concurrent
		self.tasks: dict[str, dict] = {}
		self.results: dict[str, str] = {}
		self._ready: list[str] = []
		self._lock = threading.Lock()
		self._counter = 0

	def start(
		self,
		command: str,
		timeout: int = 120,
		cwd: str | None = None,
	) -> tuple[str | None, str | None]:
		"""Launch *command* in a daemon thread.

		Returns (bg_id, None) on success, or (None, error_msg) when the
		concurrency limit has been reached.
		"""
		with self._lock:
			running = sum(
				1 for t in self.tasks.values() if t["status"] == "running"
			)
			if running >= self.max_concurrent:
				return None, (
					f"Error: too many background tasks ({running}/{self.max_concurrent}). "
					"Wait for some to finish before starting new ones."
				)
			self._counter += 1
			bg_id = f"bg_{self._counter:03d}"
			self.tasks[bg_id] = {
				"command": command,
				"status": "running",
				"thread": None,
				"pgid": None,
				"start_time": time.time(),
			}

		effective_cwd = cwd if cwd is not None else os.getcwd()
		t = threading.Thread(
			target=self._run,
			args=(bg_id, command, timeout, effective_cwd),
			daemon=True,
		)
		self.tasks[bg_id]["thread"] = t
		t.start()
		print(f"\033[36m[BG] \u25b6 {bg_id} started: {command}\033[0m")
		return bg_id, None

	# ── internal: runs inside the daemon thread ────────────────

	def _run(self, bg_id: str, command: str, timeout: int, cwd: str):
		try:
			proc = subprocess.Popen(
				command,
				shell=True,
				cwd=cwd,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				text=True,
				errors="replace",
				preexec_fn=os.setsid,
			)
			pgid = proc.pid  # setsid → process is its own group leader
			self.tasks[bg_id]["pgid"] = pgid

			try:
				stdout, stderr = proc.communicate(timeout=timeout)
				exit_code = proc.returncode
				status = "completed" if exit_code == 0 else "failed"
			except subprocess.TimeoutExpired:
				# Kill the entire process group
				try:
					os.killpg(pgid, signal.SIGKILL)
				except OSError:
					pass
				stdout, stderr = proc.communicate()
				exit_code = -1
				status = "timeout"

		except (FileNotFoundError, OSError) as exc:
			status = "failed"
			stdout = ""
			stderr = str(exc)
			exit_code = -1
		except Exception as exc:
			status = "failed"
			stdout = ""
			stderr = f"Internal error: {exc}"
			exit_code = -1

		result_text = _format_bash_result(stdout, stderr, exit_code)
		if status == "timeout":
			result_text = f"Error: Timeout ({timeout}s)\n{result_text}"

		with self._lock:
			self.tasks[bg_id]["status"] = status
			self.results[bg_id] = result_text
			self._ready.append(bg_id)

	# ── result collection ──────────────────────────────────────

	def collect(self) -> list[str]:
		"""Return formatted <task_notification> strings for all completed tasks."""
		with self._lock:
			if not self._ready:
				return []
			ids = list(self._ready)
			self._ready.clear()

		notifications = []
		for bg_id in ids:
			task = self.tasks[bg_id]
			result = self.results.get(bg_id, "(no result)")
			elapsed = time.time() - task["start_time"]
			print(f"\033[36m[BG] \u2713 {bg_id} {task['status']} ({elapsed:.1f}s)\033[0m")
			notifications.append(
				f"<task_notification>\n"
				f"Background task {bg_id} {task['status']}.\n"
				f"Command: {task['command']}\n"
				f"Elapsed: {elapsed:.1f}s\n"
				f"---\n"
				f"{result}\n"
				f"---\n"
				f"</task_notification>"
			)
		return notifications

	# ── cancellation ───────────────────────────────────────────

	def cancel(self, bg_id: str) -> bool:
		"""Kill a running background task's process group."""
		with self._lock:
			task = self.tasks.get(bg_id)
			if not task or task["status"] != "running":
				return False
			pgid = task.get("pgid")

		if pgid is not None:
			try:
				os.killpg(pgid, signal.SIGKILL)
			except OSError:
				pass

		with self._lock:
			task["status"] = "cancelled"
		return True

	def cancel_all(self):
		"""Cancel every running task — called when the Agent Loop exits."""
		with self._lock:
			running = [
				(bg_id, t.get("pgid"))
				for bg_id, t in self.tasks.items()
				if t["status"] == "running"
			]

		for bg_id, pgid in running:
			if pgid is not None:
				try:
					os.killpg(pgid, signal.SIGKILL)
				except OSError:
					pass
			with self._lock:
				self.tasks[bg_id]["status"] = "cancelled"

	# ── observability ──────────────────────────────────────────

	def status(self) -> list[dict]:
		"""Return a snapshot of all tasks for diagnostics."""
		with self._lock:
			return [
				{
					"bg_id": bg_id,
					"command": t["command"],
					"status": t["status"],
					"elapsed": time.time() - t["start_time"],
				}
				for bg_id, t in self.tasks.items()
			]


# No process-global default: every Agent creates its own BackgroundManager
# so background results are never routed across runtimes or conversations.
