from evals.metrics.aggregate import aggregate_results


def test_aggregate_results_computes_overall_and_category_metrics():
    results = [
        {
            "case_id": "a",
            "category": "bugfix",
            "success": True,
            "total_tokens": 100,
            "tool_calls": 4,
            "wall_time_seconds": 2.0,
        },
        {
            "case_id": "b",
            "category": "bugfix",
            "success": False,
            "total_tokens": 300,
            "tool_calls": 8,
            "wall_time_seconds": 4.0,
        },
        {
            "case_id": "c",
            "category": "implementation",
            "success": True,
            "total_tokens": 200,
            "tool_calls": 3,
            "wall_time_seconds": 3.0,
        },
    ]

    summary = aggregate_results(results)

    assert summary["total_cases"] == 3
    assert summary["passed_cases"] == 2
    assert summary["failed_cases"] == 1
    assert summary["success_rate"] == 2 / 3
    assert summary["total_tokens"] == 600
    assert summary["average_tokens"] == 200
    assert summary["total_tool_calls"] == 15
    assert summary["average_tool_calls"] == 5
    assert summary["average_wall_time_seconds"] == 3
    assert summary["by_category"]["bugfix"]["passed_cases"] == 1
    assert summary["by_category"]["implementation"]["success_rate"] == 1.0


def test_aggregate_results_handles_empty_input():
    summary = aggregate_results([])

    assert summary["total_cases"] == 0
    assert summary["success_rate"] == 0.0
    assert summary["average_tokens"] == 0.0
    assert summary["average_tool_calls"] == 0.0
    assert summary["average_wall_time_seconds"] == 0.0
    assert summary["by_category"] == {}
