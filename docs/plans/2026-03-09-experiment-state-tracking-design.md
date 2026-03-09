# Experiment State Tracking & Recovery Design

**Date:** 2026-03-09
**Status:** Approved

## Problem

When a Sibyl research project is interrupted (session crash, SSH disconnect, GPU OOM, or user pause), experiment progress is lost. The current `gpu_progress.json` tracks completed/running task IDs but lacks:
- Reliable recovery detection (stale `running` entries after crash)
- Remote process liveness checking
- Task-level progress tracking (epoch, loss, etc.)
- Graceful re-attachment to still-running experiments

## Design: Independent Experiment State File (Plan B)

### Approach

Introduce `exp/experiment_state.json` as the authoritative source of truth for experiment lifecycle, separate from `gpu_progress.json` (scheduling) and `experiment_db.jsonl` (history). A new `sibyl/experiment_recovery.py` module handles all recovery logic.

## Data Model

### `exp/experiment_state.json`

```json
{
  "schema_version": 1,
  "tasks": {
    "train_baseline": {
      "status": "running",
      "gpu_ids": [0, 1],
      "remote_pid": 12345,
      "pid_file": "/home/user/sibyl_system/projects/ttt-dlm/exp/results/train_baseline.pid",
      "started_at": "2026-03-09T14:00:00",
      "completed_at": null,
      "exit_code": null,
      "error_summary": null,
      "progress": {
        "epoch": 50,
        "total_epochs": 100,
        "step": 5000,
        "loss": 0.32,
        "updated_at": "2026-03-09T15:30:00"
      }
    }
  },
  "last_recovery_at": null,
  "recovery_log": []
}
```

### Remote Files (written by experimenter)

| File | Purpose | Timing |
|---|---|---|
| `{task_id}.pid` | PID file | Training process start |
| `{task_id}_PROGRESS.json` | Real-time progress | Every epoch / N steps (overwrite) |
| `{task_id}_DONE` | Completion marker (existing) | Task end |

### `_PROGRESS.json` Format

```json
{
  "task_id": "train_baseline",
  "epoch": 50, "total_epochs": 100,
  "step": 5000, "total_steps": 10000,
  "loss": 0.32,
  "metric": {"accuracy": 0.85},
  "updated_at": "2026-03-09T15:30:00"
}
```

### `experiment_db.jsonl` Append Format

```json
{"task_id": "train_baseline", "type": "progress", "epoch": 50, "loss": 0.32, "timestamp": "..."}
{"task_id": "train_baseline", "type": "completed", "status": "success", "final_metric": {...}, "timestamp": "..."}
```

## Recovery Logic

### New Module: `sibyl/experiment_recovery.py`

Core function: `recover_experiments(workspace_root, ssh_server, remote_project_dir) -> RecoveryResult`

#### Recovery Flow

1. Read `exp/experiment_state.json`, find all `status=="running"` tasks
2. If no running tasks -> return `no_recovery_needed`
3. Single SSH call with batch detection script for all running tasks:
   - Check `{task_id}_DONE` exists -> mark completed/failed (per DONE status field)
   - No DONE -> read PID file, `kill -0 $PID` -> process alive: keep running, read `_PROGRESS.json`
   - No DONE, process dead -> mark failed (`process_disappeared`)
   - SSH unreachable -> keep running, log `ssh_unreachable`
4. Update `experiment_state.json`
5. Sync `gpu_progress.json` (keep scheduler consistent)
6. Return `RecoveryResult`

#### SSH Batch Detection Script

Single SSH call to minimize connection overhead:

```bash
for task_id in train_baseline train_ablation; do
  if [ -f "exp/results/${task_id}_DONE" ]; then
    echo "DONE:${task_id}:$(cat exp/results/${task_id}_DONE)"
  elif [ -f "exp/results/${task_id}.pid" ]; then
    pid=$(cat "exp/results/${task_id}.pid")
    if kill -0 "$pid" 2>/dev/null; then
      progress=$(cat "exp/results/${task_id}_PROGRESS.json" 2>/dev/null || echo '{}')
      echo "RUNNING:${task_id}:${progress}"
    else
      echo "DEAD:${task_id}:${pid}"
    fi
  else
    echo "UNKNOWN:${task_id}"
  fi
done
```

#### RecoveryResult

```python
@dataclass
class RecoveryResult:
    recovered_completed: list[str]
    still_running: list[str]
    recovered_failed: list[str]
    ssh_unreachable: bool
    needs_monitor: bool  # True if still_running is non-empty
    progress: dict[str, dict]  # task_id -> progress info
```

## Integration Points

### Trigger Points

| Trigger | How |
|---|---|
| `cli_next()` enters experiment stage | `_action_experiment_batch` auto-calls recovery |
| `cli_resume()` resumes paused project | Resume flow auto-calls recovery |
| `cli_recover_experiments()` | User manually invokes |

### `_action_experiment_batch` Entry Logic

```
if experiment_state.json exists with running tasks:
    result = recover_experiments()
    if still_running:
        rebuild monitor, return monitor/wait action
    if recovered_failed:
        tasks return to scheduling queue (remove from gpu_progress)
    if all completed:
        check for remaining tasks, continue normal flow
```

### `_natural_next_stage` Adjustment

- Check `experiment_state.json` running tasks in addition to `get_running_gpu_ids()`
- Prevents losing running state if `gpu_progress.json` is accidentally cleared

## State Synchronization

### Three-Layer Data Relationship

```
experiment_state.json  <->  gpu_progress.json  <->  Remote Files
   (lifecycle mgmt)          (scheduler state)       (actual runtime)
```

### Write Responsibility

| Event | experiment_state.json | gpu_progress.json | Remote |
|---|---|---|---|
| Task dispatched | orchestrator: running | register_running_tasks | experimenter: .pid |
| Training loop | monitor syncs progress | unchanged | experimenter: _PROGRESS.json |
| Task complete | monitor/recovery: completed | unregister_running_task | experimenter: _DONE |
| Recovery | recovery module updates | recovery module syncs | read-only |

### Consistency Rules

- `experiment_state.json` is the **authority** (source of truth)
- `gpu_progress.json` is the **scheduling view**, derived from experiment_state
- Recovery trusts remote files over local state

### Conflict Resolution

- experiment_state=running, gpu_progress missing -> backfill gpu_progress
- gpu_progress=running, experiment_state missing -> migrate, create experiment_state record
- Both say running but remote DONE exists -> trust remote, update both to completed

### Iteration Cleanup

- `gpu_progress.json`: delete as before (scheduling reset)
- `experiment_state.json`: **archive** to `exp/history/experiment_state_iter_{N}.json`

## Experimenter Prompt Changes

### PID File (new requirement)

```python
import os
from pathlib import Path
pid_file = Path(results_dir) / f"{task_id}.pid"
pid_file.write_text(str(os.getpid()))
```

### Progress Reporting (new requirement)

```python
def report_progress(task_id, results_dir, epoch, total_epochs, step=0,
                    total_steps=0, loss=None, metric=None):
    progress = Path(results_dir) / f"{task_id}_PROGRESS.json"
    progress.write_text(json.dumps({
        "task_id": task_id,
        "epoch": epoch, "total_epochs": total_epochs,
        "step": step, "total_steps": total_steps,
        "loss": loss, "metric": metric or {},
        "updated_at": datetime.now().isoformat(),
    }))
```

### `mark_task_done` Enhancement

- Clean up PID file (delete `{task_id}.pid`)
- Merge final `_PROGRESS.json` content into DONE marker

## CLI Interface

### New: `cli_recover_experiments(workspace_path) -> JSON`

```json
{
  "status": "recovered",
  "recovered_completed": ["task_a"],
  "still_running": ["task_b"],
  "recovered_failed": ["task_c"],
  "ssh_unreachable": false,
  "progress": {
    "task_b": {"epoch": 75, "total_epochs": 100, "loss": 0.28}
  }
}
```

### Enhanced: `cli_experiment_status`

- Add per-task progress from experiment_state.json
- Show last recovery time
- Show recovery history

## Files Changed

| Change | File | ~Lines |
|---|---|---|
| **New** | `sibyl/experiment_recovery.py` | ~200 |
| **New** | `tests/test_experiment_recovery.py` | ~15 tests |
| **Modify** | `sibyl/orchestrate.py` | Integration + CLI |
| **Modify** | `sibyl/gpu_scheduler.py` | Monitor progress reading |
| **Modify** | `sibyl/workspace.py` | Archive experiment_state |
| **Modify** | `sibyl/prompts/experimenter.md` | PID + PROGRESS |
| **Modify** | `sibyl/prompts/server_experimenter.md` | PID + PROGRESS |
| **Extend** | `tests/test_orchestrate.py` | Recovery integration tests |

## Test Strategy

### Unit Tests (~10)

- Recovery: DONE exists -> completed
- Recovery: PID alive -> still_running + progress
- Recovery: PID dead -> failed
- Recovery: SSH unreachable -> keep running
- State sync: experiment_state <-> gpu_progress consistency
- Migration: old project without experiment_state.json
- Archive: iteration cleanup preserves history

### Integration Tests (~5)

- `_action_experiment_batch` auto-recovery trigger
- `cli_recover_experiments` end-to-end
- `_natural_next_stage` consistency with experiment_state
- Monitor progress field in output JSON
- SSH mock: batch detection script parse
