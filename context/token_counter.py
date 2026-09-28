class TokenCounter:
    """Token counting: prefer API-reported usage, fall back to character estimation."""

    def __init__(self):
        self._last_usage = None

    def update_from_response(self, response):
        """Capture real token usage from an API response."""
        if response.usage:
            self._last_usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

    @property
    def last_prompt_tokens(self) -> int:
        """True input tokens from the most recent API call."""
        return self._last_usage["prompt_tokens"] if self._last_usage else 0

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough estimate: ~3 chars/token for mixed Chinese/English/code."""
        if not text:
            return 0
        return max(1, len(text) // 3)

    def estimate_messages_tokens(self, messages: list) -> int:
        """Estimate total tokens across all messages (content + tool_calls)."""
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            total += self.estimate_tokens(content)
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    total += self.estimate_tokens(tc.get("function", {}).get("name", ""))
                    total += self.estimate_tokens(
                        tc.get("function", {}).get("arguments", "")
                    )
        return total
