"""SWE-bench prediction construction and JSONL output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def build_prediction(
    instance_id: str,
    model: str,
    model_patch: str,
) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "model_name_or_path": f"CodeHarness::{model}",
        "model_patch": model_patch,
    }


def write_prediction(
    path: str | Path,
    prediction: Mapping[str, Any],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(prediction), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
