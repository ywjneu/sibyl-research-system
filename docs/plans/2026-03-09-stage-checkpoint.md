# Stage Checkpoint System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add sub-step checkpoint tracking to parallel/sequential stages so interrupted stages can resume from the last completed sub-step instead of restarting entirely.

**Architecture:** Checkpoint files (`.checkpoint.json`) stored per-stage directory, validated by file mtime + size to prevent stale data pollution. Orchestrator filters remaining steps into action; `cli_checkpoint()` API for execution layer to report completion.

**Tech Stack:** Python 3.12, existing `sibyl/workspace.py` + `sibyl/orchestrate.py`

---

### Task 1: Checkpoint data model in workspace.py

**Files:**
- Modify: `sibyl/workspace.py`
- Test: `tests/test_workspace_checkpoint.py`

**Step 1: Write failing tests**

```python
# tests/test_workspace_checkpoint.py
"""Tests for workspace checkpoint functionality."""
import json
import time
from pathlib import Path
import pytest
from sibyl.workspace import Workspace, CheckpointStep


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, "test-project")


class TestCheckpointCreation:
    def test_create_checkpoint(self, ws):
        steps = {"intro": "writing/sections/intro.md", "method": "writing/sections/method.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        cp = ws.load_checkpoint("writing/sections")
        assert cp is not None
        assert cp["stage"] == "writing_sections"
        assert cp["iteration"] == 1
        assert len(cp["steps"]) == 2
        assert cp["steps"]["intro"]["status"] == "pending"
        assert cp["steps"]["method"]["status"] == "pending"

    def test_complete_step(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        # Create the actual file
        ws.write_file("writing/sections/intro.md", "# Introduction\n" * 100)
        ws.complete_checkpoint_step("writing/sections", "intro")
        cp = ws.load_checkpoint("writing/sections")
        assert cp["steps"]["intro"]["status"] == "completed"
        assert cp["steps"]["intro"]["file_size"] > 0
        assert cp["steps"]["intro"]["file_mtime"] > 0


class TestCheckpointValidation:
    def test_valid_completed_step(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "content")
        ws.complete_checkpoint_step("writing/sections", "intro")
        valid = ws.validate_checkpoint("writing/sections")
        assert valid["completed"] == ["intro"]
        assert valid["remaining"] == []

    def test_missing_file_invalidates_step(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        # Mark complete without creating file
        cp = ws.load_checkpoint("writing/sections")
        cp["steps"]["intro"]["status"] = "completed"
        cp["steps"]["intro"]["file_size"] = 100
        cp["steps"]["intro"]["file_mtime"] = time.time()
        ws.write_json("writing/sections/.checkpoint.json", cp)
        valid = ws.validate_checkpoint("writing/sections")
        assert valid["completed"] == []
        assert valid["remaining"] == ["intro"]

    def test_stale_file_invalidates_step(self, ws):
        """File from previous iteration (mtime < stage_started_at) is invalid."""
        ws.write_file("writing/sections/intro.md", "old content")
        time.sleep(0.05)  # ensure mtime difference
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=2)
        # Manually mark as completed with old mtime
        cp = ws.load_checkpoint("writing/sections")
        cp["steps"]["intro"]["status"] = "completed"
        cp["steps"]["intro"]["file_size"] = len("old content")
        cp["steps"]["intro"]["file_mtime"] = (ws.root / "writing/sections/intro.md").stat().st_mtime
        ws.write_json("writing/sections/.checkpoint.json", cp)
        valid = ws.validate_checkpoint("writing/sections")
        assert valid["completed"] == []
        assert valid["remaining"] == ["intro"]

    def test_size_mismatch_invalidates_step(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "short")
        ws.complete_checkpoint_step("writing/sections", "intro")
        # Tamper: overwrite with different content (simulating partial write)
        ws.write_file("writing/sections/intro.md", "x")
        valid = ws.validate_checkpoint("writing/sections")
        assert valid["completed"] == []
        assert valid["remaining"] == ["intro"]

    def test_no_checkpoint_returns_none(self, ws):
        valid = ws.validate_checkpoint("writing/sections")
        assert valid is None


class TestCheckpointLifecycle:
    def test_clear_checkpoint(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.clear_checkpoint("writing/sections")
        assert ws.load_checkpoint("writing/sections") is None

    def test_iteration_mismatch_ignored(self, ws):
        """Checkpoint from different iteration is treated as stale."""
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        valid = ws.validate_checkpoint("writing/sections", current_iteration=2)
        assert valid is None  # stale checkpoint

    def test_has_checkpoint_flag(self, ws):
        assert ws.has_checkpoint("writing/sections") is False
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        assert ws.has_checkpoint("writing/sections") is True
```

**Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_workspace_checkpoint.py -v`
Expected: FAIL (CheckpointStep, create_checkpoint, etc. not defined)

**Step 3: Implement checkpoint methods in workspace.py**

Add to `sibyl/workspace.py`:

```python
# After WorkspaceStatus class, add:

def _checkpoint_path(self, checkpoint_dir: str) -> Path:
    return self._check_path(f"{checkpoint_dir}/.checkpoint.json")

def create_checkpoint(self, stage: str, checkpoint_dir: str,
                      steps: dict[str, str], iteration: int):
    """Create a new checkpoint for a stage with sub-steps.

    Args:
        stage: Pipeline stage name
        checkpoint_dir: Relative dir for .checkpoint.json (e.g. "writing/sections")
        steps: {step_id: relative_file_path} mapping
        iteration: Current iteration number
    """
    cp = {
        "version": 1,
        "stage": stage,
        "iteration": iteration,
        "stage_started_at": time.time(),
        "steps": {
            step_id: {
                "status": "pending",
                "file": file_path,
                "completed_at": 0.0,
                "file_mtime": 0.0,
                "file_size": 0,
            }
            for step_id, file_path in steps.items()
        },
    }
    self.write_json(f"{checkpoint_dir}/.checkpoint.json", cp)

def load_checkpoint(self, checkpoint_dir: str) -> dict | None:
    return self.read_json(f"{checkpoint_dir}/.checkpoint.json")

def complete_checkpoint_step(self, checkpoint_dir: str, step_id: str):
    """Mark a sub-step as completed with file validation data."""
    cp = self.load_checkpoint(checkpoint_dir)
    if cp is None or step_id not in cp["steps"]:
        return
    step = cp["steps"][step_id]
    file_path = self._check_path(step["file"])
    if not file_path.exists():
        return  # Don't mark complete if file doesn't exist
    stat = file_path.stat()
    step["status"] = "completed"
    step["completed_at"] = time.time()
    step["file_mtime"] = stat.st_mtime
    step["file_size"] = stat.st_size
    self.write_json(f"{checkpoint_dir}/.checkpoint.json", cp)

def validate_checkpoint(self, checkpoint_dir: str,
                        current_iteration: int | None = None) -> dict | None:
    """Validate checkpoint, return {completed: [...], remaining: [...]}.

    Returns None if no checkpoint or iteration mismatch (stale).
    A step is valid only if:
      1. status == "completed"
      2. Target file exists
      3. file mtime >= stage_started_at
      4. file size matches recorded size and > 0
    """
    cp = self.load_checkpoint(checkpoint_dir)
    if cp is None:
        return None
    if current_iteration is not None and cp["iteration"] != current_iteration:
        return None

    completed = []
    remaining = []
    started_at = cp["stage_started_at"]

    for step_id, step in cp["steps"].items():
        if step["status"] != "completed":
            remaining.append(step_id)
            continue
        # Validate file
        file_path = self._check_path(step["file"])
        if not file_path.exists():
            remaining.append(step_id)
            continue
        stat = file_path.stat()
        if stat.st_mtime < started_at:
            remaining.append(step_id)
            continue
        if stat.st_size == 0 or stat.st_size != step["file_size"]:
            remaining.append(step_id)
            continue
        completed.append(step_id)

    return {"completed": completed, "remaining": remaining}

def clear_checkpoint(self, checkpoint_dir: str):
    path = self._checkpoint_path(checkpoint_dir)
    if path.exists():
        path.unlink()

def has_checkpoint(self, checkpoint_dir: str) -> bool:
    return self._checkpoint_path(checkpoint_dir).exists()
```

**Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_workspace_checkpoint.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sibyl/workspace.py tests/test_workspace_checkpoint.py
git commit -m "feat: add checkpoint tracking to workspace"
git push
```

---

### Task 2: Orchestrator checkpoint integration

**Files:**
- Modify: `sibyl/orchestrate.py`
- Test: `tests/test_orchestrate.py` (add checkpoint tests)

**Step 1: Write failing tests**

Add to `tests/test_orchestrate.py`:

```python
class TestCheckpointIntegration:
    """Test checkpoint-aware action generation."""

    def test_writing_sections_parallel_creates_checkpoint(self, make_orchestrator):
        o = make_orchestrator(stage="writing_sections")
        o.config.writing_mode = "parallel"
        action = o.get_next_action()
        assert action["action_type"] == "team"
        # Checkpoint should be created
        cp = o.ws.load_checkpoint("writing/sections")
        assert cp is not None
        assert len(cp["steps"]) == 6

    def test_writing_sections_resumes_from_checkpoint(self, make_orchestrator):
        o = make_orchestrator(stage="writing_sections")
        o.config.writing_mode = "parallel"
        iteration = o.ws.get_status().iteration
        # Create checkpoint with 3 completed
        steps = {sid: f"writing/sections/{sid}.md" for sid, _ in PAPER_SECTIONS}
        o.ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=iteration)
        for sid in ["intro", "related_work", "method"]:
            o.ws.write_file(f"writing/sections/{sid}.md", f"# {sid}\n" * 50)
            o.ws.complete_checkpoint_step("writing/sections", sid)
        action = o.get_next_action()
        assert action["action_type"] == "team"
        assert action.get("checkpoint_info") is not None
        assert set(action["checkpoint_info"]["completed_steps"]) == {"intro", "related_work", "method"}
        assert len(action["checkpoint_info"]["remaining_steps"]) == 3

    def test_writing_critique_creates_checkpoint(self, make_orchestrator):
        o = make_orchestrator(stage="writing_critique")
        action = o.get_next_action()
        assert action["action_type"] == "team"
        cp = o.ws.load_checkpoint("writing/critique")
        assert cp is not None
        assert len(cp["steps"]) == 6

    def test_all_steps_complete_skips_stage(self, make_orchestrator):
        """If all checkpoint steps valid, action indicates stage is complete."""
        o = make_orchestrator(stage="writing_sections")
        o.config.writing_mode = "parallel"
        iteration = o.ws.get_status().iteration
        steps = {sid: f"writing/sections/{sid}.md" for sid, _ in PAPER_SECTIONS}
        o.ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=iteration)
        for sid, _ in PAPER_SECTIONS:
            o.ws.write_file(f"writing/sections/{sid}.md", f"# {sid}\n" * 50)
            o.ws.complete_checkpoint_step("writing/sections", sid)
        action = o.get_next_action()
        assert action.get("checkpoint_info", {}).get("all_complete") is True

    def test_clear_iteration_artifacts_clears_checkpoints(self, make_orchestrator):
        o = make_orchestrator(stage="writing_sections")
        steps = {"intro": "writing/sections/intro.md"}
        o.ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        o._clear_iteration_artifacts()
        assert o.ws.has_checkpoint("writing/sections") is False
```

**Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py::TestCheckpointIntegration -v`
Expected: FAIL

**Step 3: Implement checkpoint logic in orchestrator**

Changes to `sibyl/orchestrate.py`:

1. Add `checkpoint_info` field to `Action` dataclass
2. Add checkpoint-aware stage config constant `CHECKPOINT_STAGES`
3. Modify `_action_writing_sections`, `_action_writing_critique`, `_action_idea_debate`, `_action_result_debate`
4. Add `cli_checkpoint()` CLI function
5. Update `_clear_iteration_artifacts` to clear checkpoint files

**Step 4: Run all tests**

Run: `.venv/bin/python3 -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: integrate checkpoint tracking into orchestrator actions"
git push
```

---

### Task 3: cli_checkpoint API

**Files:**
- Modify: `sibyl/orchestrate.py`
- Test: `tests/test_orchestrate.py`

**Step 1: Write failing test**

```python
class TestCliCheckpoint:
    def test_cli_checkpoint_marks_step(self, make_orchestrator, capsys):
        o = make_orchestrator(stage="writing_sections")
        steps = {"intro": "writing/sections/intro.md"}
        o.ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        o.ws.write_file("writing/sections/intro.md", "content here")
        cli_checkpoint(str(o.ws.root), "writing_sections", "intro")
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["status"] == "ok"
        cp = o.ws.load_checkpoint("writing/sections")
        assert cp["steps"]["intro"]["status"] == "completed"
```

**Step 2: Implement cli_checkpoint**

```python
# Add to CLI helpers section of orchestrate.py

# Mapping: stage -> checkpoint directory
CHECKPOINT_DIRS = {
    "idea_debate": "idea",
    "result_debate": "idea/result_debate",
    "writing_sections": "writing/sections",
    "writing_critique": "writing/critique",
}

def cli_checkpoint(workspace_path: str, stage: str, step_id: str):
    """CLI: Mark a checkpoint sub-step as completed."""
    o = FarsOrchestrator(workspace_path)
    cp_dir = CHECKPOINT_DIRS.get(stage)
    if cp_dir is None:
        print(json.dumps({"status": "error", "message": f"No checkpoint support for stage '{stage}'"}))
        return
    o.ws.complete_checkpoint_step(cp_dir, step_id)
    print(json.dumps({"status": "ok", "stage": stage, "step": step_id}))
```

**Step 3: Run tests**

Run: `.venv/bin/python3 -m pytest tests/test_orchestrate.py -v -k "checkpoint"`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add sibyl/orchestrate.py tests/test_orchestrate.py
git commit -m "feat: add cli_checkpoint API for sub-step progress reporting"
git push
```

---

### Task 4: Update _clear_iteration_artifacts

**Files:**
- Modify: `sibyl/orchestrate.py`

**Step 1: Add checkpoint cleanup to _clear_iteration_artifacts**

In the existing method, after clearing directories, also explicitly remove any `.checkpoint.json` files that survived (since dirs_to_clear uses rmtree which already handles this, but add safety for checkpoint dirs not in the clear list):

```python
# Add after existing cleanup in _clear_iteration_artifacts:
# Clear checkpoint files
for cp_dir in CHECKPOINT_DIRS.values():
    self.ws.clear_checkpoint(cp_dir)
```

**Step 2: Run existing + new tests**

Run: `.venv/bin/python3 -m pytest tests/ -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add sibyl/orchestrate.py
git commit -m "fix: clear checkpoint files during iteration artifact cleanup"
git push
```

---

### Task 5: Update plugin command documentation

**Files:**
- Modify: `plugin/commands/start.md`

**Step 1: Add checkpoint protocol to orchestration loop docs**

Add to the stage execution section:

```markdown
### Checkpoint 协议（子步骤恢复）

部分 stage（writing_sections, writing_critique, idea_debate, result_debate）支持子步骤 checkpoint。

**执行时**：
- `cli_next()` 返回的 action 若包含 `checkpoint_info`，表示该 stage 支持 checkpoint
- `checkpoint_info.remaining_steps` 列出需要执行的子步骤
- `checkpoint_info.completed_steps` 列出已完成的子步骤（可作为上下文参考）
- 如果 `checkpoint_info.all_complete == true`，直接 `cli_record()` 推进

**每个子步骤完成后**：
```bash
.venv/bin/python3 -c "from sibyl.orchestrate import cli_checkpoint; cli_checkpoint('{ws}', '{stage}', '{step_id}')"
```

**恢复机制**：中断后重新 `cli_next()` 会自动检测 checkpoint，只返回未完成的子步骤。
```

**Step 2: Commit**

```bash
git add plugin/commands/start.md
git commit -m "docs: add checkpoint protocol to orchestration loop"
git push
```
