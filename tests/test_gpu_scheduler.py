"""Tests for sibyl.gpu_scheduler module."""
import json
from pathlib import Path

import pytest

from sibyl.gpu_scheduler import (
    topo_sort_layers, assign_gpus, get_next_batch,
    estimate_batch_minutes, get_batch_info,
)


# ══════════════════════════════════════════════
# Topological sort
# ══════════════════════════════════════════════

class TestTopoSortLayers:
    def test_empty(self):
        assert topo_sort_layers([]) == []

    def test_single_task(self):
        tasks = [{"id": "a", "depends_on": []}]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 1
        assert layers[0][0]["id"] == "a"

    def test_independent_tasks(self):
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": []},
        ]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 1
        assert len(layers[0]) == 3

    def test_linear_chain(self):
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 3
        assert layers[0][0]["id"] == "a"
        assert layers[1][0]["id"] == "b"
        assert layers[2][0]["id"] == "c"

    def test_diamond_dag(self):
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["a"]},
            {"id": "d", "depends_on": ["b", "c"]},
        ]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 3
        assert layers[0][0]["id"] == "a"
        ids_1 = {t["id"] for t in layers[1]}
        assert ids_1 == {"b", "c"}
        assert layers[2][0]["id"] == "d"

    def test_missing_dep_ignored(self):
        """Dependencies referencing non-existent tasks should be ignored."""
        tasks = [
            {"id": "a", "depends_on": ["nonexistent"]},
        ]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 1

    def test_no_depends_on_key(self):
        tasks = [{"id": "a"}, {"id": "b"}]
        layers = topo_sort_layers(tasks)
        assert len(layers) == 1
        assert len(layers[0]) == 2


# ══════════════════════════════════════════════
# GPU assignment
# ══════════════════════════════════════════════

class TestAssignGpus:
    def test_basic_assignment(self):
        tasks = [{"id": "a"}, {"id": "b"}]
        result = assign_gpus(tasks, [0, 1, 2, 3])
        assert len(result) == 2
        assert result[0]["task_ids"] == ["a"]
        assert result[0]["gpu_ids"] == [0]
        assert result[1]["task_ids"] == ["b"]
        assert result[1]["gpu_ids"] == [1]

    def test_per_task_gpu_count(self):
        """Each task declares its own gpu_count."""
        tasks = [
            {"id": "a", "gpu_count": 2},
            {"id": "b", "gpu_count": 1},
        ]
        result = assign_gpus(tasks, [0, 1, 2, 3])
        assert len(result) == 2
        assert result[0]["gpu_ids"] == [0, 1]  # task a gets 2 GPUs
        assert result[1]["gpu_ids"] == [2]      # task b gets 1 GPU

    def test_mixed_gpu_counts_exhaust(self):
        """Tasks with different gpu_count, not enough GPUs for all."""
        tasks = [
            {"id": "a", "gpu_count": 2},
            {"id": "b", "gpu_count": 2},
            {"id": "c", "gpu_count": 1},
        ]
        result = assign_gpus(tasks, [0, 1, 2, 3])
        assert len(result) == 2  # a=2, b=2, c can't fit
        assert result[0]["gpu_ids"] == [0, 1]
        assert result[1]["gpu_ids"] == [2, 3]

    def test_default_gpus_per_task_fallback(self):
        """Tasks without gpu_count use the default."""
        tasks = [{"id": "a"}, {"id": "b"}]
        result = assign_gpus(tasks, [0, 1, 2, 3], default_gpus_per_task=2)
        assert len(result) == 2
        assert result[0]["gpu_ids"] == [0, 1]
        assert result[1]["gpu_ids"] == [2, 3]

    def test_task_gpu_count_overrides_default(self):
        """Per-task gpu_count takes precedence over default."""
        tasks = [
            {"id": "a", "gpu_count": 1},  # overrides default 2
            {"id": "b"},                   # uses default 2
        ]
        result = assign_gpus(tasks, [0, 1, 2, 3], default_gpus_per_task=2)
        assert len(result) == 2
        assert result[0]["gpu_ids"] == [0]
        assert result[1]["gpu_ids"] == [1, 2]

    def test_more_tasks_than_gpus(self):
        tasks = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        result = assign_gpus(tasks, [0, 1])
        assert len(result) == 2  # only 2 GPUs available

    def test_task_needs_more_than_total_gpus(self):
        """Task needs 4 GPUs but only 2 available → gets all GPUs."""
        tasks = [{"id": "a", "gpu_count": 4}]
        result = assign_gpus(tasks, [0, 1])
        assert len(result) == 1
        assert result[0]["gpu_ids"] == [0, 1]

    def test_empty_inputs(self):
        assert assign_gpus([], [0, 1]) == []
        assert assign_gpus([{"id": "a"}], []) == []


# ══════════════════════════════════════════════
# Time estimation
# ══════════════════════════════════════════════

class TestEstimateBatchMinutes:
    def test_default(self):
        assert estimate_batch_minutes([], []) == 10

    def test_uses_task_estimate(self):
        tasks = [
            {"id": "a", "estimated_minutes": 30},
            {"id": "b", "estimated_minutes": 60},
        ]
        batch = [
            {"task_ids": ["a"], "gpu_ids": [0]},
            {"task_ids": ["b"], "gpu_ids": [1]},
        ]
        assert estimate_batch_minutes(batch, tasks) == 60  # max

    def test_missing_estimate_uses_default(self):
        tasks = [{"id": "a"}]
        batch = [{"task_ids": ["a"], "gpu_ids": [0]}]
        assert estimate_batch_minutes(batch, tasks, default_minutes=15) == 15

    def test_single_long_task(self):
        tasks = [
            {"id": "a", "estimated_minutes": 120},
            {"id": "b", "estimated_minutes": 5},
        ]
        batch = [{"task_ids": ["a"], "gpu_ids": [0]}]
        assert estimate_batch_minutes(batch, tasks) == 120


# ══════════════════════════════════════════════
# Batch scheduling (get_next_batch)
# ══════════════════════════════════════════════

class TestGetNextBatch:
    def test_no_task_plan(self, tmp_path):
        """No task_plan.json → returns None (fallback)."""
        result = get_next_batch(tmp_path, [0, 1])
        assert result is None

    def test_empty_tasks(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": []}))
        result = get_next_batch(tmp_path, [0, 1])
        assert result is None

    def test_no_tasks_key(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "task_plan.json").write_text(json.dumps({"description": "test"}))
        result = get_next_batch(tmp_path, [0, 1])
        assert result is None

    def test_first_batch(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": ["a", "b"]},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        result = get_next_batch(tmp_path, [0, 1, 2, 3])
        assert result is not None
        assert len(result) == 2  # a and b are ready
        ids = [r["task_ids"][0] for r in result]
        assert set(ids) == {"a", "b"}

    def test_second_batch_after_progress(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": ["a", "b"]},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        # Mark a and b complete
        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "gpu_progress.json").write_text(json.dumps({
            "completed": ["a", "b"], "failed": []
        }))

        result = get_next_batch(tmp_path, [0, 1, 2, 3])
        assert result is not None
        assert len(result) == 1
        assert result[0]["task_ids"] == ["c"]

    def test_all_complete(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [{"id": "a", "depends_on": []}]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "gpu_progress.json").write_text(json.dumps({
            "completed": ["a"], "failed": []
        }))

        result = get_next_batch(tmp_path, [0, 1])
        assert result is None  # all done

    def test_blocked_tasks(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "c", "depends_on": []},
            {"id": "b", "depends_on": ["a", "c"]},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))
        exp_dir = tmp_path / "exp"
        exp_dir.mkdir(exist_ok=True)
        (exp_dir / "gpu_progress.json").write_text(json.dumps({
            "completed": ["a"], "failed": []
        }))

        result = get_next_batch(tmp_path, [0, 1])
        # c is ready (no deps, not completed), b is blocked on c
        assert result is not None
        assert len(result) == 1
        assert result[0]["task_ids"] == ["c"]

    def test_per_task_gpu_count(self, tmp_path):
        """Tasks with per-task gpu_count are respected."""
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": [], "gpu_count": 2},
            {"id": "b", "depends_on": [], "gpu_count": 2},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        result = get_next_batch(tmp_path, [0, 1, 2, 3])
        assert len(result) == 2
        assert result[0]["gpu_ids"] == [0, 1]
        assert result[1]["gpu_ids"] == [2, 3]

    def test_corrupt_json(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "task_plan.json").write_text("not valid json {{{")
        result = get_next_batch(tmp_path, [0, 1])
        assert result is None


# ══════════════════════════════════════════════
# Batch info (with timing metadata)
# ══════════════════════════════════════════════

class TestGetBatchInfo:
    def test_no_task_plan(self, tmp_path):
        assert get_batch_info(tmp_path, [0, 1]) is None

    def test_returns_metadata(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": [], "estimated_minutes": 30},
            {"id": "b", "depends_on": [], "estimated_minutes": 60},
            {"id": "c", "depends_on": ["a", "b"], "estimated_minutes": 15},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        info = get_batch_info(tmp_path, [0, 1, 2, 3])
        assert info is not None
        assert len(info["batch"]) == 2
        assert info["estimated_minutes"] == 60  # max of a=30, b=60
        assert info["remaining_count"] == 3
        assert info["total_count"] == 3

    def test_progress_updates_counts(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))
        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "gpu_progress.json").write_text(json.dumps({
            "completed": ["a"], "failed": []
        }))

        info = get_batch_info(tmp_path, [0, 1])
        assert info["remaining_count"] == 1
        assert info["total_count"] == 2
        assert len(info["batch"]) == 1

    def test_all_complete_returns_none(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [{"id": "a", "depends_on": []}]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))
        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "gpu_progress.json").write_text(json.dumps({
            "completed": ["a"], "failed": []
        }))

        assert get_batch_info(tmp_path, [0, 1]) is None

    def test_blocked_returns_empty_batch(self, tmp_path):
        plan_dir = tmp_path / "plan"
        plan_dir.mkdir()
        tasks = [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},  # circular → both blocked
        ]
        (plan_dir / "task_plan.json").write_text(json.dumps({"tasks": tasks}))

        info = get_batch_info(tmp_path, [0, 1])
        assert info is not None
        assert info["batch"] == []
        assert info["remaining_count"] == 2
