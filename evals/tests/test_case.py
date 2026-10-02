import json

import pytest

from evals.runner.case import CaseValidationError, discover_cases, load_case


def _write_case(tmp_path, **overrides):
    case_id = overrides.get("case_id", "sample")
    case_dir = tmp_path / case_id
    fixture = case_dir / "fixture"
    fixture.mkdir(parents=True)
    payload = {
        "case_id": "sample",
        "category": "bugfix",
        "description": "sample case",
        "task": "Fix the code.",
        "timeout_seconds": 12,
        "verification": {"command": ["python", "-m", "pytest", "-q"]},
    }
    payload.update(overrides)
    path = case_dir / "case.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_case_resolves_fixture_and_optional_goal(tmp_path):
    path = _write_case(tmp_path, goal="All tests pass.")

    case = load_case(path)

    assert case.case_id == "sample"
    assert case.category == "bugfix"
    assert case.fixture_dir == path.parent / "fixture"
    assert case.timeout_seconds == 12
    assert case.verification.command == ("python", "-m", "pytest", "-q")
    assert case.goal == "All tests pass."


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"case_id": ""}, "case_id"),
        ({"task": "  "}, "task"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"verification": {"command": []}}, "verification.command"),
    ],
)
def test_load_case_rejects_required_invalid_values(tmp_path, overrides, message):
    path = _write_case(tmp_path, **overrides)

    with pytest.raises(CaseValidationError, match=message):
        load_case(path)


def test_discover_cases_is_sorted_and_supports_single_case(tmp_path):
    suite = tmp_path / "smoke"
    _write_case(suite, case_id="zeta")
    _write_case(suite, case_id="alpha")

    cases = discover_cases(suite)

    assert [case.case_id for case in cases] == ["alpha", "zeta"]
    assert discover_cases(suite, case_id="zeta")[0].case_id == "zeta"
    with pytest.raises(CaseValidationError, match="not found"):
        discover_cases(suite, case_id="missing")
