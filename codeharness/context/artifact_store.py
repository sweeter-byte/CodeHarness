import uuid
from pathlib import Path

from .token_counter import TokenCounter

_PREVIEW_CHARS = 1500


class ArtifactStore:
    """Persist large tool results to disk; support rehydration on demand.

    Externalization and Rehydration are designed as a pair — information moves
    from "Always In Context" to "Load On Demand", never simply discarded.
    """

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = Path(base_dir) if base_dir is not None else None
        self._registry: dict[str, dict] = {}

    @property
    def base_dir(self) -> Path:
        if self._base_dir is None:
            raise RuntimeError(
                "artifact store is not configured; start CodeHarness first"
            )
        return self._base_dir

    def configure(self, base_dir: Path) -> None:
        """Point this store at an explicit runtime-owned directory."""
        self._base_dir = Path(base_dir)
        self._registry.clear()

    def save(self, content: str, prefix: str = "tool") -> str:
        """Persist content to disk; return an artifact_id for reference."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        filepath = self.base_dir / f"{artifact_id}.txt"
        filepath.write_text(content, errors="replace")
        self._registry[artifact_id] = {
            "path": str(filepath),
            "size_chars": len(content),
            "size_tokens": TokenCounter.estimate_tokens(content),
            "preview": content[:_PREVIEW_CHARS],
        }
        return artifact_id

    def read(
        self,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Rehydrate: read back a previously externalized artifact."""
        meta = self._registry.get(artifact_id)
        if not meta:
            # Fall back to direct file access (cross-session recovery)
            filepath = self.base_dir / f"{artifact_id}.txt"
            if not filepath.exists():
                return f"Error: artifact '{artifact_id}' not found"
            content = filepath.read_text(errors="replace")
        else:
            content = Path(meta["path"]).read_text(errors="replace")

        if offset is not None or limit is not None:
            lines = content.splitlines()
            start = (offset or 1) - 1
            end = start + (limit or len(lines))
            content = "\n".join(lines[start:end])
        return content

    def list_artifacts(self) -> dict:
        return dict(self._registry)


# Module-level singleton shared by main agent and subagents
ARTIFACT_STORE = ArtifactStore()
