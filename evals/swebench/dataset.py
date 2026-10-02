"""Load one SWE-bench instance without a hard runtime dependency."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_DATASET_NAME = "SWE-bench/SWE-bench_Lite"
DEFAULT_SPLIT = "test"
DEPENDENCY_ERROR = "SWE-bench evaluation dependencies are not installed."


class SWEbenchDependencyError(RuntimeError):
    """Raised when an optional SWE-bench evaluation dependency is absent."""


class SWEbenchInstanceError(ValueError):
    """Raised when a dataset row is missing required instance fields."""


@dataclass(frozen=True)
class SWEbenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> SWEbenchInstance:
        payload = dict(raw)

        def required(name: str) -> str:
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise SWEbenchInstanceError(
                    f"SWE-bench instance field {name!r} must be a non-empty string"
                )
            return value.strip()

        return cls(
            instance_id=required("instance_id"),
            repo=required("repo"),
            base_commit=required("base_commit"),
            problem_statement=required("problem_statement"),
            raw=payload,
        )


DatasetLoader = Callable[..., Iterable[Mapping[str, Any]]]


def _load_huggingface_dataset(
    dataset_name: str,
    *,
    split: str,
) -> Iterable[Mapping[str, Any]]:
    try:
        from datasets import load_dataset
    except (ImportError, ModuleNotFoundError) as exc:
        raise SWEbenchDependencyError(DEPENDENCY_ERROR) from exc
    return load_dataset(dataset_name, split=split)


def load_instance(
    instance_id: str,
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
    split: str = DEFAULT_SPLIT,
    dataset_loader: DatasetLoader | None = None,
) -> SWEbenchInstance:
    """Return exactly one requested instance from the configured split."""
    loader = dataset_loader or _load_huggingface_dataset
    for raw in loader(dataset_name, split=split):
        if raw.get("instance_id") == instance_id:
            return SWEbenchInstance.from_raw(raw)
    raise LookupError(f"SWE-bench instance not found: {instance_id}")
