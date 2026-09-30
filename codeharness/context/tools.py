"""Context tool adapter — artifact rehydration for the model (read_artifact).

The ArtifactStore implementation stays in artifact_store.py; this module
only owns the tool schema + handler pair.
"""

# ── Tool Schema ───────────────────────────────────────────────

CONTEXT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_artifact",
            "description": (
                "Read a previously saved artifact by its ID. "
                "Use when you see 'artifact://xxx' references in earlier tool results "
                "and need the full content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "The artifact ID from an artifact:// reference.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based start line (optional, for large artifacts).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to return (optional).",
                    },
                },
                "required": ["artifact_id"],
            },
        },
    },
]

# ── Tool Implementation ───────────────────────────────────────


def run_read_artifact(artifact_id: str, offset: int = None, limit: int = None) -> str:
    """Rehydration: read back a previously externalized artifact."""
    from codeharness.context.artifact_store import ARTIFACT_STORE
    return ARTIFACT_STORE.read(artifact_id, offset, limit)


CONTEXT_HANDLERS = {
    "read_artifact": run_read_artifact,
}
