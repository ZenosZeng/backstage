"""Stable filesystem layout helpers for BEHAVIOR evaluation artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


STEP_PATTERN = re.compile(r"^(?:step|epoch)[-_]?\d+$", re.IGNORECASE)
RUN_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return value or "eval"


def validate_run_namespace(value: str) -> str:
    """Validate a repeat-sampling namespace before using it as a path segment."""
    if not RUN_NAMESPACE_PATTERN.fullmatch(value):
        raise ValueError("run namespace must match [A-Za-z0-9_-]{1,32}")
    return value


@dataclass(frozen=True)
class CheckpointLayout:
    experiment: str
    point: str
    weight: str


def checkpoint_layout(checkpoint: str | Path) -> CheckpointLayout:
    """Derive experiment, step/epoch, and weight without requiring the path to exist."""
    path = Path(checkpoint)
    if path.name.casefold() == "ema" and STEP_PATTERN.fullmatch(path.parent.name):
        point_path = path.parent
        weight = "ema"
    elif STEP_PATTERN.fullmatch(path.name):
        point_path = path
        weight = "raw"
    else:
        return CheckpointLayout(
            experiment=slugify(path.name),
            point="checkpoint",
            weight="raw",
        )

    return CheckpointLayout(
        experiment=slugify(point_path.parent.name),
        point=slugify(point_path.name),
        weight=weight,
    )


def run_directory(
    evaluations_root: Path,
    checkpoint: str | Path,
    task: str,
    action_horizon: int = 16,
    num_steps: int = 10,
    namespace: str | None = None,
) -> Path:
    layout = checkpoint_layout(checkpoint)
    task_directory = (
        evaluations_root
        / layout.experiment
        / layout.point
        / layout.weight
        / slugify(task)
    )
    if namespace:
        task_directory = task_directory / validate_run_namespace(namespace)
    if action_horizon == 16 and num_steps == 10:
        return task_directory
    return task_directory / f"h{action_horizon}-n{num_steps}"


def cloud_run_directory(
    evaluations_root: Path,
    model_id: str,
    model_digest: str,
    task: str,
    action_horizon: int = 16,
) -> Path:
    """Return a stable artifact path for a cloud release without faking a checkpoint."""
    digest = model_digest.removeprefix("sha256:")
    task_directory = (
        evaluations_root
        / "cloud"
        / slugify(model_id)
        / slugify(digest[:16])
        / slugify(task)
    )
    if action_horizon == 16:
        return task_directory
    return task_directory / f"h{action_horizon}"


def find_run_configs(evaluations_root: Path) -> list[Path]:
    return sorted(evaluations_root.rglob("run_config.json"))
