from dataclasses import dataclass


@dataclass
class ContextBudget:
    """Token Budget derived from the model's real context window.

    All thresholds come from one unified budget — not independent magic constants.
    Hysteresis: Trigger (soft_limit) > Target (compact_target) to avoid oscillation.
    """
    model_window: int
    reserved_output: int
    safety_margin: int
    fixed_context: int
    soft_limit: int
    compact_target: int
    hard_limit: int

    @property
    def usable_context(self) -> int:
        return max(
            0,
            self.model_window - self.reserved_output
            - self.safety_margin - self.fixed_context,
        )

    @classmethod
    def from_model_config(
        cls,
        model_window: int,
        max_output_tokens: int,
        fixed_context_tokens: int = 0,
        safety_margin_ratio: float = 0.05,
        soft_ratio: float = 0.80,
        compact_ratio: float = 0.60,
        hard_ratio: float = 0.90,
    ) -> "ContextBudget":
        safety_margin = int(model_window * safety_margin_ratio)
        usable = max(
            0,
            model_window - max_output_tokens - safety_margin - fixed_context_tokens,
        )
        return cls(
            model_window=model_window,
            reserved_output=max_output_tokens,
            safety_margin=safety_margin,
            fixed_context=fixed_context_tokens,
            soft_limit=int(usable * soft_ratio),
            compact_target=int(usable * compact_ratio),
            hard_limit=int(usable * hard_ratio),
        )
