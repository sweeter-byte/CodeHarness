"""ContextManager — the four-layer compression pipeline entry point.

Pipeline order (deterministic/recoverable first, lossy semantic last):
  Layer 1  Externalize large tool results → Artifact Store
  Layer 2  Archive old history (Head + Tail) → Transcript Store
  Layer 3  Evict consumed tool results → Artifact Store
  Layer 4  Semantic compaction → Structured Checkpoint
"""

from .budget import ContextBudget
from .token_counter import TokenCounter
from .artifact_store import ARTIFACT_STORE, ArtifactStore
from .transcript_store import TRANSCRIPT_STORE, TranscriptStore
from .compactor import Compactor

# Layer 1: externalize a single tool result above this token count
_EXTERNALIZE_TOKEN_THRESHOLD = 8000
# Layer 1: preview chars kept in context after externalization
_PREVIEW_CHARS = 1500
# Layer 3: evict a consumed tool result above this token count
_EVICT_TOKEN_THRESHOLD = 2000
# Layer 2/4: messages always kept at the head
_HEAD_KEEP = 2
# Layer 2: tail budget as fraction of soft_limit
_TAIL_BUDGET_RATIO = 0.5


class ContextManager:
    """Orchestrates the full context management pipeline.

    Public API:
      prepare(messages)          — called before every LLM call
      compact_history(messages)  — manual /compact
      reactive_compact(messages) — API overflow recovery (no LLM call)
      get_observability_report() — /context diagnostic
    """

    def __init__(
        self,
        budget: ContextBudget,
        token_counter: TokenCounter,
        artifact_store: ArtifactStore = None,
        transcript_store: TranscriptStore = None,
    ):
        self.budget = budget
        self.tc = token_counter
        self.artifact_store = artifact_store or ARTIFACT_STORE
        self.transcript_store = transcript_store or TRANSCRIPT_STORE
        self.compactor = Compactor()

        self._processed_tool_ids: set[str] = set()
        self._consumed_upto: int = 0
        self.active_request: str = ""

        # Set by Agent.__init__ for observability & compaction
        self.system_prompt: str = ""
        self.tool_schemas: list = []

    def configure_llm(self, client, model: str):
        """Wire the LLM client so Layer 4 can call the summarizer."""
        self.compactor = Compactor(client=client, model=model)

    def set_active_request(self, request: str):
        self.active_request = request

    # ── Public API ──────────────────────────────────────────────

    def prepare(self, messages: list) -> list:
        """Run the progressive pipeline before each LLM call.

        Layer 1 always runs (externalize fresh large results).
        Layers 2-4 run only when the soft/hard limit is exceeded.
        """
        messages = self._externalize_large_results(messages)

        # Save the PREVIOUS consumed boundary (what the model has already seen)
        # before updating it for the next call.
        prev_consumed = self._consumed_upto
        # After this prepare() + LLM call, everything currently in messages
        # will have been seen by the model.
        self._consumed_upto = len(messages)

        current = self.tc.estimate_messages_tokens(messages)

        if current > self.budget.soft_limit:
            messages = self._archive_old_history(messages)
            current = self.tc.estimate_messages_tokens(messages)

        if current > self.budget.soft_limit:
            messages = self._evict_consumed_tool_results(messages, prev_consumed)
            current = self.tc.estimate_messages_tokens(messages)

        if current > self.budget.hard_limit:
            messages = self._semantic_compact(messages)

        return messages

    def compact_history(self, messages: list) -> list:
        """Manual /compact: run Layers 2 → 3 → 4 unconditionally."""
        messages = self._archive_old_history(messages)
        messages = self._evict_consumed_tool_results(messages)
        messages = self._semantic_compact(messages)
        return messages

    def reactive_compact(self, messages: list) -> list:
        """Emergency recovery after API context-overflow.

        No LLM call (the API is already failing).  Keeps Head + Tail,
        archives the middle to a transcript.
        """
        if len(messages) <= _HEAD_KEEP + 2:
            return messages

        head = messages[:_HEAD_KEEP]
        tail_budget = int(self.budget.compact_target * _TAIL_BUDGET_RATIO)
        tail_start = len(messages)
        tail_tokens = 0
        while tail_start > _HEAD_KEEP:
            t = self.tc.estimate_tokens(str(messages[tail_start - 1].get("content", "")))
            if tail_tokens + t > tail_budget:
                break
            tail_tokens += t
            tail_start -= 1

        tail_start = self._align_to_tool_round(messages, tail_start)
        middle = messages[_HEAD_KEEP:tail_start]
        if middle:
            tid = self.transcript_store.save(middle)
            marker = {
                "role": "user",
                "content": (
                    f"[Reactive compact: {len(middle)} messages archived, "
                    f"transcript://{tid}]"
                ),
            }
            messages = head + [marker] + messages[tail_start:]

        self._ensure_active_request(messages)
        return messages

    def get_observability_report(self, messages: list) -> str:
        """/context diagnostic output."""
        sys_tokens = self.tc.estimate_tokens(self.system_prompt)
        schema_tokens = self.tc.estimate_tokens(str(self.tool_schemas))
        fixed = sys_tokens + schema_tokens

        user_t = asst_t = tool_res_t = tool_call_t = 0
        artifact_refs = 0
        for msg in messages:
            role = msg.get("role", "")
            content = str(msg.get("content", ""))
            t = self.tc.estimate_tokens(content)
            if role == "user":
                user_t += t
            elif role == "assistant":
                asst_t += t
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    tool_call_t += self.tc.estimate_tokens(
                        fn.get("name", "") + fn.get("arguments", "")
                    )
            elif role == "tool":
                tool_res_t += t
                if "artifact://" in content:
                    artifact_refs += 1

        dynamic = user_t + asst_t + tool_res_t + tool_call_t
        total = fixed + dynamic
        usable = self.budget.usable_context
        pct = total * 100 // usable if usable else 0

        lines = [
            f"Model Context Window: {self.budget.model_window:>12,} tokens",
            f"Usable Budget:        {usable:>12,} tokens",
            "",
            f"Fixed Context                     {fixed:>8,} tokens",
            f"  ├── System Prompt               {sys_tokens:>8,}",
            f"  └── Tool Schemas                {schema_tokens:>8,}",
            "",
            f"Dynamic Context                   {dynamic:>8,} tokens",
            f"  ├── User Messages               {user_t:>8,}",
            f"  ├── Assistant Messages          {asst_t:>8,}",
            f"  ├── Tool Calls (fn+args)        {tool_call_t:>8,}",
            f"  └── Tool Results                {tool_res_t:>8,}",
            f"      └── Externalized (artifact)  {artifact_refs} refs",
            "",
            f"Messages Count: {len(messages)}",
            f"Artifacts on Disk: {len(self.artifact_store.list_artifacts())}",
            "",
            "Budget Status",
            f"  ├── Soft Limit      {self.budget.soft_limit:>10,}  (auto compact trigger)",
            f"  ├── Compact Target  {self.budget.compact_target:>10,}  (compress down to)",
            f"  ├── Hard Limit      {self.budget.hard_limit:>10,}  (emergency zone)",
            f"  └── Current Usage   {total:>10,}  ({pct}%)",
        ]
        if pct >= 90:
            lines.append("\n⚠ WARNING: Context in emergency zone!")
        elif pct >= 80:
            lines.append("\n⚠ Context approaching soft limit, auto compact imminent.")
        return "\n".join(lines)

    # ── Layer 1: Externalize large tool results ─────────────────

    def _externalize_large_results(self, messages: list) -> list:
        """Replace oversized fresh tool results with artifact ref + preview."""
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tcid = msg.get("tool_call_id", "")
            if tcid in self._processed_tool_ids:
                continue
            self._processed_tool_ids.add(tcid)

            content = msg.get("content", "")
            if not content:
                continue
            tokens = self.tc.estimate_tokens(content)
            if tokens <= _EXTERNALIZE_TOKEN_THRESHOLD:
                continue

            aid = self.artifact_store.save(content, prefix="tool")
            preview = content[:_PREVIEW_CHARS]
            msg["content"] = (
                f"<persisted-output>\n"
                f"Full output: artifact://{aid}\n"
                f"Preview:\n{preview}\n"
                f"...</persisted-output>"
            )
        return messages

    # ── Layer 2: History structural pruning ─────────────────────

    def _archive_old_history(self, messages: list) -> list:
        """Keep Head + Tail, archive the middle to a transcript."""
        if len(messages) <= _HEAD_KEEP + 4:
            return messages

        head_count = _HEAD_KEEP
        # Skip over an existing archive marker in the head region
        while head_count < len(messages):
            c = str(messages[head_count].get("content", ""))
            if c.startswith("[") and "archived" in c:
                head_count += 1
            else:
                break

        tail_budget = int(self.budget.soft_limit * _TAIL_BUDGET_RATIO)
        tail_start = len(messages)
        tail_tokens = 0
        while tail_start > head_count:
            t = self.tc.estimate_tokens(str(messages[tail_start - 1].get("content", "")))
            if tail_tokens + t > tail_budget:
                break
            tail_tokens += t
            tail_start -= 1

        tail_start = self._align_to_tool_round(messages, tail_start)

        middle = messages[head_count:tail_start]
        if not middle:
            return messages

        tid = self.transcript_store.save(middle)
        marker = {
            "role": "user",
            "content": (
                f"[{len(middle)} messages archived, transcript://{tid}]"
            ),
        }
        messages[:] = messages[:head_count] + [marker] + messages[tail_start:]
        self._ensure_active_request(messages)
        return messages

    # ── Layer 3: Tool result eviction ───────────────────────────

    def _evict_consumed_tool_results(self, messages: list, consumed_upto: int) -> list:
        """Evict old consumed tool results; keep unseen (latest) ones intact.

        *consumed_upto* is the message count from the PREVIOUS prepare() call —
        messages at index < consumed_upto have already been seen by the model.
        """
        for i, msg in enumerate(messages):
            if msg.get("role") != "tool":
                continue
            if i >= consumed_upto:
                continue  # unseen — model hasn't processed yet
            content = msg.get("content", "")
            if "artifact://" in content:
                continue  # already externalized
            tokens = self.tc.estimate_tokens(content)
            if tokens <= _EVICT_TOKEN_THRESHOLD:
                continue
            aid = self.artifact_store.save(content, prefix="evicted")
            msg["content"] = f"[Earlier tool result saved: artifact://{aid}]"
        return messages

    # ── Layer 4: Semantic compaction ────────────────────────────

    def _semantic_compact(self, messages: list) -> list:
        """LLM-based compaction; falls back to emergency archive on failure."""
        if len(messages) <= _HEAD_KEEP + 4:
            return messages

        head = messages[:_HEAD_KEEP]
        tail_budget = int(self.budget.compact_target * _TAIL_BUDGET_RATIO)
        tail_start = len(messages)
        tail_tokens = 0
        while tail_start > _HEAD_KEEP:
            t = self.tc.estimate_tokens(str(messages[tail_start - 1].get("content", "")))
            if tail_tokens + t > tail_budget:
                break
            tail_tokens += t
            tail_start -= 1

        tail_start = self._align_to_tool_round(messages, tail_start)
        middle = messages[_HEAD_KEEP:tail_start]
        tail = messages[tail_start:]

        if not middle:
            return messages

        tid = self.transcript_store.save(middle)
        checkpoint = self.compactor.compact(middle, self.active_request, f"transcript://{tid}")

        if not checkpoint.current_goal and not checkpoint.active_request:
            # LLM compaction failed → emergency fallback (no LLM)
            marker = {
                "role": "user",
                "content": f"[{len(middle)} messages archived, transcript://{tid}]",
            }
            messages[:] = head + [marker] + tail
        else:
            import dataclasses, json
            summary_msg = {
                "role": "user",
                "content": (
                    "<context-compacted>\n"
                    + json.dumps(dataclasses.asdict(checkpoint), ensure_ascii=False, indent=2)
                    + "\n</context-compacted>"
                ),
            }
            messages[:] = head + [summary_msg] + tail

        self._ensure_active_request(messages)
        return messages

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _identify_tool_rounds(messages: list) -> list[tuple[int, int]]:
        """Return (start, end) index pairs for each complete Tool Round.

        A round = one assistant message with tool_calls + all its tool results.
        Tool Call and Tool Result must be kept or archived together.
        """
        rounds: list[tuple[int, int]] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected = {tc.get("id") for tc in msg["tool_calls"]}
                collected: set = set()
                j = i + 1
                while j < len(messages) and collected != expected:
                    if messages[j].get("role") == "tool":
                        collected.add(messages[j].get("tool_call_id"))
                    j += 1
                rounds.append((i, j - 1))
                i = j
            else:
                i += 1
        return rounds

    def _align_to_tool_round(self, messages: list, proposed: int) -> int:
        """Shift *proposed* back to a Tool Round boundary so no pair is split."""
        for rs, re in self._identify_tool_rounds(messages):
            if rs < proposed <= re:
                return rs
        return proposed

    def _ensure_active_request(self, messages: list):
        """Guarantee the active user request is visible (Protected Context).

        If it was archived away, re-inject it right after the head.
        """
        if not self.active_request:
            return
        req = self.active_request
        for msg in messages:
            if msg.get("role") == "user":
                c = str(msg.get("content", ""))
                if req in c or c.strip() == req.strip():
                    return
        inject = {
            "role": "user",
            "content": f"[Active request — re-injected after compaction]\n{req}",
        }
        pos = min(_HEAD_KEEP, len(messages))
        messages.insert(pos, inject)
