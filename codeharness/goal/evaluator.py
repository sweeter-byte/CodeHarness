"""GoalEvaluator — independent LLM-based completion judge.

The Evaluator reads the conversation transcript and determines whether
the user-defined Completion Condition has been satisfied, based solely
on observable evidence (tool outputs, exit codes, test results).

It never executes tools, modifies files, or continues the task itself.
"""

from __future__ import annotations

import json
from typing import Any

from codeharness.goal.state import EvalResult


# ── Evaluator System Prompt ──────────────────────────────────

EVALUATOR_SYSTEM = (
	"You are a Goal Evaluator. Your ONLY job is to determine whether a "
	"completion condition has been satisfied based on the evidence in the "
	"conversation transcript.\n\n"
	"Rules:\n"
	"- Judge ONLY by observable evidence (tool outputs, exit codes, test results).\n"
	"- Do NOT accept the agent's claims without supporting evidence in the transcript.\n"
	"- If evidence is insufficient or ambiguous, the goal is NOT achieved.\n"
	"- If the goal is fundamentally impossible to achieve, set impossible=true.\n"
	"- Be strict: partial completion is NOT completion.\n\n"
	"Respond ONLY with a JSON object (no markdown, no explanation):\n"
	'{"ok": boolean, "reason": "string", "impossible": boolean}'
)

# Maximum tokens for the evaluator transcript window
_EVALUATOR_TRANSCRIPT_BUDGET = 12000
# Minimum recent messages always kept (even if over budget)
_EVALUATOR_MIN_RECENT = 4


class EvaluatorError(Exception):
	"""Raised when the Evaluator call or parsing fails."""


class GoalEvaluator:
	"""Calls an LLM to judge goal completion from transcript evidence."""

	def __init__(
		self,
		client: Any,
		model: str | None = None,
		max_tokens: int = 1024,
	):
		self.client = client
		self.model = model
		self.max_tokens = max_tokens

	def evaluate(self, condition: str, messages: list[dict]) -> EvalResult:
		"""Run goal evaluation against the transcript.

		Args:
			condition: The user-defined completion condition.
			messages: The full conversation messages list.

		Returns:
			EvalResult with ok/reason/impossible fields.

		Raises:
			EvaluatorError: If the LLM call or JSON parsing fails.
		"""
		evaluator_messages = self._build_messages(condition, messages)
		try:
			response = self.client.chat.completions.create(
				model=self.model,
				messages=evaluator_messages,
				max_tokens=self.max_tokens,
			)
		except Exception as exc:
			raise EvaluatorError(f"LLM call failed: {exc}") from exc

		raw = response.choices[0].message.content or ""
		return self._parse_result(raw)

	# ── Transcript Construction ──────────────────────────────

	def _build_messages(self, condition: str, messages: list[dict]) -> list[dict]:
		"""Build the evaluator's input: system + goal + trimmed transcript."""
		transcript = self._trim_transcript(messages)
		transcript_text = self._format_transcript(transcript)

		user_prompt = (
			f"## Completion Condition\n\n{condition}\n\n"
			f"## Conversation Transcript (evidence)\n\n{transcript_text}\n\n"
			"## Your Task\n\n"
			"Based on the evidence above, determine whether the completion "
			"condition has been satisfied. Respond with JSON only."
		)

		return [
			{"role": "system", "content": EVALUATOR_SYSTEM},
			{"role": "user", "content": user_prompt},
		]

	def _trim_transcript(self, messages: list[dict]) -> list[dict]:
		"""Trim transcript to fit evaluator budget.

		Strategy: keep the most recent messages that fit within the token
		budget. Always keep at least _EVALUATOR_MIN_RECENT messages.
		If the first user message exists, always include it (task context).
		"""
		if not messages:
			return []

		# Always include the first user message for context
		head: list[dict] = []
		body = list(messages)
		for i, msg in enumerate(messages):
			if msg.get("role") == "user":
				head = [msg]
				body = messages[i + 1:]
				break

		# Collect from tail until budget exceeded
		tail: list[dict] = []
		budget = _EVALUATOR_TRANSCRIPT_BUDGET
		used = 0

		for msg in reversed(body):
			msg_tokens = self._estimate_msg_tokens(msg)
			if used + msg_tokens > budget and len(tail) >= _EVALUATOR_MIN_RECENT:
				break
			tail.append(msg)
			used += msg_tokens

		tail.reverse()
		return head + tail

	def _format_transcript(self, messages: list[dict]) -> str:
		"""Format messages into readable transcript text."""
		parts: list[str] = []
		for msg in messages:
			role = msg.get("role", "unknown")
			content = msg.get("content") or ""

			# Include tool_calls summary for assistant messages
			if msg.get("tool_calls"):
				tc_summary = []
				for tc in msg["tool_calls"]:
					fn = tc.get("function", {})
					name = fn.get("name", "?")
					args_str = fn.get("arguments", "{}")
					# Truncate long arguments
					if len(args_str) > 500:
						args_str = args_str[:500] + "..."
					tc_summary.append(f"  → {name}({args_str})")
				content += "\n" + "\n".join(tc_summary)

			# Truncate very long tool results but keep head+tail
			if role == "tool" and len(content) > 3000:
				content = (
					content[:1500]
					+ "\n... [truncated] ...\n"
					+ content[-500:]
				)

			parts.append(f"[{role}]\n{content}")

		return "\n\n".join(parts)

	@staticmethod
	def _estimate_msg_tokens(msg: dict) -> int:
		"""Rough token estimate for a single message."""
		text = msg.get("content") or ""
		if msg.get("tool_calls"):
			for tc in msg["tool_calls"]:
				fn = tc.get("function", {})
				text += fn.get("name", "") + fn.get("arguments", "")
		return max(1, len(text) // 3)

	# ── Result Parsing ───────────────────────────────────────

	def _parse_result(self, raw: str) -> EvalResult:
		"""Parse the evaluator's JSON response into EvalResult."""
		# Try direct parse
		parsed = self._try_parse_json(raw)
		if parsed is None:
			raise EvaluatorError(
				f"Failed to parse evaluator response as JSON: {raw[:200]}"
			)

		# Validate required fields
		if "ok" not in parsed:
			raise EvaluatorError(
				f"Evaluator response missing 'ok' field: {parsed}"
			)

		return EvalResult(
			ok=bool(parsed["ok"]),
			reason=str(parsed.get("reason", "")),
			impossible=bool(parsed.get("impossible", False)),
		)

	@staticmethod
	def _try_parse_json(text: str) -> dict | None:
		"""Attempt JSON extraction from text (handles markdown fences)."""
		text = text.strip()
		# Strip markdown fences
		if text.startswith("```"):
			lines = text.split("\n")
			lines = [ln for ln in lines if not ln.strip().startswith("```")]
			text = "\n".join(lines)
		try:
			result = json.loads(text)
			if isinstance(result, dict):
				return result
		except (json.JSONDecodeError, TypeError):
			pass
		# Try to find JSON object in text
		start = text.find("{")
		end = text.rfind("}")
		if start != -1 and end > start:
			try:
				result = json.loads(text[start:end + 1])
				if isinstance(result, dict):
					return result
			except (json.JSONDecodeError, TypeError):
				pass
		return None
