"""Structured event logging for Sibyl monitoring dashboard.

Provides an append-only JSONL event log (logs/events.jsonl) that records
all significant system events: stage transitions, agent invocations,
pause/resume, errors, experiment dispatch, and iteration milestones.

Each event is a single JSON line with a guaranteed schema:
  {"ts": float, "event": str, "iteration": int, ...event-specific fields}

This module is intentionally dependency-free (no imports from sibyl.*) so it
can be used safely from any layer without circular imports.
"""

import json
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _lock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


class EventLogger:
    """Append-only structured event logger writing to logs/events.jsonl."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.events_file = workspace_root / "logs" / "events.jsonl"

    def _ensure_dir(self):
        self.events_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **kwargs) -> dict:
        """Append a single event to the log. Returns the event dict."""
        entry = {"ts": time.time(), "event": event_type}
        entry.update(kwargs)
        self._ensure_dir()
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with open(self.events_file, "a", encoding="utf-8") as f:
            _lock(f)
            try:
                f.write(line)
            finally:
                _unlock(f)
        return entry

    # ── Stage lifecycle ──────────────────────────────────────────────

    def stage_start(self, stage: str, iteration: int,
                    action_type: str = "", **extra) -> dict:
        return self.log("stage_start", stage=stage, iteration=iteration,
                        action_type=action_type, **extra)

    def stage_end(self, stage: str, iteration: int,
                  duration_sec: float | None = None,
                  score: float | None = None,
                  next_stage: str = "", **extra) -> dict:
        return self.log("stage_end", stage=stage, iteration=iteration,
                        duration_sec=duration_sec, score=score,
                        next_stage=next_stage, **extra)

    # ── Agent lifecycle ──────────────────────────────────────────────

    def agent_start(self, stage: str, agent_name: str,
                    model_tier: str = "", iteration: int = 0,
                    prompt_summary: str = "", **extra) -> dict:
        return self.log("agent_start", stage=stage, agent=agent_name,
                        model_tier=model_tier, iteration=iteration,
                        prompt_summary=prompt_summary, **extra)

    def agent_end(self, stage: str, agent_name: str,
                  status: str = "ok", duration_sec: float | None = None,
                  output_files: list[str] | None = None,
                  output_summary: str = "", iteration: int = 0,
                  **extra) -> dict:
        return self.log("agent_end", stage=stage, agent=agent_name,
                        status=status, duration_sec=duration_sec,
                        output_files=output_files or [],
                        output_summary=output_summary,
                        iteration=iteration, **extra)

    # ── System events ────────────────────────────────────────────────

    def project_init(self, topic: str, project_name: str = "", **extra) -> dict:
        return self.log("project_init", topic=topic,
                        project_name=project_name, **extra)

    def pause(self, reason: str, stage: str = "",
              iteration: int = 0, **extra) -> dict:
        return self.log("pause", reason=reason, stage=stage,
                        iteration=iteration, **extra)

    def resume(self, stage: str = "", iteration: int = 0, **extra) -> dict:
        return self.log("resume", stage=stage, iteration=iteration, **extra)

    def error(self, message: str, stage: str = "",
              category: str = "", iteration: int = 0, **extra) -> dict:
        return self.log("error", message=message, stage=stage,
                        category=category, iteration=iteration, **extra)

    # ── Experiment events ────────────────────────────────────────────

    def task_dispatch(self, task_ids: list[str], gpu_ids: list[int],
                      iteration: int = 0, **extra) -> dict:
        return self.log("task_dispatch", task_ids=task_ids, gpu_ids=gpu_ids,
                        iteration=iteration, **extra)

    def experiment_recover(self, recovered_tasks: list[str],
                           iteration: int = 0, **extra) -> dict:
        return self.log("experiment_recover",
                        recovered_tasks=recovered_tasks,
                        iteration=iteration, **extra)

    def checkpoint_step(self, stage: str, step_id: str,
                        iteration: int = 0, **extra) -> dict:
        return self.log("checkpoint_step", stage=stage, step_id=step_id,
                        iteration=iteration, **extra)

    # ── Iteration milestone ──────────────────────────────────────────

    def iteration_complete(self, iteration: int, score: float | None = None,
                           issues_count: int = 0, **extra) -> dict:
        return self.log("iteration_complete", iteration=iteration,
                        score=score, issues_count=issues_count, **extra)

    # ── Query helpers (for dashboard) ────────────────────────────────

    def read_all(self) -> list[dict]:
        """Read all events from the log file."""
        if not self.events_file.exists():
            return []
        events = []
        for line in self.events_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def tail(self, n: int = 50) -> list[dict]:
        """Read the last N events efficiently."""
        if not self.events_file.exists():
            return []
        # Read from end for efficiency on large files
        lines = []
        try:
            with open(self.events_file, "rb") as f:
                f.seek(0, 2)
                file_size = f.tell()
                if file_size == 0:
                    return []
                # Read chunks from end
                chunk_size = min(file_size, max(8192, n * 2048))
                f.seek(max(0, file_size - chunk_size))
                content = f.read().decode("utf-8", errors="replace")
                raw_lines = content.splitlines()
                # Parse in reverse, collect up to n
                for raw in reversed(raw_lines):
                    if raw.strip():
                        try:
                            lines.append(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
                    if len(lines) >= n:
                        break
        except OSError:
            return []
        lines.reverse()
        return lines

    def query(self, event_type: str | None = None,
              stage: str | None = None,
              agent: str | None = None,
              since: float | None = None,
              limit: int = 200) -> list[dict]:
        """Filter events by type, stage, agent, or time range."""
        results = []
        for ev in self.read_all():
            if event_type and ev.get("event") != event_type:
                continue
            if stage and ev.get("stage") != stage:
                continue
            if agent and ev.get("agent") != agent:
                continue
            if since and ev.get("ts", 0) < since:
                continue
            results.append(ev)
            if len(results) >= limit:
                break
        return results

    def get_stage_durations(self, iteration: int | None = None) -> list[dict]:
        """Compute stage durations from start/end event pairs."""
        starts: dict[str, dict] = {}
        durations = []
        for ev in self.read_all():
            if iteration is not None and ev.get("iteration") != iteration:
                continue
            if ev["event"] == "stage_start":
                key = f"{ev.get('iteration', 0)}:{ev['stage']}"
                starts[key] = ev
            elif ev["event"] == "stage_end":
                key = f"{ev.get('iteration', 0)}:{ev['stage']}"
                start_ev = starts.pop(key, None)
                dur = ev.get("duration_sec")
                if dur is None and start_ev:
                    dur = ev["ts"] - start_ev["ts"]
                durations.append({
                    "stage": ev["stage"],
                    "iteration": ev.get("iteration", 0),
                    "duration_sec": dur,
                    "score": ev.get("score"),
                    "started_at": start_ev["ts"] if start_ev else None,
                    "ended_at": ev["ts"],
                })
        return durations

    def get_agent_summary(self, iteration: int | None = None) -> list[dict]:
        """Summarize agent invocations with durations and statuses."""
        agents = []
        for ev in self.read_all():
            if ev["event"] != "agent_end":
                continue
            if iteration is not None and ev.get("iteration") != iteration:
                continue
            agents.append({
                "agent": ev.get("agent", ""),
                "stage": ev.get("stage", ""),
                "iteration": ev.get("iteration", 0),
                "status": ev.get("status", ""),
                "duration_sec": ev.get("duration_sec"),
                "output_files": ev.get("output_files", []),
                "output_summary": ev.get("output_summary", ""),
            })
        return agents
