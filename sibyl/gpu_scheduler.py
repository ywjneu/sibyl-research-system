"""GPU-aware task scheduler for experiment parallelization.

Reads task_plan.json for task definitions and depends_on graph,
tracks progress in exp/gpu_progress.json, and assigns GPU subsets
to independent tasks for parallel execution.

Task plan format:
    {
        "tasks": [
            {
                "id": "train_baseline",
                "depends_on": [],
                "gpu_count": 2,           // optional, default 1
                "estimated_minutes": 60   // optional, default 10
            },
            ...
        ]
    }
"""
import json
from collections import deque
from pathlib import Path


def topo_sort_layers(tasks: list[dict]) -> list[list[dict]]:
    """BFS topological sort, grouping tasks by dependency layer.

    Each layer contains tasks whose dependencies are all in earlier layers.
    Returns list of layers, each layer is a list of task dicts.
    """
    if not tasks:
        return []

    task_map = {t["id"]: t for t in tasks}
    in_degree = {t["id"]: 0 for t in tasks}
    children = {t["id"]: [] for t in tasks}

    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep in task_map:
                in_degree[t["id"]] += 1
                children[dep].append(t["id"])

    layers = []
    queue = deque([tid for tid, deg in in_degree.items() if deg == 0])

    while queue:
        layer = list(queue)
        queue.clear()
        layers.append([task_map[tid] for tid in layer])
        for tid in layer:
            for child in children[tid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

    return layers


def assign_gpus(ready_tasks: list[dict], gpu_ids: list[int],
                default_gpus_per_task: int = 1) -> list[dict]:
    """Assign GPU subsets to ready tasks based on per-task gpu_count.

    Each task can declare its own gpu_count in the task dict. Falls back
    to default_gpus_per_task if not specified.

    Returns list of assignments:
        [{"task_ids": ["task_0a"], "gpu_ids": [0, 1]}, ...]

    Greedy allocation: assigns tasks in order until GPUs are exhausted.
    """
    if not ready_tasks or not gpu_ids:
        return []

    available = list(gpu_ids)
    assignments = []

    for task in ready_tasks:
        needed = task.get("gpu_count", default_gpus_per_task)
        needed = max(1, needed)  # at least 1 GPU

        if needed > len(available):
            break  # not enough GPUs for this task

        assigned = available[:needed]
        available = available[needed:]
        assignments.append({
            "task_ids": [task["id"]],
            "gpu_ids": assigned,
        })

        if not available:
            break

    # Edge case: no task could be assigned (first task needs more GPUs than total)
    if not assignments and ready_tasks:
        needed = ready_tasks[0].get("gpu_count", default_gpus_per_task)
        if needed > len(gpu_ids):
            # Give all GPUs to the first task anyway
            assignments = [{"task_ids": [ready_tasks[0]["id"]], "gpu_ids": list(gpu_ids)}]

    return assignments


def estimate_batch_minutes(batch: list[dict], tasks: list[dict],
                           default_minutes: int = 10) -> int:
    """Estimate how long a batch will take (max of task estimates).

    Each task can declare estimated_minutes. The batch duration is the max
    across all tasks (since they run in parallel).
    """
    if not batch:
        return default_minutes

    task_map = {t["id"]: t for t in tasks}
    max_est = default_minutes

    for assignment in batch:
        for tid in assignment["task_ids"]:
            task = task_map.get(tid, {})
            est = task.get("estimated_minutes", default_minutes)
            if est > max_est:
                max_est = est

    return max_est


def get_next_batch(workspace_root: Path, gpu_ids: list[int], mode: str = "PILOT",
                   gpus_per_task: int = 1) -> list[dict] | None:
    """Get the next batch of experiment tasks to execute.

    Args:
        workspace_root: Path to workspace directory
        gpu_ids: Available GPU IDs
        mode: "PILOT" or "FULL"
        gpus_per_task: Default GPUs per task (overridden by task-level gpu_count)

    Returns:
        None: No task_plan.json or no tasks array → fallback to single-agent
        []: Tasks exist but all blocked by dependencies
        [assignments]: Next batch of task-GPU assignments
    """
    task_plan_path = workspace_root / "plan" / "task_plan.json"
    if not task_plan_path.exists():
        return None

    try:
        with open(task_plan_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    tasks = plan.get("tasks")
    if not tasks or not isinstance(tasks, list):
        return None

    # Load progress
    progress_path = workspace_root / "exp" / "gpu_progress.json"
    completed = set()
    if progress_path.exists():
        try:
            with open(progress_path, encoding="utf-8") as f:
                progress = json.load(f)
            completed = set(progress.get("completed", []))
        except (json.JSONDecodeError, OSError):
            pass

    # Filter out completed tasks
    remaining = [t for t in tasks if t["id"] not in completed]
    if not remaining:
        return None  # All done

    # Find ready tasks (all deps completed)
    ready = [
        t for t in remaining
        if all(dep in completed for dep in t.get("depends_on", []))
    ]

    if not ready:
        return []  # Blocked

    return assign_gpus(ready, gpu_ids, gpus_per_task)


def get_batch_info(workspace_root: Path, gpu_ids: list[int], mode: str = "PILOT",
                   gpus_per_task: int = 1) -> dict | None:
    """Get next batch with metadata (assignments + estimated time).

    Returns None if no task_plan, or dict:
        {
            "batch": [assignments],
            "estimated_minutes": int,
            "remaining_count": int,
            "total_count": int,
        }
    Returns {"batch": [], ...} if blocked.
    """
    task_plan_path = workspace_root / "plan" / "task_plan.json"
    if not task_plan_path.exists():
        return None

    try:
        with open(task_plan_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    tasks = plan.get("tasks")
    if not tasks or not isinstance(tasks, list):
        return None

    progress_path = workspace_root / "exp" / "gpu_progress.json"
    completed = set()
    if progress_path.exists():
        try:
            with open(progress_path, encoding="utf-8") as f:
                progress = json.load(f)
            completed = set(progress.get("completed", []))
        except (json.JSONDecodeError, OSError):
            pass

    remaining = [t for t in tasks if t["id"] not in completed]
    if not remaining:
        return None

    ready = [
        t for t in remaining
        if all(dep in completed for dep in t.get("depends_on", []))
    ]

    if not ready:
        return {"batch": [], "estimated_minutes": 0,
                "remaining_count": len(remaining), "total_count": len(tasks)}

    batch = assign_gpus(ready, gpu_ids, gpus_per_task)
    est = estimate_batch_minutes(batch, tasks)

    return {
        "batch": batch,
        "estimated_minutes": est,
        "remaining_count": len(remaining),
        "total_count": len(tasks),
    }
