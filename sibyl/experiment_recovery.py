"""Experiment recovery: detect and recover from interrupted experiments.

Manages experiment state independently from gpu_progress.json, providing
richer tracking (PID files, recovery logs, detection scripts) for crash
recovery on shared GPU servers.

State file: exp/experiment_state.json (relative to workspace root)
"""

import datetime
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


STATE_FILE = "exp/experiment_state.json"


@dataclass
class ExperimentState:
    """Persistent state for experiment recovery."""

    schema_version: int = 1
    tasks: dict = field(default_factory=dict)
    last_recovery_at: str = ""
    recovery_log: list = field(default_factory=list)


def load_experiment_state(workspace_root: Path) -> ExperimentState:
    """Load experiment state from disk, returning empty state if not found."""
    state_path = workspace_root / STATE_FILE
    if not state_path.exists():
        return ExperimentState()
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        return ExperimentState(
            schema_version=data.get("schema_version", 1),
            tasks=data.get("tasks", {}),
            last_recovery_at=data.get("last_recovery_at", ""),
            recovery_log=data.get("recovery_log", []),
        )
    except (json.JSONDecodeError, OSError):
        return ExperimentState()


def save_experiment_state(workspace_root: Path, state: ExperimentState) -> None:
    """Save experiment state to disk."""
    state_path = workspace_root / STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)


def register_task(
    state: ExperimentState,
    task_id: str,
    gpu_ids: list[int],
    pid_file: str = "",
) -> None:
    """Register a task as running in the experiment state."""
    state.tasks[task_id] = {
        "status": "running",
        "gpu_ids": gpu_ids,
        "pid_file": pid_file,
        "registered_at": datetime.datetime.now().isoformat(),
    }
