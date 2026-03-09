# Experiment State Tracking & Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add reliable experiment task state tracking and recovery so interrupted projects can resume experiments without losing progress.

**Architecture:** New `sibyl/experiment_recovery.py` module as the recovery engine, `exp/experiment_state.json` as source of truth for task lifecycle, integrated into orchestrator's experiment batch entry and resume flow. Experimenter prompts updated to write PID files and progress reports.

**Tech Stack:** Python 3.12, pytest, JSON state files, SSH batch detection scripts

---

### Task 1: Core Data Model — `experiment_state.json` I/O

**Files:**
- Create: `sibyl/experiment_recovery.py`
- Create: `tests/test_experiment_recovery.py`

**Step 1: Write the failing test**

```python
# tests/test_experiment_recovery.py
"""Tests for sibyl.experiment_recovery module."""
import json
from pathlib import Path

import pytest

from sibyl.experiment_recovery import (
    ExperimentState,
    load_experiment_state,
    save_experiment_state,
    register_task,
)


class TestExperimentStateIO:
    """Test experiment_state.json read/write."""

    def test_load_nonexistent_returns_empty(self, tmp_path):
        state = load_experiment_state(tmp_path)
        assert state.tasks == {}
        assert state.last_recovery_at is None
        assert state.recovery_log == []

    def test_save_and_load_roundtrip(self, tmp_path):
        state = ExperimentState()
        state.tasks["train_baseline"] = {
            "status": "running",
            "gpu_ids": [0, 1],
            "remote_pid": 12345,
            "pid_file": "/tmp/train_baseline.pid",
            "started_at": "2026-03-09T14:00:00",
            "completed_at": None,
            "exit_code": None,
            "error_summary": None,
            "progress": {},
        }
        save_experiment_state(tmp_path, state)
        loaded = load_experiment_state(tmp_path)
        assert loaded.tasks["train_baseline"]["status"] == "running"
        assert loaded.tasks["train_baseline"]["gpu_ids"] == [0, 1]

    def test_register_task(self, tmp_path):
        state = load_experiment_state(tmp_path)
        register_task(state, "task_a", gpu_ids=[0], pid_file="/tmp/task_a.pid")
        assert state.tasks["task_a"]["status"] == "running"
        assert state.tasks["task_a"]["gpu_ids"] == [0]
        assert state.tasks["task_a"]["started_at"] is not None
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestExperimentStateIO -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'sibyl.experiment_recovery'"

**Step 3: Write minimal implementation**

```python
# sibyl/experiment_recovery.py
"""Experiment state tracking and recovery.

Manages exp/experiment_state.json as the authoritative source of truth
for experiment task lifecycle, separate from gpu_progress.json (scheduling).

Supports recovery detection when projects are interrupted: checks remote
PID files and DONE markers to determine actual task status.
"""
import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
STATE_FILENAME = "exp/experiment_state.json"


@dataclass
class ExperimentState:
    """In-memory representation of experiment_state.json."""
    schema_version: int = SCHEMA_VERSION
    tasks: dict[str, dict] = field(default_factory=dict)
    last_recovery_at: str | None = None
    recovery_log: list[dict] = field(default_factory=list)


def load_experiment_state(workspace_root: Path) -> ExperimentState:
    """Load experiment state from workspace. Returns empty state if not found."""
    path = workspace_root / STATE_FILENAME
    if not path.exists():
        return ExperimentState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = ExperimentState()
        state.schema_version = data.get("schema_version", SCHEMA_VERSION)
        state.tasks = data.get("tasks", {})
        state.last_recovery_at = data.get("last_recovery_at")
        state.recovery_log = data.get("recovery_log", [])
        return state
    except (json.JSONDecodeError, OSError):
        return ExperimentState()


def save_experiment_state(workspace_root: Path, state: ExperimentState) -> None:
    """Persist experiment state to workspace."""
    path = workspace_root / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": state.schema_version,
        "tasks": state.tasks,
        "last_recovery_at": state.last_recovery_at,
        "recovery_log": state.recovery_log,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def register_task(
    state: ExperimentState,
    task_id: str,
    gpu_ids: list[int],
    pid_file: str = "",
) -> None:
    """Register a task as running in experiment state."""
    state.tasks[task_id] = {
        "status": "running",
        "gpu_ids": gpu_ids,
        "remote_pid": None,
        "pid_file": pid_file,
        "started_at": datetime.datetime.now().isoformat(),
        "completed_at": None,
        "exit_code": None,
        "error_summary": None,
        "progress": {},
    }
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestExperimentStateIO -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add sibyl/experiment_recovery.py tests/test_experiment_recovery.py
git commit -m "feat: add experiment state data model and I/O"
git push
```

---

### Task 2: SSH Batch Detection Script Generation

**Files:**
- Modify: `sibyl/experiment_recovery.py`
- Modify: `tests/test_experiment_recovery.py`

**Step 1: Write the failing test**

```python
class TestRecoveryScriptGeneration:
    """Test SSH batch detection script generation and output parsing."""

    def test_generate_detection_script(self):
        from sibyl.experiment_recovery import generate_detection_script
        script = generate_detection_script(
            remote_project_dir="/home/user/sibyl/projects/ttt-dlm",
            task_ids=["train_baseline", "train_ablation"],
        )
        assert "train_baseline" in script
        assert "train_ablation" in script
        assert "_DONE" in script
        assert ".pid" in script
        assert "kill -0" in script
        assert "_PROGRESS.json" in script

    def test_parse_detection_output_done(self):
        from sibyl.experiment_recovery import parse_detection_output
        output = 'DONE:train_baseline:{"task_id":"train_baseline","status":"success","summary":"ok","timestamp":"2026-03-09T15:00:00"}'
        results = parse_detection_output(output)
        assert results["train_baseline"]["detected_status"] == "done"
        assert results["train_baseline"]["done_info"]["status"] == "success"

    def test_parse_detection_output_running(self):
        from sibyl.experiment_recovery import parse_detection_output
        output = 'RUNNING:task_a:{"epoch":50,"total_epochs":100,"loss":0.32}'
        results = parse_detection_output(output)
        assert results["task_a"]["detected_status"] == "running"
        assert results["task_a"]["progress"]["epoch"] == 50

    def test_parse_detection_output_dead(self):
        from sibyl.experiment_recovery import parse_detection_output
        output = "DEAD:task_b:99999"
        results = parse_detection_output(output)
        assert results["task_b"]["detected_status"] == "dead"
        assert results["task_b"]["dead_pid"] == "99999"

    def test_parse_detection_output_unknown(self):
        from sibyl.experiment_recovery import parse_detection_output
        output = "UNKNOWN:task_c"
        results = parse_detection_output(output)
        assert results["task_c"]["detected_status"] == "unknown"

    def test_parse_multiline_output(self):
        from sibyl.experiment_recovery import parse_detection_output
        output = (
            'DONE:task_a:{"status":"success"}\n'
            'RUNNING:task_b:{"epoch":10}\n'
            'DEAD:task_c:12345\n'
        )
        results = parse_detection_output(output)
        assert len(results) == 3
        assert results["task_a"]["detected_status"] == "done"
        assert results["task_b"]["detected_status"] == "running"
        assert results["task_c"]["detected_status"] == "dead"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestRecoveryScriptGeneration -v`
Expected: FAIL with "ImportError: cannot import name 'generate_detection_script'"

**Step 3: Write minimal implementation**

Add to `sibyl/experiment_recovery.py`:

```python
def generate_detection_script(
    remote_project_dir: str,
    task_ids: list[str],
) -> str:
    """Generate bash script to detect task status on remote server.

    Designed to run as a single SSH command. For each task, checks:
    1. DONE marker file → completed/failed
    2. PID file + kill -0 → still running (with progress)
    3. PID file + dead process → crashed
    4. No files → unknown

    Returns bash script string.
    """
    task_ids_str = " ".join(task_ids)
    return f'''#!/bin/bash
cd "{remote_project_dir}" 2>/dev/null || exit 1
for task_id in {task_ids_str}; do
  if [ -f "exp/results/${{task_id}}_DONE" ]; then
    content=$(cat "exp/results/${{task_id}}_DONE" 2>/dev/null || echo '{{}}')
    echo "DONE:${{task_id}}:${{content}}"
  elif [ -f "exp/results/${{task_id}}.pid" ]; then
    pid=$(cat "exp/results/${{task_id}}.pid")
    if kill -0 "$pid" 2>/dev/null; then
      progress=$(cat "exp/results/${{task_id}}_PROGRESS.json" 2>/dev/null || echo '{{}}')
      echo "RUNNING:${{task_id}}:${{progress}}"
    else
      echo "DEAD:${{task_id}}:${{pid}}"
    fi
  else
    echo "UNKNOWN:${{task_id}}"
  fi
done
'''


def parse_detection_output(output: str) -> dict[str, dict]:
    """Parse the output of generate_detection_script.

    Returns dict: {task_id: {detected_status, ...extra_info}}
    """
    results = {}
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: STATUS:task_id:payload
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        status_tag = parts[0]
        task_id = parts[1]
        payload = parts[2] if len(parts) > 2 else ""

        if status_tag == "DONE":
            done_info = {}
            try:
                done_info = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                pass
            results[task_id] = {"detected_status": "done", "done_info": done_info}
        elif status_tag == "RUNNING":
            progress = {}
            try:
                progress = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                pass
            results[task_id] = {"detected_status": "running", "progress": progress}
        elif status_tag == "DEAD":
            results[task_id] = {"detected_status": "dead", "dead_pid": payload}
        elif status_tag == "UNKNOWN":
            results[task_id] = {"detected_status": "unknown"}

    return results
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestRecoveryScriptGeneration -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add sibyl/experiment_recovery.py tests/test_experiment_recovery.py
git commit -m "feat: add SSH batch detection script generation and parsing"
git push
```

---

### Task 3: Core Recovery Logic — `recover_experiments()`

**Files:**
- Modify: `sibyl/experiment_recovery.py`
- Modify: `tests/test_experiment_recovery.py`

**Step 1: Write the failing test**

```python
from sibyl.experiment_recovery import (
    RecoveryResult,
    recover_from_detection,
    get_running_tasks,
)


class TestRecoveryLogic:
    """Test the core recovery state machine."""

    def _make_state_with_running(self, tmp_path, task_ids):
        """Helper: create experiment_state.json with running tasks."""
        state = ExperimentState()
        for tid in task_ids:
            register_task(state, tid, gpu_ids=[0], pid_file=f"/tmp/{tid}.pid")
        save_experiment_state(tmp_path, state)
        return state

    def test_get_running_tasks_filters_correctly(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["a", "b"])
        state.tasks["a"]["status"] = "completed"
        running = get_running_tasks(state)
        assert running == ["b"]

    def test_recover_done_marks_completed(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {"detected_status": "done", "done_info": {"status": "success"}}}
        result = recover_from_detection(state, detection)
        assert "task_a" in result.recovered_completed
        assert state.tasks["task_a"]["status"] == "completed"
        assert state.tasks["task_a"]["completed_at"] is not None

    def test_recover_done_failed_marks_failed(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {"detected_status": "done", "done_info": {"status": "failed"}}}
        result = recover_from_detection(state, detection)
        assert "task_a" in result.recovered_failed
        assert state.tasks["task_a"]["status"] == "failed"

    def test_recover_running_keeps_running_with_progress(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {
            "detected_status": "running",
            "progress": {"epoch": 50, "total_epochs": 100},
        }}
        result = recover_from_detection(state, detection)
        assert "task_a" in result.still_running
        assert state.tasks["task_a"]["status"] == "running"
        assert state.tasks["task_a"]["progress"]["epoch"] == 50
        assert result.needs_monitor is True

    def test_recover_dead_marks_failed(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {"detected_status": "dead", "dead_pid": "99999"}}
        result = recover_from_detection(state, detection)
        assert "task_a" in result.recovered_failed
        assert state.tasks["task_a"]["status"] == "failed"
        assert "process_disappeared" in state.tasks["task_a"]["error_summary"]

    def test_recover_unknown_marks_failed(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {"detected_status": "unknown"}}
        result = recover_from_detection(state, detection)
        assert "task_a" in result.recovered_failed

    def test_recovery_result_needs_monitor(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["a", "b"])
        detection = {
            "a": {"detected_status": "done", "done_info": {"status": "success"}},
            "b": {"detected_status": "running", "progress": {}},
        }
        result = recover_from_detection(state, detection)
        assert result.needs_monitor is True
        assert result.recovered_completed == ["a"]
        assert result.still_running == ["b"]

    def test_recovery_log_appended(self, tmp_path):
        state = self._make_state_with_running(tmp_path, ["task_a"])
        detection = {"task_a": {"detected_status": "done", "done_info": {"status": "success"}}}
        recover_from_detection(state, detection)
        assert len(state.recovery_log) == 1
        assert state.recovery_log[0]["recovered_completed"] == ["task_a"]
        assert state.last_recovery_at is not None
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestRecoveryLogic -v`
Expected: FAIL with "ImportError"

**Step 3: Write minimal implementation**

Add to `sibyl/experiment_recovery.py`:

```python
@dataclass
class RecoveryResult:
    """Result of experiment recovery detection."""
    recovered_completed: list[str] = field(default_factory=list)
    still_running: list[str] = field(default_factory=list)
    recovered_failed: list[str] = field(default_factory=list)
    ssh_unreachable: bool = False
    needs_monitor: bool = False
    progress: dict[str, dict] = field(default_factory=dict)


def get_running_tasks(state: ExperimentState) -> list[str]:
    """Get task IDs with status == 'running'."""
    return [tid for tid, info in state.tasks.items() if info["status"] == "running"]


def recover_from_detection(
    state: ExperimentState,
    detection: dict[str, dict],
) -> RecoveryResult:
    """Apply detection results to experiment state.

    Updates state in-place and returns RecoveryResult.

    Args:
        state: Current experiment state (modified in-place)
        detection: Output of parse_detection_output()
    """
    now = datetime.datetime.now().isoformat()
    result = RecoveryResult()

    for task_id, info in detection.items():
        if task_id not in state.tasks:
            continue
        task = state.tasks[task_id]
        detected = info["detected_status"]

        if detected == "done":
            done_info = info.get("done_info", {})
            done_status = done_info.get("status", "success")
            if done_status == "failed":
                task["status"] = "failed"
                task["error_summary"] = done_info.get("summary", "task reported failure")
                task["completed_at"] = now
                result.recovered_failed.append(task_id)
            else:
                task["status"] = "completed"
                task["completed_at"] = now
                result.recovered_completed.append(task_id)

        elif detected == "running":
            task["status"] = "running"
            progress = info.get("progress", {})
            if progress:
                task["progress"] = progress
                result.progress[task_id] = progress
            result.still_running.append(task_id)

        elif detected == "dead":
            task["status"] = "failed"
            task["error_summary"] = f"process_disappeared (pid={info.get('dead_pid', '?')})"
            task["completed_at"] = now
            result.recovered_failed.append(task_id)

        elif detected == "unknown":
            task["status"] = "failed"
            task["error_summary"] = "no_pid_no_done (unknown state)"
            task["completed_at"] = now
            result.recovered_failed.append(task_id)

    result.needs_monitor = len(result.still_running) > 0

    # Update state metadata
    state.last_recovery_at = now
    state.recovery_log.append({
        "timestamp": now,
        "recovered_completed": result.recovered_completed,
        "still_running": result.still_running,
        "recovered_failed": result.recovered_failed,
        "ssh_unreachable": result.ssh_unreachable,
    })

    return result
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestRecoveryLogic -v`
Expected: PASS (8 tests)

**Step 5: Commit**

```bash
git add sibyl/experiment_recovery.py tests/test_experiment_recovery.py
git commit -m "feat: add core experiment recovery logic"
git push
```

---

### Task 4: State Sync — Sync `experiment_state` with `gpu_progress`

**Files:**
- Modify: `sibyl/experiment_recovery.py`
- Modify: `tests/test_experiment_recovery.py`

**Step 1: Write the failing test**

```python
class TestStateSyncWithGpuProgress:
    """Test synchronization between experiment_state and gpu_progress."""

    def test_sync_completed_removes_from_gpu_running(self, tmp_path):
        from sibyl.experiment_recovery import sync_to_gpu_progress
        from sibyl.gpu_scheduler import register_running_tasks, _load_progress
        # Setup: task in gpu_progress as running
        register_running_tasks(tmp_path, {"task_a": [0]})
        # State says completed
        state = ExperimentState()
        state.tasks["task_a"] = {"status": "completed", "gpu_ids": [0]}
        sync_to_gpu_progress(tmp_path, state)
        completed, running_ids, _, _ = _load_progress(tmp_path)
        assert "task_a" in completed
        assert "task_a" not in running_ids

    def test_sync_failed_removes_from_gpu_progress(self, tmp_path):
        from sibyl.experiment_recovery import sync_to_gpu_progress
        from sibyl.gpu_scheduler import register_running_tasks, _load_progress
        register_running_tasks(tmp_path, {"task_a": [0]})
        state = ExperimentState()
        state.tasks["task_a"] = {"status": "failed", "gpu_ids": [0]}
        sync_to_gpu_progress(tmp_path, state)
        completed, running_ids, _, _ = _load_progress(tmp_path)
        assert "task_a" not in running_ids
        # Failed tasks should be in the 'failed' list
        progress_path = tmp_path / "exp" / "gpu_progress.json"
        progress = json.loads(progress_path.read_text())
        assert "task_a" in progress.get("failed", [])

    def test_sync_running_backfills_gpu_progress(self, tmp_path):
        from sibyl.experiment_recovery import sync_to_gpu_progress
        from sibyl.gpu_scheduler import _load_progress
        # experiment_state says running but gpu_progress has nothing
        state = ExperimentState()
        state.tasks["task_a"] = {"status": "running", "gpu_ids": [0, 1]}
        sync_to_gpu_progress(tmp_path, state)
        _, running_ids, running_map, _ = _load_progress(tmp_path)
        assert "task_a" in running_ids
        assert running_map["task_a"]["gpu_ids"] == [0, 1]

    def test_migrate_from_gpu_progress_only(self, tmp_path):
        """Old project with gpu_progress but no experiment_state."""
        from sibyl.experiment_recovery import migrate_from_gpu_progress
        from sibyl.gpu_scheduler import register_running_tasks
        register_running_tasks(tmp_path, {"task_a": [0], "task_b": [1]})
        state = migrate_from_gpu_progress(tmp_path)
        assert "task_a" in state.tasks
        assert "task_b" in state.tasks
        assert state.tasks["task_a"]["status"] == "running"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestStateSyncWithGpuProgress -v`
Expected: FAIL with "ImportError"

**Step 3: Write minimal implementation**

Add to `sibyl/experiment_recovery.py`:

```python
def sync_to_gpu_progress(workspace_root: Path, state: ExperimentState) -> None:
    """Synchronize experiment_state to gpu_progress.json.

    Ensures the scheduling view matches the authoritative lifecycle state:
    - completed tasks: add to completed list, remove from running
    - failed tasks: add to failed list, remove from running
    - running tasks: ensure present in running map
    """
    progress_path = workspace_root / "exp" / "gpu_progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    progress = {"completed": [], "failed": [], "running": {}, "timings": {}}
    if progress_path.exists():
        try:
            progress.update(json.loads(progress_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    # Ensure lists exist
    if "completed" not in progress:
        progress["completed"] = []
    if "failed" not in progress:
        progress["failed"] = []
    if "running" not in progress:
        progress["running"] = {}

    for task_id, task in state.tasks.items():
        status = task["status"]

        if status == "completed":
            # Add to completed, remove from running
            if task_id not in progress["completed"]:
                progress["completed"].append(task_id)
            progress["running"].pop(task_id, None)

        elif status == "failed":
            # Add to failed, remove from running
            if task_id not in progress["failed"]:
                progress["failed"].append(task_id)
            progress["running"].pop(task_id, None)

        elif status == "running":
            # Ensure present in running map
            if task_id not in progress["running"]:
                progress["running"][task_id] = {
                    "gpu_ids": task.get("gpu_ids", []),
                    "started_at": task.get("started_at", ""),
                }

    progress_path.write_text(
        json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def migrate_from_gpu_progress(workspace_root: Path) -> ExperimentState:
    """Create experiment_state from existing gpu_progress.json.

    Used for backward compatibility with old projects that only have
    gpu_progress.json.
    """
    from sibyl.gpu_scheduler import _load_progress

    state = ExperimentState()
    completed, running_ids, running_map, timings = _load_progress(workspace_root)

    for task_id in completed:
        state.tasks[task_id] = {
            "status": "completed",
            "gpu_ids": [],
            "remote_pid": None,
            "pid_file": "",
            "started_at": timings.get(task_id, {}).get("started_at", ""),
            "completed_at": timings.get(task_id, {}).get("completed_at", ""),
            "exit_code": None,
            "error_summary": None,
            "progress": {},
        }

    for task_id in running_ids:
        info = running_map.get(task_id, {})
        state.tasks[task_id] = {
            "status": "running",
            "gpu_ids": info.get("gpu_ids", []),
            "remote_pid": None,
            "pid_file": "",
            "started_at": info.get("started_at", ""),
            "completed_at": None,
            "exit_code": None,
            "error_summary": None,
            "progress": {},
        }

    return state
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestStateSyncWithGpuProgress -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add sibyl/experiment_recovery.py tests/test_experiment_recovery.py
git commit -m "feat: add experiment state <-> gpu_progress sync and migration"
git push
```

---

### Task 5: Orchestrator Integration — Register Tasks in `experiment_state`

**Files:**
- Modify: `sibyl/orchestrate.py:809-817` — after `register_running_tasks`, also register in experiment_state
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

Add to `tests/test_orchestrate.py`:

```python
class TestExperimentStateIntegration:
    """Test experiment_state.json integration in orchestrator."""

    def test_experiment_batch_registers_in_experiment_state(self, make_orchestrator):
        from sibyl.experiment_recovery import load_experiment_state
        o = make_orchestrator(stage="pilot_experiments", gpu_poll_enabled=False)
        tasks = [
            {"id": "a", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10},
            {"id": "b", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10},
        ]
        o.ws.write_file("plan/task_plan.json", json.dumps({"tasks": tasks}))
        action = o.get_next_action()
        # Check experiment_state.json was created
        state = load_experiment_state(o.ws.active_root)
        assert "a" in state.tasks
        assert "b" in state.tasks
        assert state.tasks["a"]["status"] == "running"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateIntegration::test_experiment_batch_registers_in_experiment_state -v`
Expected: FAIL — experiment_state.json not created

**Step 3: Write minimal implementation**

In `sibyl/orchestrate.py`, after line 817 (`register_running_tasks(...)`), add:

```python
        # Register in experiment_state.json (authoritative lifecycle)
        from sibyl.experiment_recovery import (
            load_experiment_state, save_experiment_state, register_task,
        )
        exp_state = load_experiment_state(self.ws.active_root)
        remote_dir = f"{self.config.remote_base}/projects/{self.ws.name}"
        for tid, gpus in task_gpu_map.items():
            pid_file = f"{remote_dir}/exp/results/{tid}.pid"
            register_task(exp_state, tid, gpu_ids=gpus, pid_file=pid_file)
        save_experiment_state(self.ws.active_root, exp_state)
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateIntegration -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: register experiment tasks in experiment_state.json"
git push
```

---

### Task 6: Orchestrator Integration — Auto-Recovery in `_action_experiment_batch`

**Files:**
- Modify: `sibyl/orchestrate.py:703-718` — add recovery check at entry
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

Add to `tests/test_orchestrate.py::TestExperimentStateIntegration`:

```python
    def test_experiment_batch_auto_recovers_completed(self, make_orchestrator):
        """If experiment_state has running tasks with DONE detection, recover."""
        from sibyl.experiment_recovery import (
            load_experiment_state, save_experiment_state, register_task,
            ExperimentState,
        )
        o = make_orchestrator(stage="pilot_experiments", gpu_poll_enabled=False)
        tasks = [
            {"id": "a", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10},
            {"id": "b", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10},
        ]
        o.ws.write_file("plan/task_plan.json", json.dumps({"tasks": tasks}))

        # Simulate: experiment_state says both running, but "a" actually completed
        state = ExperimentState()
        register_task(state, "a", gpu_ids=[0])
        register_task(state, "b", gpu_ids=[1])
        save_experiment_state(o.ws.active_root, state)

        # Simulate gpu_progress also tracking running
        from sibyl.gpu_scheduler import register_running_tasks
        register_running_tasks(o.ws.active_root, {"a": [0], "b": [1]})

        # Mark "a" as completed in gpu_progress
        progress_path = o.ws.active_path("exp/gpu_progress.json")
        progress = json.loads(progress_path.read_text())
        progress["completed"] = ["a"]
        del progress["running"]["a"]
        progress_path.write_text(json.dumps(progress))

        # Now get_next_action should see "a" completed and only schedule remaining
        action = o.get_next_action()
        # "b" should be dispatched (already running or re-dispatched)
        assert action["stage"] == "pilot_experiments"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateIntegration::test_experiment_batch_auto_recovers_completed -v`

**Step 3: Write minimal implementation**

At the start of `_action_experiment_batch` (after the GPU poll check, before task plan validation ~line 737), add:

```python
        # --- Auto-recovery: check for stale running tasks ---
        from sibyl.experiment_recovery import (
            load_experiment_state, save_experiment_state,
            get_running_tasks, sync_to_gpu_progress,
            migrate_from_gpu_progress,
        )
        exp_state = load_experiment_state(self.ws.active_root)
        # Backward compat: migrate from gpu_progress if no experiment_state
        if not exp_state.tasks:
            from sibyl.gpu_scheduler import _load_progress
            _, running_ids, _, _ = _load_progress(self.ws.active_root)
            if running_ids:
                exp_state = migrate_from_gpu_progress(self.ws.active_root)
                save_experiment_state(self.ws.active_root, exp_state)

        running_tasks = get_running_tasks(exp_state)
        if running_tasks:
            # Sync state from gpu_progress (local check, no SSH needed here)
            # SSH-based recovery is done via cli_recover_experiments or
            # triggered by the calling skill before cli_next
            completed_set, _, _, _ = _load_progress(self.ws.active_root)
            for tid in running_tasks:
                if tid in completed_set:
                    exp_state.tasks[tid]["status"] = "completed"
                    exp_state.tasks[tid]["completed_at"] = (
                        datetime.datetime.now().isoformat()
                    )
            save_experiment_state(self.ws.active_root, exp_state)
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateIntegration -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: auto-recover experiment state on batch entry"
git push
```

---

### Task 7: CLI — `cli_recover_experiments`

**Files:**
- Modify: `sibyl/orchestrate.py` — add `cli_recover_experiments` function
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

```python
class TestCliRecoverExperiments:
    def test_no_running_tasks(self, make_orchestrator):
        from sibyl.orchestrate import cli_recover_experiments
        import io, sys
        o = make_orchestrator(stage="pilot_experiments")
        captured = io.StringIO()
        sys.stdout = captured
        cli_recover_experiments(str(o.ws.root))
        sys.stdout = sys.__stdout__
        result = json.loads(captured.getvalue())
        assert result["status"] == "no_recovery_needed"

    def test_with_running_tasks_no_ssh(self, make_orchestrator):
        """Without SSH, just reports current state."""
        from sibyl.orchestrate import cli_recover_experiments
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
        )
        import io, sys
        o = make_orchestrator(stage="pilot_experiments")
        state = ExperimentState()
        register_task(state, "task_a", gpu_ids=[0])
        save_experiment_state(o.ws.active_root, state)

        captured = io.StringIO()
        sys.stdout = captured
        cli_recover_experiments(str(o.ws.root))
        sys.stdout = sys.__stdout__
        result = json.loads(captured.getvalue())
        assert result["status"] == "has_running_tasks"
        assert "task_a" in result["running_tasks"]
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliRecoverExperiments -v`
Expected: FAIL with "ImportError"

**Step 3: Write minimal implementation**

Add to `sibyl/orchestrate.py` near the other CLI functions:

```python
def cli_recover_experiments(workspace_path: str):
    """CLI: Check and recover experiment task states.

    Reads experiment_state.json, reports running tasks, and generates
    the SSH detection script for manual or automated recovery.

    For full SSH-based recovery, the calling session should:
    1. Call this to get the detection script
    2. Execute the script via SSH MCP
    3. Call with --apply and the SSH output to update state
    """
    from sibyl.experiment_recovery import (
        load_experiment_state, get_running_tasks,
        generate_detection_script, parse_detection_output,
        recover_from_detection, save_experiment_state,
        sync_to_gpu_progress, migrate_from_gpu_progress,
    )

    project_root = resolve_workspace_root(workspace_path)
    active_root = resolve_active_workspace_path(workspace_path)

    state = load_experiment_state(active_root)

    # Backward compat
    if not state.tasks:
        from sibyl.gpu_scheduler import _load_progress
        _, running_ids, _, _ = _load_progress(active_root)
        if running_ids:
            state = migrate_from_gpu_progress(active_root)
            save_experiment_state(active_root, state)

    running = get_running_tasks(state)
    if not running:
        print(json.dumps({"status": "no_recovery_needed", "total_tasks": len(state.tasks)}))
        return

    o = FarsOrchestrator(workspace_path)
    remote_dir = f"{o.config.remote_base}/projects/{o.ws.name}"
    script = generate_detection_script(remote_dir, running)

    print(json.dumps({
        "status": "has_running_tasks",
        "running_tasks": running,
        "detection_script": script,
        "ssh_server": o.config.ssh_server,
        "instructions": (
            "Execute detection_script via SSH on ssh_server, "
            "then call cli_apply_recovery(workspace_path, ssh_output) "
            "to apply the results."
        ),
    }, indent=2, ensure_ascii=False))
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliRecoverExperiments -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: add cli_recover_experiments CLI function"
git push
```

---

### Task 8: CLI — `cli_apply_recovery`

**Files:**
- Modify: `sibyl/orchestrate.py`
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

```python
class TestCliApplyRecovery:
    def test_apply_recovery_updates_state(self, make_orchestrator):
        from sibyl.orchestrate import cli_apply_recovery
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
        )
        from sibyl.gpu_scheduler import register_running_tasks
        import io, sys

        o = make_orchestrator(stage="pilot_experiments")
        state = ExperimentState()
        register_task(state, "task_a", gpu_ids=[0])
        register_task(state, "task_b", gpu_ids=[1])
        save_experiment_state(o.ws.active_root, state)
        register_running_tasks(o.ws.active_root, {"task_a": [0], "task_b": [1]})

        ssh_output = (
            'DONE:task_a:{"status":"success","summary":"ok"}\n'
            'RUNNING:task_b:{"epoch":50,"total_epochs":100}\n'
        )

        captured = io.StringIO()
        sys.stdout = captured
        cli_apply_recovery(str(o.ws.root), ssh_output)
        sys.stdout = sys.__stdout__
        result = json.loads(captured.getvalue())

        assert result["status"] == "recovered"
        assert "task_a" in result["recovered_completed"]
        assert "task_b" in result["still_running"]
        assert result["progress"]["task_b"]["epoch"] == 50
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliApplyRecovery -v`

**Step 3: Write minimal implementation**

Add to `sibyl/orchestrate.py`:

```python
def cli_apply_recovery(workspace_path: str, ssh_output: str):
    """CLI: Apply SSH detection output to recover experiment states.

    Takes the raw output from the detection script (run via SSH)
    and updates experiment_state.json + gpu_progress.json accordingly.
    """
    from sibyl.experiment_recovery import (
        load_experiment_state, save_experiment_state,
        parse_detection_output, recover_from_detection,
        sync_to_gpu_progress,
    )

    active_root = resolve_active_workspace_path(workspace_path)
    state = load_experiment_state(active_root)
    detection = parse_detection_output(ssh_output)
    result = recover_from_detection(state, detection)

    save_experiment_state(active_root, state)
    sync_to_gpu_progress(active_root, state)

    print(json.dumps({
        "status": "recovered",
        "recovered_completed": result.recovered_completed,
        "still_running": result.still_running,
        "recovered_failed": result.recovered_failed,
        "ssh_unreachable": result.ssh_unreachable,
        "needs_monitor": result.needs_monitor,
        "progress": result.progress,
    }, indent=2, ensure_ascii=False))
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliApplyRecovery -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: add cli_apply_recovery for SSH-based experiment recovery"
git push
```

---

### Task 9: Iteration Cleanup — Archive `experiment_state.json`

**Files:**
- Modify: `sibyl/orchestrate.py:1687-1693` — `_clear_iteration_artifacts` method
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

```python
class TestExperimentStateArchive:
    def test_iteration_cleanup_archives_experiment_state(self, make_orchestrator):
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
            load_experiment_state,
        )
        o = make_orchestrator(stage="quality_gate", iteration=1)
        state = ExperimentState()
        register_task(state, "a", gpu_ids=[0])
        state.tasks["a"]["status"] = "completed"
        save_experiment_state(o.ws.active_root, state)

        # Trigger cleanup (simulating quality_gate -> new iteration)
        o._clear_iteration_artifacts(1)

        # experiment_state.json should be gone from active root
        fresh = load_experiment_state(o.ws.active_root)
        assert fresh.tasks == {}

        # But archived version should exist
        archive = o.ws.active_root / "exp" / "history" / "experiment_state_iter_001.json"
        assert archive.exists()
        import json
        archived_data = json.loads(archive.read_text())
        assert "a" in archived_data["tasks"]
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateArchive -v`

**Step 3: Write minimal implementation**

In `sibyl/orchestrate.py`, in `_clear_iteration_artifacts` method, after the gpu_progress cleanup block (~line 1693), add:

```python
        # Archive experiment_state.json before clearing
        exp_state_path = self.ws.active_path("exp/experiment_state.json")
        if exp_state_path.exists():
            try:
                history_dir = self.ws.active_path("exp/history")
                history_dir.mkdir(parents=True, exist_ok=True)
                archive_name = f"experiment_state_iter_{iteration:03d}.json"
                shutil.copy2(exp_state_path, history_dir / archive_name)
                exp_state_path.unlink()
            except OSError:
                pass
```

Note: `import shutil` is already at the top of orchestrate.py (used by `_clear_iteration_artifacts`).

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestExperimentStateArchive -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: archive experiment_state.json on iteration cleanup"
git push
```

---

### Task 10: `_natural_next_stage` — Check `experiment_state` for Running Tasks

**Files:**
- Modify: `sibyl/orchestrate.py:1568-1581`
- Modify: `tests/test_orchestrate.py`

**Step 1: Write the failing test**

```python
class TestNaturalNextStageExperimentState:
    def test_stays_in_stage_when_experiment_state_has_running(self, make_orchestrator):
        """Even if gpu_progress is empty, experiment_state running keeps stage."""
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
        )
        o = make_orchestrator(stage="pilot_experiments", gpu_poll_enabled=False)
        tasks = [
            {"id": "a", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10},
        ]
        o.ws.write_file("plan/task_plan.json", json.dumps({"tasks": tasks}))

        # experiment_state says running, but gpu_progress is empty
        state = ExperimentState()
        register_task(state, "a", gpu_ids=[0])
        save_experiment_state(o.ws.active_root, state)

        # record_result should stay in pilot_experiments
        o.record_result("pilot_experiments")
        assert o.ws.get_status().stage == "pilot_experiments"
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestNaturalNextStageExperimentState -v`

**Step 3: Write minimal implementation**

In `sibyl/orchestrate.py`, in `_natural_next_stage`, at the experiment stages block (~line 1568), add an additional check:

```python
        # experiment stages: loop if more batches remain OR tasks still running
        if current_stage in ("pilot_experiments", "experiment_cycle"):
            from sibyl.gpu_scheduler import get_batch_info, get_running_gpu_ids
            from sibyl.experiment_recovery import (
                load_experiment_state, get_running_tasks,
            )
            exp_mode = "PILOT" if current_stage == "pilot_experiments" else "FULL"

            # Check experiment_state.json for running tasks (authoritative)
            exp_state = load_experiment_state(self.ws.active_root)
            exp_running = get_running_tasks(exp_state)
            if exp_running:
                return (current_stage, None)  # wait for running tasks

            # Also check gpu_progress (backward compat)
            running_gpus = get_running_gpu_ids(self.ws.active_root)
            if running_gpus:
                return (current_stage, None)  # wait for running tasks
            # ... rest of existing logic unchanged
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestNaturalNextStageExperimentState -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: check experiment_state in _natural_next_stage"
git push
```

---

### Task 11: Experimenter Prompt — PID File and Progress Reporting

**Files:**
- Modify: `sibyl/prompts/experimenter.md:69-93`
- Modify: `sibyl/prompts/server_experimenter.md:73-83`

**Step 1: Update `experimenter.md`**

In `sibyl/prompts/experimenter.md`, in the "完成标记与通知" section, add PID and progress requirements BEFORE the existing `mark_task_done` code:

```markdown
## 进程标识与进度上报（CRITICAL）

每个训练任务启动时**必须**写入 PID 文件，供系统恢复检测：

\```python
import os
from pathlib import Path

# 训练进程启动时立即写入
pid_file = Path(results_dir) / f"{task_id}.pid"
pid_file.write_text(str(os.getpid()))
\```

训练循环中**必须**每个 epoch 写入进度文件：

\```python
import json
from datetime import datetime
from pathlib import Path

def report_progress(task_id, results_dir, epoch, total_epochs, step=0,
                    total_steps=0, loss=None, metric=None):
    """Write progress file for system monitor to track."""
    progress = Path(results_dir) / f"{task_id}_PROGRESS.json"
    progress.write_text(json.dumps({
        "task_id": task_id,
        "epoch": epoch, "total_epochs": total_epochs,
        "step": step, "total_steps": total_steps,
        "loss": loss, "metric": metric or {},
        "updated_at": datetime.now().isoformat(),
    }))
\```

- PID 文件路径: `{remote_base}/projects/{project}/exp/results/{task_id}.pid`
- 进度文件路径: `{remote_base}/projects/{project}/exp/results/{task_id}_PROGRESS.json`
- **不写 PID 文件的任务在系统中断后无法被恢复检测**
- 进度文件每 epoch 覆写一次（非追加），系统监控读取最新状态
```

Also update `mark_task_done` to clean up PID file:

```python
def mark_task_done(task_id, results_dir, status="success", summary=""):
    """Write DONE marker file for system monitor to detect."""
    # Clean up PID file
    pid_file = Path(results_dir) / f"{task_id}.pid"
    if pid_file.exists():
        pid_file.unlink()
    # Write DONE marker
    marker = Path(results_dir) / f"{task_id}_DONE"
    # Merge final progress if available
    progress_file = Path(results_dir) / f"{task_id}_PROGRESS.json"
    final_progress = {}
    if progress_file.exists():
        try:
            final_progress = json.loads(progress_file.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    marker.write_text(json.dumps({
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "final_progress": final_progress,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }))
```

**Step 2: Update `server_experimenter.md`**

Add the same PID and progress requirements to the server experimenter prompt, adapted for the server context. In section "完成标记文件（CRITICAL）", add PID file writing before the DONE marker code.

**Step 3: Verify prompts load correctly**

Run: `.venv/bin/python3 -c "from sibyl.orchestrate import load_prompt; p = load_prompt('experimenter'); assert '.pid' in p; assert 'report_progress' in p; print('OK')"`
Expected: "OK"

**Step 4: Commit**

```bash
git add sibyl/prompts/experimenter.md sibyl/prompts/server_experimenter.md
git commit -m "feat: add PID file and progress reporting to experimenter prompts"
git push
```

---

### Task 12: Monitor Enhancement — Progress Reading

**Files:**
- Modify: `sibyl/gpu_scheduler.py:607-730` — `experiment_monitor_script` function
- Modify: `tests/test_experiment_recovery.py` (or new test)

**Step 1: Write the failing test**

```python
class TestMonitorProgressReading:
    def test_monitor_script_reads_progress(self):
        from sibyl.gpu_scheduler import experiment_monitor_script
        script = experiment_monitor_script(
            ssh_server="cs8000d",
            remote_project_dir="/home/user/project",
            task_ids=["task_a"],
        )
        assert "_PROGRESS.json" in script
        assert '"progress"' in script
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestMonitorProgressReading -v`
Expected: FAIL — current monitor script doesn't read _PROGRESS.json

**Step 3: Write minimal implementation**

In `experiment_monitor_script` in `sibyl/gpu_scheduler.py`, in the main loop where task status is checked, add progress reading. After checking for DONE, before writing to MARKER, add:

```bash
    # Read progress for pending tasks
    PROGRESS_JSON=""
    for task_id in "${ALL_TASKS[@]}"; do
        result=$(ssh {ssh_server} "test -f $REMOTE_DIR/exp/results/${task_id}_DONE && echo 'DONE' || echo 'PENDING'" 2>/dev/null)
        if [ "$result" != "DONE" ]; then
            prog=$(ssh {ssh_server} "cat $REMOTE_DIR/exp/results/${task_id}_PROGRESS.json 2>/dev/null || echo ''" 2>/dev/null)
            if [ -n "$prog" ]; then
                if [ -z "$PROGRESS_JSON" ]; then
                    PROGRESS_JSON="\"$task_id\": $prog"
                else
                    PROGRESS_JSON="$PROGRESS_JSON, \"$task_id\": $prog"
                fi
            fi
        fi
    done
```

And include the `"progress": {$PROGRESS_JSON}` field in the JSON written to the marker file.

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestMonitorProgressReading -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/gpu_scheduler.py tests/test_experiment_recovery.py
git commit -m "feat: add progress reading to experiment monitor script"
git push
```

---

### Task 13: `cli_experiment_status` Enhancement

**Files:**
- Modify: `sibyl/orchestrate.py:1838-1980` — `cli_experiment_status` function

**Step 1: Write the failing test**

```python
class TestCliExperimentStatusEnhanced:
    def test_status_shows_progress(self, make_orchestrator):
        from sibyl.orchestrate import cli_experiment_status
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
        )
        from sibyl.gpu_scheduler import register_running_tasks
        import io, sys

        o = make_orchestrator(stage="pilot_experiments")
        tasks = [{"id": "a", "depends_on": [], "gpu_count": 1, "estimated_minutes": 10}]
        o.ws.write_file("plan/task_plan.json", json.dumps({"tasks": tasks}))

        state = ExperimentState()
        register_task(state, "a", gpu_ids=[0])
        state.tasks["a"]["progress"] = {"epoch": 50, "total_epochs": 100, "loss": 0.3}
        save_experiment_state(o.ws.active_root, state)
        register_running_tasks(o.ws.active_root, {"a": [0]})

        captured = io.StringIO()
        sys.stdout = captured
        cli_experiment_status(str(o.ws.root))
        sys.stdout = sys.__stdout__
        result = json.loads(captured.getvalue())
        assert "task_progress" in result
        assert result["task_progress"]["a"]["epoch"] == 50
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliExperimentStatusEnhanced -v`

**Step 3: Write minimal implementation**

In `cli_experiment_status`, after loading `_load_progress` data, add:

```python
    # Load experiment state for progress info
    from sibyl.experiment_recovery import load_experiment_state
    exp_state = load_experiment_state(active_root)
    task_progress = {}
    for tid, task in exp_state.tasks.items():
        if task.get("progress"):
            task_progress[tid] = task["progress"]
    result["task_progress"] = task_progress
    if exp_state.last_recovery_at:
        result["last_recovery_at"] = exp_state.last_recovery_at
```

Also update the display string to show progress for running tasks.

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCliExperimentStatusEnhanced -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: enhance cli_experiment_status with progress and recovery info"
git push
```

---

### Task 14: `_reset_experiment_runtime_state` — Also Clear `experiment_state`

**Files:**
- Modify: `sibyl/orchestrate.py:1713-1719`

**Step 1: Write the failing test**

```python
    def test_reset_experiment_runtime_clears_experiment_state(self, make_orchestrator):
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
            load_experiment_state,
        )
        o = make_orchestrator(stage="pilot_experiments")
        state = ExperimentState()
        register_task(state, "a", gpu_ids=[0])
        save_experiment_state(o.ws.active_root, state)

        o._reset_experiment_runtime_state()
        fresh = load_experiment_state(o.ws.active_root)
        assert fresh.tasks == {}
```

**Step 2: Run test, implement, verify**

In `_reset_experiment_runtime_state`, add after the gpu_progress cleanup:

```python
        exp_state_path = self.ws.active_path("exp/experiment_state.json")
        if exp_state_path.exists():
            try:
                exp_state_path.unlink()
            except OSError:
                pass
```

**Step 3: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: clear experiment_state in runtime reset"
git push
```

---

### Task 15: Full Integration Test — End-to-End Recovery

**Files:**
- Modify: `tests/test_experiment_recovery.py`

**Step 1: Write the integration test**

```python
class TestEndToEndRecovery:
    """Full pipeline: register → simulate interrupt → recover."""

    def test_full_recovery_pipeline(self, tmp_path):
        from sibyl.experiment_recovery import (
            ExperimentState, register_task, save_experiment_state,
            load_experiment_state, generate_detection_script,
            parse_detection_output, recover_from_detection,
            sync_to_gpu_progress,
        )
        from sibyl.gpu_scheduler import register_running_tasks, _load_progress

        # Phase 1: Register tasks (simulating orchestrator dispatch)
        state = ExperimentState()
        register_task(state, "train_baseline", gpu_ids=[0, 1], pid_file="/tmp/train_baseline.pid")
        register_task(state, "train_ablation", gpu_ids=[2], pid_file="/tmp/train_ablation.pid")
        register_task(state, "train_extra", gpu_ids=[3], pid_file="/tmp/train_extra.pid")
        save_experiment_state(tmp_path, state)
        register_running_tasks(tmp_path, {
            "train_baseline": [0, 1],
            "train_ablation": [2],
            "train_extra": [3],
        })

        # Phase 2: Generate detection script
        script = generate_detection_script("/home/user/project", [
            "train_baseline", "train_ablation", "train_extra",
        ])
        assert "train_baseline" in script

        # Phase 3: Simulate SSH output (as if script ran on server)
        ssh_output = (
            'DONE:train_baseline:{"status":"success","summary":"loss=0.1"}\n'
            'RUNNING:train_ablation:{"epoch":75,"total_epochs":100,"loss":0.25}\n'
            'DEAD:train_extra:99999\n'
        )

        # Phase 4: Parse and recover
        detection = parse_detection_output(ssh_output)
        state = load_experiment_state(tmp_path)
        result = recover_from_detection(state, detection)

        assert result.recovered_completed == ["train_baseline"]
        assert result.still_running == ["train_ablation"]
        assert result.recovered_failed == ["train_extra"]
        assert result.needs_monitor is True
        assert result.progress["train_ablation"]["epoch"] == 75

        # Phase 5: Sync to gpu_progress
        save_experiment_state(tmp_path, state)
        sync_to_gpu_progress(tmp_path, state)

        completed, running_ids, _, _ = _load_progress(tmp_path)
        assert "train_baseline" in completed
        assert "train_ablation" in running_ids
        assert "train_extra" not in running_ids

        # Phase 6: Verify state file
        final = load_experiment_state(tmp_path)
        assert final.tasks["train_baseline"]["status"] == "completed"
        assert final.tasks["train_ablation"]["status"] == "running"
        assert final.tasks["train_extra"]["status"] == "failed"
        assert "process_disappeared" in final.tasks["train_extra"]["error_summary"]
        assert len(final.recovery_log) == 1
```

**Step 2: Run test to verify it passes** (all code already implemented)

Run: `.venv/bin/python3 -m pytest tests/test_experiment_recovery.py::TestEndToEndRecovery -v`
Expected: PASS

**Step 3: Run full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add tests/test_experiment_recovery.py
git commit -m "test: add end-to-end experiment recovery integration test"
git push
```

---

### Task 16: Update CLAUDE.md and Memory

**Files:**
- Modify: `CLAUDE.md` — add experiment state tracking section
- Modify: `/Users/cwan0785/.claude/projects/-Users-cwan0785-sibyl-system/memory/MEMORY.md`

**Step 1: Update CLAUDE.md**

Add to the "GPU 轮询" section or create a new section:

```markdown
### 实验状态追踪与恢复（`experiment_state.json`）
- `exp/experiment_state.json` 是实验任务生命周期的权威源
- 每个任务: pending → running → completed/failed
- Experimenter 写入: `.pid`（进程标识）、`_PROGRESS.json`（进度）、`_DONE`（完成）
- 恢复检测: SSH 批量脚本检查 DONE/PID/进程存活
- `cli_recover_experiments()`: 生成检测脚本
- `cli_apply_recovery()`: 应用 SSH 检测结果
- 迭代清理时归档到 `exp/history/experiment_state_iter_NNN.json`
```

**Step 2: Update Memory**

Add experiment state tracking section to MEMORY.md.

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add experiment state tracking documentation"
git push
```
