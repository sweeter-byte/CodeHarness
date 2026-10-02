"""JSON case definitions and discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CaseValidationError(ValueError):
    """Raised when an evaluation case is malformed."""


@dataclass(frozen=True)
class Verification:
    command: tuple[str, ...]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    description: str
    task: str
    fixture_dir: Path
    timeout_seconds: int
    verification: Verification
    goal: str | None = None
    source_path: Path | None = None


def _nonempty_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CaseValidationError(f"{name} must be a non-empty string")
    return value.strip()


def load_case(path: str | Path) -> EvalCase:
    """Load and minimally validate one case.json file."""
    case_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseValidationError(f"cannot load {case_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CaseValidationError("case document must be a JSON object")

    case_id = _nonempty_string(payload, "case_id")
    task = _nonempty_string(payload, "task")
    category = _nonempty_string(payload, "category")
    description = _nonempty_string(payload, "description")

    timeout = payload.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise CaseValidationError("timeout_seconds must be a positive integer")

    verification = payload.get("verification")
    command = verification.get("command") if isinstance(verification, dict) else None
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise CaseValidationError(
            "verification.command must be a non-empty list of strings"
        )

    goal = payload.get("goal")
    if goal is not None and (not isinstance(goal, str) or not goal.strip()):
        raise CaseValidationError("goal must be null or a non-empty string")

    fixture_dir = case_path.parent / "fixture"
    if not fixture_dir.is_dir():
        raise CaseValidationError(f"fixture directory not found: {fixture_dir}")

    return EvalCase(
        case_id=case_id,
        category=category,
        description=description,
        task=task,
        fixture_dir=fixture_dir,
        timeout_seconds=timeout,
        verification=Verification(tuple(command)),
        goal=goal.strip() if isinstance(goal, str) else None,
        source_path=case_path,
    )


def discover_cases(
    suite_dir: str | Path,
    case_id: str | None = None,
) -> list[EvalCase]:
    """Discover cases in deterministic case-id order."""
    cases = [
        load_case(path)
        for path in sorted(Path(suite_dir).glob("*/case.json"))
    ]
    cases.sort(key=lambda item: item.case_id)
    if case_id is None:
        return cases
    selected = [case for case in cases if case.case_id == case_id]
    if not selected:
        raise CaseValidationError(f"case not found: {case_id}")
    return selected
