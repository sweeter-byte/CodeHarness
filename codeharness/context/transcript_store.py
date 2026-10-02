import json
import uuid
from pathlib import Path


class TranscriptStore:
    """Archive pruned conversation history to disk (JSONL, one message per line).

    Used by Layer 2 (history structural pruning) to persist messages removed
    from the active context, so they remain recoverable.
    """

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = Path(base_dir) if base_dir is not None else None

    @property
    def base_dir(self) -> Path:
        if self._base_dir is None:
            raise RuntimeError(
                "transcript store is not configured; start CodeHarness first"
            )
        return self._base_dir

    def configure(self, base_dir: Path) -> None:
        """Point this store at an explicit runtime-owned directory."""
        self._base_dir = Path(base_dir)

    def save(self, messages: list) -> str:
        """Persist a list of messages; return a transcript_id."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        transcript_id = f"transcript-{uuid.uuid4().hex[:8]}"
        filepath = self.base_dir / f"{transcript_id}.jsonl"
        with open(filepath, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return transcript_id

    # 预留接口,目前未向LLM暴露
    def read(
        self,
        transcript_id: str,
        start_turn: int | None = None,
        end_turn: int | None = None,
    ) -> list:
        """Read back an archived transcript (Rehydration)."""
        filepath = self.base_dir / f"{transcript_id}.jsonl"
        if not filepath.exists():
            return []
        messages = []
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        if start_turn is not None or end_turn is not None:
            messages = messages[start_turn:end_turn]
        return messages


# Module-level singleton shared by main agent and subagents
TRANSCRIPT_STORE = TranscriptStore()
