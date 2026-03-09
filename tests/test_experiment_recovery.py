"""Tests for experiment recovery module."""

import json

import pytest

from sibyl.experiment_recovery import (
    ExperimentState,
    load_experiment_state,
    save_experiment_state,
    register_task,
)


class TestExperimentStateIO:
    """Task 1: Core data model and I/O."""

    def test_load_nonexistent_returns_empty(self, tmp_path):
        state = load_experiment_state(tmp_path)
        assert isinstance(state, ExperimentState)
        assert state.schema_version == 1
        assert state.tasks == {}
        assert state.last_recovery_at == ""
        assert state.recovery_log == []

    def test_save_and_load_roundtrip(self, tmp_path):
        state = ExperimentState(
            schema_version=1,
            tasks={"t1": {"status": "running", "gpu_ids": [0, 1]}},
            last_recovery_at="2026-03-09T10:00:00",
            recovery_log=["recovered t1"],
        )
        save_experiment_state(tmp_path, state)

        # Verify file exists
        state_file = tmp_path / "exp" / "experiment_state.json"
        assert state_file.exists()

        loaded = load_experiment_state(tmp_path)
        assert loaded.schema_version == 1
        assert loaded.tasks == {"t1": {"status": "running", "gpu_ids": [0, 1]}}
        assert loaded.last_recovery_at == "2026-03-09T10:00:00"
        assert loaded.recovery_log == ["recovered t1"]

    def test_register_task(self, tmp_path):
        state = load_experiment_state(tmp_path)
        register_task(state, "train_baseline", [0, 1], pid_file="/tmp/train.pid")

        assert "train_baseline" in state.tasks
        task = state.tasks["train_baseline"]
        assert task["status"] == "running"
        assert task["gpu_ids"] == [0, 1]
        assert task["pid_file"] == "/tmp/train.pid"
        assert "registered_at" in task
