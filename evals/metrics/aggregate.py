"""Aggregate case result dictionaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(bool(item.get("success")) for item in results)
    total_tokens = sum(int(item.get("total_tokens", 0)) for item in results)
    total_tools = sum(int(item.get("tool_calls", 0)) for item in results)
    total_time = sum(float(item.get("wall_time_seconds", 0)) for item in results)
    return {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": total - passed,
        "success_rate": passed / total if total else 0.0,
        "total_tokens": total_tokens,
        "average_tokens": total_tokens / total if total else 0.0,
        "total_tool_calls": total_tools,
        "average_tool_calls": total_tools / total if total else 0.0,
        "average_wall_time_seconds": total_time / total if total else 0.0,
    }


def aggregate_results(
    results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compute overall and per-category summary metrics."""
    materialized = list(results)
    summary = _summarize(materialized)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in materialized:
        grouped[str(result.get("category", "unknown"))].append(result)
    summary["by_category"] = {
        category: _summarize(items)
        for category, items in sorted(grouped.items())
    }
    return summary
