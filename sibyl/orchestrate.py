"""Sibyl orchestrator for Claude Code native mode.

This module provides a state-machine orchestrator that returns the next action
for the main Claude Code session to execute. It does NOT call claude-agent-sdk.

Usage (called by Skill via Bash):
    python -c "from sibyl.orchestrate import FarsOrchestrator; ..."
"""
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass, asdict

import yaml

from sibyl._paths import REPO_ROOT, get_system_evolution_dir
from sibyl.config import Config
from sibyl.event_logger import EventLogger
from sibyl.runtime_assets import (
    detect_workspace_root,
    load_project_memory,
    load_project_prompt_overlay,
)
from sibyl.workspace import Workspace, workspace_status_from_data

PAPER_SECTIONS = [
    ("intro", "Introduction"),
    ("related_work", "Related Work"),
    ("method", "Method"),
    ("experiments", "Experiments"),
    ("discussion", "Discussion"),
    ("conclusion", "Conclusion"),
]

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Mapping: stage -> checkpoint directory (relative to workspace root)
CHECKPOINT_DIRS = {
    "idea_debate": "idea",
    "result_debate": "idea/result_debate",
    "writing_sections": "writing/sections",
    "writing_critique": "writing/critique",
}
_RUNTIME_GITIGNORE_LINES = (
    "*.pyc",
    "__pycache__/",
    ".DS_Store",
    ".venv/",
    "CLAUDE.md",
    ".claude/agents",
    ".claude/skills",
    ".claude/settings.local.json",
    ".sibyl/system.json",
)

_FIGURES_BLOCK_RE = re.compile(
    r"<!--\s*FIGURES\s*(.*?)-->",
    re.IGNORECASE | re.DOTALL,
)
_FIGURE_ARTIFACT_RE = re.compile(
    r"[\w./-]+\.(?:pdf|png|svg|py|md)",
    re.IGNORECASE,
)


def extract_section_figure_artifacts(section_markdown: str) -> tuple[list[str], bool]:
    """Extract figure-related artifact paths from a section's FIGURES block.

    Returns:
        (artifacts, has_figures_block)

    The FIGURES block is expected to list exact artifact filenames, e.g.
    `gen_method_overview.py, method_overview.pdf` or `pipeline_desc.md`.
    Filenames without a directory are treated as relative to writing/figures/.
    """
    match = _FIGURES_BLOCK_RE.search(section_markdown)
    if match is None:
        return ([], False)

    artifacts: list[str] = []
    seen: set[str] = set()

    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        if line.lower() in {"- none", "- no figures", "- no figure"}:
            continue

        artifact_part = line
        if ":" in artifact_part:
            artifact_part = artifact_part.split(":", 1)[1]
        artifact_part = re.split(r"\s+—\s+|\s+-\s+", artifact_part, maxsplit=1)[0]

        for artifact in _FIGURE_ARTIFACT_RE.findall(artifact_part):
            rel_path = artifact if "/" in artifact else f"writing/figures/{artifact}"
            if rel_path not in seen:
                seen.add(rel_path)
                artifacts.append(rel_path)

    return (artifacts, True)


def pack_skill_args(*parts: object) -> str:
    """Pack positional skill args using shell-safe quoting.

    Claude Code fork skills consume positional `$ARGUMENTS[n]`, so we keep a
    fixed order but quote each part to preserve spaces safely.
    """
    return " ".join(
        shlex.quote(str(part))
        for part in parts
        if part is not None and str(part) != ""
    )


def language_label(language: str) -> str:
    """Return a human-readable language label for prompts."""
    return "Chinese" if language == "zh" else "English"


def non_paper_output_requirement(language: str) -> str:
    """Prompt snippet for non-paper artifacts that follow config.language."""
    return f"All non-paper output must be written in {language_label(language)}."


def paper_writing_requirement() -> str:
    """Prompt snippet for paper-related drafts, which are always English."""
    return (
        "All paper outlines, section drafts, critiques, integrated paper text, "
        "and writing reviews must be written in English."
    )


def project_marker_file(project_name: str, suffix: str) -> str:
    """Build a per-project marker file path under /tmp."""
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", project_name).strip("-") or "sibyl"
    return f"/tmp/sibyl_{safe_name}_{suffix}.json"


def resolve_workspace_root(workspace_path: str | Path) -> Path:
    """Normalize a workspace path to the stable project root."""
    workspace_root = Path(workspace_path)
    if workspace_root.name == "current" and (workspace_root.parent / "status.json").exists():
        workspace_root = workspace_root.parent
    return workspace_root.resolve()


def build_repo_python_cli_command(*args: str | Path) -> str:
    """Build a shell-safe repo-local `python -m sibyl.cli ...` command."""
    cmd = shlex.join([sys.executable, "-m", "sibyl.cli", *(str(arg) for arg in args)])
    return f"cd {shlex.quote(str(REPO_ROOT))} && {cmd}"


def self_heal_status_file(workspace_path: str | Path) -> str:
    """Return the project-scoped self-heal monitor status file under /tmp."""
    workspace_root = resolve_workspace_root(workspace_path)
    return project_marker_file(workspace_root.name, "self_heal_monitor")


def load_workspace_iteration_dirs(workspace_path: str | Path, default: bool = False) -> bool:
    """Read iteration_dirs from workspace status when available."""
    status_path = resolve_workspace_root(workspace_path) / "status.json"
    if not status_path.exists():
        return default
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
        return bool(status_data.get("iteration_dirs", default))
    except (json.JSONDecodeError, OSError, TypeError):
        return default


def resolve_active_workspace_path(workspace_path: str | Path) -> Path:
    """Normalize a workspace path to the active iteration workspace."""
    workspace_root = resolve_workspace_root(workspace_path)
    if load_workspace_iteration_dirs(workspace_root):
        current_path = workspace_root / "current"
        if current_path.exists():
            return current_path
    return workspace_root


def load_effective_config(
    workspace_path: str | Path | None = None,
    config_path: str | None = None,
) -> Config:
    """Load effective config with explicit path > project config > root config."""
    if config_path:
        return Config.from_yaml(config_path)

    root_config = Path("config.yaml")
    workspace_root = resolve_workspace_root(workspace_path) if workspace_path else None
    project_config = workspace_root / "config.yaml" if workspace_root else None

    if root_config.exists() and project_config and project_config.exists():
        return Config.from_yaml_chain(str(root_config), str(project_config))
    if project_config and project_config.exists():
        return Config.from_yaml(str(project_config))
    if root_config.exists():
        return Config.from_yaml(str(root_config))
    return Config()


def write_project_config(ws: Workspace, config: Config):
    """Persist the effective config into the workspace for stable future runs."""
    ws.write_file(
        "config.yaml",
        yaml.safe_dump(config.to_dict(), allow_unicode=True, sort_keys=False),
    )


def _load_workspace_action_plan(
    ws: Workspace,
    rel_path: str = "reflection/action_plan.json",
    *,
    persist_normalized: bool = False,
) -> dict | None:
    """Load and normalize a reflection action plan if present."""
    from sibyl.evolution import normalize_action_plan

    raw = ws.read_file(rel_path)
    if not raw:
        return None
    try:
        action_plan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    normalized = normalize_action_plan(action_plan)
    if persist_normalized and normalized != action_plan:
        ws.write_file(
            rel_path,
            json.dumps(normalized, indent=2, ensure_ascii=False),
        )
    return normalized


def _load_prompt_evolution_context(
    workspace_path: str | Path | None,
) -> tuple[str, str, list[str]]:
    """Collect workspace context for lesson filtering."""
    from sibyl.evolution import normalize_action_plan

    workspace_root = detect_workspace_root(workspace_path)
    if workspace_root is None:
        return "", "", []

    workspace_root = resolve_workspace_root(workspace_root)
    active_root = resolve_active_workspace_path(workspace_root)
    stage = ""
    topic = ""
    recent_issues: list[str] = []
    seen_issues: set[str] = set()

    status_path = workspace_root / "status.json"
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text(encoding="utf-8"))
            stage = str(status_data.get("stage", "") or "")
        except (json.JSONDecodeError, OSError, TypeError):
            stage = ""

    for topic_path in (active_root / "topic.txt", workspace_root / "topic.txt"):
        if topic_path.exists():
            try:
                topic = topic_path.read_text(encoding="utf-8").strip()
            except OSError:
                topic = ""
            if topic:
                break

    plan_candidates = [
        active_root / "reflection" / "action_plan.json",
        active_root / "reflection" / "prev_action_plan.json",
        workspace_root / "reflection" / "action_plan.json",
        workspace_root / "reflection" / "prev_action_plan.json",
    ]
    for plan_path in plan_candidates:
        if not plan_path.exists():
            continue
        try:
            action_plan = normalize_action_plan(
                json.loads(plan_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, TypeError):
            continue
        for issue in action_plan.get("issues_classified", []):
            if issue.get("status") == "fixed":
                continue
            description = str(issue.get("description", "")).strip()
            if description and description not in seen_issues:
                seen_issues.add(description)
                recent_issues.append(description)
        if recent_issues:
            break

    return topic, stage, recent_issues


def _load_evolution_overlay(
    agent_name: str,
    workspace_path: str | Path | None = None,
) -> str:
    """Load contextual lessons first, then fall back to the global overlay."""
    from sibyl.evolution import EvolutionEngine

    topic, stage, recent_issues = _load_prompt_evolution_context(workspace_path)
    engine = EvolutionEngine()
    if topic or stage or recent_issues:
        contextual = engine.filter_relevant_lessons(
            agent_name=agent_name,
            topic=topic,
            stage=stage,
            recent_issues=recent_issues,
        )
        if contextual:
            return contextual

    overlay_path = get_system_evolution_dir() / "lessons" / f"{agent_name}.md"
    if overlay_path.exists():
        return overlay_path.read_text(encoding="utf-8")
    return ""


def _append_prompt_layer(base: str, content: str) -> str:
    if content.strip():
        return f"{base}\n\n---\n\n{content}"
    return base


def load_prompt(
    agent_name: str,
    overlay_content: str | None = None,
    workspace_path: str | Path | None = None,
) -> str:
    """Load an agent prompt from the prompts/ directory, with overlay injection.

    If overlay_content is provided (e.g. from filter_relevant_lessons),
    use it instead of the global overlay file.
    """
    path = PROMPTS_DIR / f"{agent_name}.md"
    if not path.exists():
        return ""
    base = path.read_text(encoding="utf-8")

    if overlay_content is not None:
        base = _append_prompt_layer(base, overlay_content)
    else:
        overlay = _load_evolution_overlay(agent_name, workspace_path)
        if overlay:
            base = _append_prompt_layer(base, overlay)

    project_overlay = load_project_prompt_overlay(agent_name, workspace_path)
    if project_overlay:
        base = _append_prompt_layer(base, project_overlay)

    return base


def load_common_prompt(workspace_path: str | Path | None = None) -> str:
    """Load the common instructions prompt in the configured language.

    Reads SIBYL_LANGUAGE env var (set by plugin commands from action.language).
    Default "zh" -> loads _common_zh.md; "en" -> loads _common.md.
    """
    import os
    lang = os.environ.get("SIBYL_LANGUAGE", "zh")
    filename = "_common_zh" if lang == "zh" else "_common"
    prompt = load_prompt(filename, workspace_path=workspace_path)
    project_memory = load_project_memory(workspace_path)
    if project_memory:
        prompt = _append_prompt_layer(prompt, project_memory)
    return prompt


def cli_write_ralph_prompt(
    workspace_path: str,
    project_name: str | None = None,
    output_path: str = "/tmp/sibyl-ralph-prompt.txt",
) -> None:
    """Load ralph_loop prompt template, inject parameters, write to file.

    Called by start.md and resume.md to generate the Ralph Loop prompt.
    """
    import json

    ws = Path(workspace_path)
    if project_name is None:
        project_name = ws.name

    template = load_prompt("ralph_loop", workspace_path=workspace_path)
    if not template:
        print(json.dumps({"error": "ralph_loop.md not found in prompts/"}))
        return

    content = template.replace("{project_name}", project_name)
    content = content.replace("{workspace_path}", str(workspace_path))

    Path(output_path).write_text(content, encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "output_path": output_path,
        "project_name": project_name,
        "chars": len(content),
    }))


@dataclass
class AgentTask:
    """A task to be executed by a Claude Code Agent."""
    agent_name: str
    prompt: str
    description: str
    workspace_path: str


@dataclass
class Action:
    """An action for the main Claude Code session to execute."""
    action_type: str  # "skill", "skills_parallel", "agents_parallel", "agent_single", "team", "bash", "gpu_poll", "experiment_wait", "done", "stopped"
    agents: list[dict] | None = None  # for legacy agent actions
    skills: list[dict] | None = None  # for fork skill actions: [{"name": "sibyl-xxx", "args": "..."}]
    team: dict | None = None  # for Agent Teams: {"prompt": "...", "teammates": [{"role": "...", "prompt": "..."}], "require_plan_approval": bool}
    bash_command: str | None = None  # for bash actions
    gpu_poll: dict | None = None  # for gpu_poll actions: {"ssh_connection", "query_cmd", "max_gpus", "threshold_mb", "interval_sec", "marker_file"}
    description: str = ""
    stage: str = ""
    iteration: int = 0
    estimated_minutes: int = 0  # expected runtime hint for experiment batches
    checkpoint_info: dict | None = None  # {resuming, completed_steps, remaining_steps, all_complete}
    experiment_monitor: dict | None = None  # {script, marker_file, task_ids, timeout_minutes}


class FarsOrchestrator:
    """State-machine orchestrator for Sibyl research pipeline.

    Called by the Sibyl Skill, returns the next action for Claude Code to execute.
    """

    # Pipeline stages in order
    STAGES = [
        "init",
        "literature_search",
        "idea_debate",
        "planning",
        "pilot_experiments",
        "idea_validation_decision",
        "experiment_cycle",
        "result_debate",
        "experiment_decision",
        "writing_outline",
        "writing_sections",
        "writing_critique",
        "writing_integrate",
        "writing_final_review",
        "writing_latex",
        "review",
        "reflection",
        "quality_gate",
        "done",
    ]

    def __init__(self, workspace_path: str, config: Config | None = None):
        ws_path = resolve_workspace_root(Path(workspace_path).expanduser())
        if config is not None:
            self.config = config
        else:
            self.config = load_effective_config(ws_path)
        iteration_dirs = load_workspace_iteration_dirs(ws_path, self.config.iteration_dirs)
        self.ws = Workspace(
            ws_path.parent,
            ws_path.name,
            iteration_dirs=iteration_dirs,
        )
        self.project_path = str(self.ws.root)
        self.workspace_path = str(self.ws.active_root)

    @classmethod
    def init_project(cls, topic: str, project_name: str | None = None,
                     config_path: str | None = None) -> dict:
        """Initialize a new research project. Returns project info."""
        config = load_effective_config(config_path=config_path)

        if project_name is None:
            project_name = cls._slugify(topic)

        ws = Workspace(
            config.workspaces_dir,
            project_name,
            iteration_dirs=config.iteration_dirs,
        )

        write_project_config(ws, config)
        ws.write_file("topic.txt", topic)
        ws.update_stage("init")
        ws.git_init()

        return {
            "project_name": project_name,
            "workspace_path": str(ws.root),
            "topic": topic,
            "config": {
                "ssh_server": config.ssh_server,
                "remote_base": config.remote_base,
                "max_gpus": config.max_gpus,
                "pilot_samples": config.pilot_samples,
                "pilot_seeds": config.pilot_seeds,
                "full_seeds": config.full_seeds,
                "debate_rounds": config.debate_rounds,
                "idea_exp_cycles": config.idea_exp_cycles,
                "lark_enabled": config.lark_enabled,
                "iteration_dirs": config.iteration_dirs,
                "language": config.language,
            },
        }

    def _resolve_model_tier(self, agent_name: str) -> tuple[str, str]:
        """Return (tier, model_id) for a given agent name."""
        tier_key = agent_name
        if agent_name.startswith("writer_"):
            tier_key = "section_writer"
        elif agent_name.startswith("critic_") and agent_name != "critic":
            tier_key = "section_critic"
        elif "_critiques_" in agent_name:
            tier_key = "idea_critique"

        tier = self.config.agent_tier_map.get(tier_key, "standard")
        model = self.config.model_tiers.get(tier, self.config.model_tiers["standard"])
        return tier, model

    def _control_plane_language_name(self) -> str:
        """Return the current locale name for discussion/review artifacts."""
        return "Chinese" if self.config.language == "zh" else "English"

    def _non_paper_output_instruction(self) -> str:
        """Language instruction for user-visible/non-paper artifacts."""
        return non_paper_output_requirement(self.config.language)

    @staticmethod
    def _paper_output_instruction() -> str:
        """Language instruction for paper-writing artifacts."""
        return paper_writing_requirement()

    def _codex_reviewer_args(self, mode: str, ws: str) -> str:
        """Build reviewer args with an optional model override."""
        if self.config.codex_model:
            return pack_skill_args(ws, mode, self.config.codex_model)
        return pack_skill_args(ws, mode)

    def _codex_writer_args(self, ws: str) -> str:
        """Build Codex writer args with an optional model override."""
        model = self.config.codex_writing_model or self.config.codex_model
        if model:
            return pack_skill_args(ws, model)
        return pack_skill_args(ws)

    def get_next_action(self) -> dict:
        """Determine and return the next action based on current state."""
        self.workspace_path = str(self.ws.active_root)
        status = self.ws.get_status()

        if status.stop_requested:
            stopped_at = (
                f"项目已于 {time.strftime('%H:%M', time.localtime(status.stop_requested_at))} 手动停止。"
                if status.stop_requested_at is not None else
                "项目已手动停止。"
            )
            return asdict(Action(
                action_type="stopped",
                description=f"{stopped_at}使用 /sibyl-research:resume 重新进入自治循环。",
                stage=status.stage,
                iteration=status.iteration,
            ))

        # Legacy pause markers should never stall the autonomous loop.
        if status.paused:
            self.ws.resume()
            status = self.ws.get_status()

        stage = status.stage
        topic = self.ws.read_file("topic.txt") or ""

        action = self._compute_action(stage, topic, status.iteration)
        if action.iteration == 0:
            action.iteration = status.iteration

        # Inject model tier info into legacy agents (cross-critique still uses this)
        if action.agents:
            for agent in action.agents:
                tier, model = self._resolve_model_tier(agent["name"])
                agent["model_tier"] = tier
                agent["model"] = model

        result = asdict(action)
        result["language"] = self.config.language
        return result

    def record_result(self, stage: str, result: str = "",
                      score: float | None = None):
        """Record the result of a completed stage and advance state.

        Idempotent for stale retries: if a stage has already been advanced
        past, this is a no-op. Future-stage mismatches still raise so callers
        do not accidentally mark the wrong stage as complete.
        """
        if stage == "done":
            raise ValueError("Cannot record result for terminal stage 'done'")
        current = self.ws.get_status().stage
        if stage != current:
            try:
                if self.STAGES.index(stage) < self.STAGES.index(current):
                    return
            except ValueError:
                pass
            raise ValueError(
                f"Stage mismatch: recording '{stage}' but current is '{current}'"
            )

        # Post-reflection hook: process reflection agent outputs
        if stage == "reflection":
            self._post_reflection_hook()

        next_stage, new_iteration = self._get_next_stage(stage, result, score)
        if new_iteration is not None:
            self.ws.update_stage_and_iteration(next_stage, new_iteration)
        else:
            self.ws.update_stage(next_stage)

        if score is not None:
            self.ws.write_file(
                f"logs/stage_{stage}_score.txt",
                f"{score}"
            )

        # Auto git commit after each stage
        score_str = f" (score={score})" if score is not None else ""
        self.ws.git_commit(f"sibyl: complete {stage}{score_str}")

        # Trigger background Feishu sync if enabled
        _NO_SYNC_TRIGGER = {"init", "quality_gate", "done", "lark_sync"}
        if (self.config.lark_enabled
                and stage not in _NO_SYNC_TRIGGER):
            self._append_pending_sync(stage)

    def _append_pending_sync(self, stage: str):
        """Append a sync trigger to lark_sync/pending_sync.jsonl."""
        import datetime
        entry = {
            "trigger_stage": stage,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "iteration": self.ws.get_status().iteration,
        }
        sync_dir = self.ws.root / "lark_sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        with open(sync_dir / "pending_sync.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def get_status(self) -> dict:
        """Get current project status."""
        meta = self.ws.get_project_metadata()
        meta["topic"] = self.ws.read_file("topic.txt") or ""
        return meta

    def _compute_action(self, stage: str, topic: str, iteration: int) -> Action:
        """Compute the next action based on current stage."""
        ws = self.workspace_path

        # Backward compat: migrate old stage names
        if stage in ("critic_review", "supervisor_review"):
            stage = "review"
            self.ws.update_stage("review")

        if stage == "init":
            # init is a transient stage; the real research work starts at
            # literature_search after callers record init as complete.
            return Action(
                action_type="bash",
                bash_command="echo 'Sibyl project initialized'",
                description="项目初始化完成，推进到 literature_search 后再执行文献调研",
                stage="init",
            )

        elif stage == "literature_search":
            return self._action_literature_search(topic, ws)

        elif stage == "idea_debate":
            return self._action_idea_debate(topic, ws)

        elif stage == "planning":
            return self._action_planning(ws)

        elif stage == "pilot_experiments":
            return self._action_pilot_experiments(ws)

        elif stage == "idea_validation_decision":
            return self._action_idea_validation_decision(ws)

        elif stage == "experiment_cycle":
            return self._action_experiment_cycle(ws, iteration)

        elif stage == "result_debate":
            return self._action_result_debate(ws)

        elif stage == "experiment_decision":
            return self._action_experiment_decision(ws)

        elif stage == "writing_outline":
            return self._action_writing_outline(ws)

        elif stage == "writing_sections":
            return self._action_writing_sections(ws)

        elif stage == "writing_critique":
            return self._action_writing_critique(ws)

        elif stage == "writing_integrate":
            return self._action_writing_integrate(ws)

        elif stage == "writing_final_review":
            return self._action_writing_final_review(ws)

        elif stage == "writing_latex":
            return self._action_writing_latex(ws)

        elif stage == "review":
            return self._action_review(ws)

        elif stage == "reflection":
            return self._action_reflection(ws, iteration)

        elif stage == "quality_gate":
            return self._action_quality_gate()

        elif stage == "done":
            return Action(action_type="done", description="Pipeline complete", stage="done")

        else:
            return Action(action_type="done", description="Unknown stage", stage="done")

    # ══════════════════════════════════════════════
    # Checkpoint helpers
    # ══════════════════════════════════════════════

    def _get_or_create_checkpoint(self, stage: str, steps: dict[str, str]) -> dict | None:
        """Get validated checkpoint or create a new one.

        Returns checkpoint_info dict: {resuming, completed_steps, remaining_steps, all_complete}
        or None if this stage doesn't support checkpoints.
        """
        cp_dir = CHECKPOINT_DIRS.get(stage)
        if cp_dir is None:
            return None

        iteration = self.ws.get_status().iteration

        # Try to load and validate existing checkpoint
        valid = self.ws.validate_checkpoint(cp_dir, current_iteration=iteration)
        if valid is not None:
            if not valid["remaining"]:
                return {
                    "resuming": True,
                    "completed_steps": valid["completed"],
                    "remaining_steps": [],
                    "all_complete": True,
                    "checkpoint_dir": cp_dir,
                }
            return {
                "resuming": True,
                "completed_steps": valid["completed"],
                "remaining_steps": valid["remaining"],
                "all_complete": False,
                "checkpoint_dir": cp_dir,
            }

        # No valid checkpoint — create fresh
        self.ws.create_checkpoint(stage, cp_dir, steps, iteration=iteration)
        return {
            "resuming": False,
            "completed_steps": [],
            "remaining_steps": list(steps.keys()),
            "all_complete": False,
            "checkpoint_dir": cp_dir,
        }

    # ══════════════════════════════════════════════
    # Action builders
    # ══════════════════════════════════════════════

    def _action_literature_search(self, topic: str, ws: str) -> Action:
        """Single fork skill performs literature search via arXiv + WebSearch."""
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-literature", "args": pack_skill_args(ws, topic)}],
            description="文献调研：arXiv 搜索 + Web 搜索，建立领域现状基础",
            stage="literature_search",
        )

    def _action_idea_debate(self, topic: str, ws: str) -> Action:
        """Agent Team: 6 teammates generate, debate, and synthesize research ideas.

        Six diverse perspectives ensure thorough exploration:
        - Innovator: bold cross-domain transfer ideas
        - Pragmatist: engineering-feasible, resource-conscious ideas
        - Theoretical: mathematically grounded, provable guarantees
        - Contrarian: challenges assumptions, finds blind spots
        - Interdisciplinary: borrows from other sciences (neuro, physics, bio)
        - Empiricist: experiment-first thinking, rigorous evaluation design
        """
        # Checkpoint: track per-perspective progress
        idea_roles = ["innovator", "pragmatist", "theoretical", "contrarian",
                       "interdisciplinary", "empiricist"]
        steps = {role: f"idea/perspectives/{role}.md" for role in idea_roles}
        cp_info = self._get_or_create_checkpoint("idea_debate", steps)

        if cp_info and cp_info["all_complete"]:
            return Action(
                action_type="bash",
                bash_command="echo 'All idea perspectives already written (checkpoint valid)'",
                description="所有视角提案已完成（checkpoint 校验通过），可直接 record",
                stage="idea_debate",
                checkpoint_info=cp_info,
            )

        # Prepare context file for teammates to read
        spec = self.ws.read_file("spec.md") or ""
        initial_ideas = self.ws.read_file("idea/initial_ideas.md") or ""
        seed_refs = self.ws.read_file("idea/references_seed.md") or ""
        literature = self.ws.read_file("context/literature.md") or ""
        prior_proposal = self.ws.read_file("idea/proposal.md") or ""
        prior_hypotheses = self.ws.read_file("idea/hypotheses.md") or ""
        pilot_summary = self.ws.read_file("exp/results/pilot_summary.md") or ""
        pilot_summary_json = self.ws.read_file("exp/results/pilot_summary.json") or ""
        candidate_ideas = self.ws.read_file("idea/candidates.json") or ""
        validation_feedback = self.ws.read_file("supervisor/idea_validation_decision.md") or ""
        validation_feedback_json = (
            self.ws.read_file("supervisor/idea_validation_decision.json") or ""
        )
        validation_round = self._get_current_validation_round()

        extra_context = ""
        if spec:
            extra_context += f"\n\n## Project Spec\n{spec}"
        if initial_ideas:
            extra_context += f"\n\n## User's Initial Ideas\n{initial_ideas}"
        if seed_refs:
            extra_context += f"\n\n## Seed References (from user)\n{seed_refs}"
        if literature:
            extra_context += f"\n\n## 文献调研报告（请仔细阅读，避免重复已有工作）\n{literature}"
        if prior_proposal:
            extra_context += (
                "\n\n## 当前综合提案（如已有，请在此基础上迭代，而不是从零开始）\n"
                f"{prior_proposal}"
            )
        if prior_hypotheses:
            extra_context += f"\n\n## 当前可检验假设\n{prior_hypotheses}"
        if pilot_summary:
            extra_context += (
                "\n\n## 小型实验真实反馈（必须基于这些证据修正 idea，不能忽略负结果）\n"
                f"{pilot_summary}"
            )
        if pilot_summary_json:
            extra_context += (
                "\n\n## 小型实验结构化信号（供你提炼 go/no-go / confidence / hypothesis status）\n"
                f"{pilot_summary_json}"
            )
        if candidate_ideas:
            extra_context += (
                "\n\n## 当前候选 idea 池（保留 2-3 个候选，必要时淘汰或替换）\n"
                f"{candidate_ideas}"
            )
        if validation_feedback:
            extra_context += (
                "\n\n## 上一轮 validation 决策意见\n"
                f"{validation_feedback}"
            )
        if validation_feedback_json:
            extra_context += (
                "\n\n## 上一轮 validation 结构化决策\n"
                f"{validation_feedback_json}"
            )

        if extra_context:
            self.ws.write_file("context/idea_context.md", extra_context)

        remaining = set(cp_info["remaining_steps"]) if cp_info else set(idea_roles)

        refinement_hint = ""
        if pilot_summary:
            refinement_hint = (
                "This is an evidence-driven refinement round. Read the pilot summary carefully, "
                "update or discard hypotheses that the data weakened, preserve the parts that "
                "show early promise, and make the next proposal easier to falsify.\n\n"
            )
            if validation_round > 0:
                refinement_hint = (
                    f"This is evidence-driven refinement round {validation_round + 1}. "
                    + refinement_hint
                )
        candidate_hint = (
            "Maintain a small candidate pool: keep 2-3 serious ideas alive until pilot evidence "
            "separates them. Do not collapse to a single idea too early unless the evidence is overwhelming.\n\n"
        )

        team_prompt = (
            f"Create an agent team to generate and debate research ideas for: {topic}\n\n"
            f"Workspace: {ws}\n\n"
            f"{refinement_hint}"
            f"{candidate_hint}"
            f"Spawn teammates for remaining perspectives:\n"
            + "\n".join(f"- {role}" for role in idea_roles if role in remaining) + "\n\n"
            f"Each reads {ws}/context/idea_context.md for background and writes to "
            f"{ws}/idea/perspectives/<role>.md\n\n"
            f"After generating ideas, have teammates critique each other's work (score 1-10). "
            f"Write critiques to {ws}/idea/debate/CRITIC_on_AUTHOR.md\n\n"
            f"Finally, synthesize all ideas and critiques into a final proposal at "
            f"{ws}/idea/proposal.md. Pick the strongest idea, incorporating feedback.\n\n"
            f"Run exactly {self.config.debate_rounds} critique rounds before final synthesis.\n"
            f"{non_paper_output_requirement(self.config.language)} Use Sonnet for teammates."
        )

        all_teammates = [
            {"name": "innovator", "skill": "sibyl-innovator", "args": pack_skill_args(ws, topic)},
            {"name": "pragmatist", "skill": "sibyl-pragmatist", "args": pack_skill_args(ws, topic)},
            {"name": "theoretical", "skill": "sibyl-theoretical", "args": pack_skill_args(ws, topic)},
            {"name": "contrarian", "skill": "sibyl-contrarian", "args": pack_skill_args(ws, topic)},
            {"name": "interdisciplinary", "skill": "sibyl-interdisciplinary", "args": pack_skill_args(ws, topic)},
            {"name": "empiricist", "skill": "sibyl-empiricist", "args": pack_skill_args(ws, topic)},
        ]
        teammates = [t for t in all_teammates if t["name"] in remaining]

        post_steps = [
            {"type": "skill", "skill": "sibyl-synthesizer", "args": ws},
        ]
        if self.config.codex_enabled:
            post_steps.append({
                "type": "codex",
                "skill": "sibyl-codex-reviewer",
                "args": self._codex_reviewer_args("idea_debate", ws),
            })

        team_dict = {
            "team_name": "sibyl-idea-debate",
            "teammates": teammates,
            "post_steps": post_steps,
            "prompt": team_prompt,
        }

        return Action(
            action_type="team",
            team=team_dict,
            description=f"Agent Team: {len(teammates)}人辩论生成研究提案"
                        + (f"（恢复：已完成 {len(cp_info['completed_steps'])}/6）"
                           if cp_info and cp_info["resuming"] else
                           "（创新者+实用主义者+理论家+反对者+跨学科者+实验主义者）")
                        + (" + Codex 独立审查" if self.config.codex_enabled else ""),
            stage="idea_debate",
            checkpoint_info=cp_info,
        )

    def _action_planning(self, ws: str) -> Action:
        pilot_config = (
            f"samples={self.config.pilot_samples}, "
            f"seeds={self.config.pilot_seeds}, timeout={self.config.pilot_timeout}s"
        )
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-planner", "args": pack_skill_args(ws, "plan", pilot_config)}],
            description="Design experiment plan with pilot/full configs",
            stage="planning",
        )

    def _action_pilot_experiments(self, ws: str) -> Action:
        return self._action_experiment_batch(ws, "PILOT", "pilot_experiments")

    def _action_idea_validation_decision(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-idea-validation-decision", "args": ws}],
            description="Review pilot evidence and decide ADVANCE / REFINE / PIVOT",
            stage="idea_validation_decision",
        )

    def _action_experiment_cycle(self, ws: str, iteration: int) -> Action:
        return self._action_experiment_batch(ws, "FULL", "experiment_cycle")

    def _action_experiment_batch(self, ws: str, mode: str, stage: str) -> Action:
        """Build experiment action with GPU-aware batch scheduling.

        If task_plan.json exists and has a tasks array, uses gpu_scheduler
        to assign GPU subsets to parallel tasks. Otherwise falls back to
        single-agent mode with all GPUs.

        Each task MUST declare:
          - gpu_count: how many GPUs it needs
          - estimated_minutes: expected runtime
        If any task is missing these fields, returns a planner action to fix it.

        GPU polling: If gpu_poll_enabled is True, checks free GPUs via SSH
        before scheduling. If no GPUs are free, returns a bash polling action
        that waits for availability without consuming LLM tokens.
        """
        from sibyl.gpu_scheduler import (
            get_batch_info, validate_task_plan, read_poll_result,
            get_running_gpu_ids, _load_progress,
        )

        # --- Check if experiments are already running (must be BEFORE gpu_poll) ---
        from sibyl.experiment_recovery import (
            load_experiment_state as _load_exp_state,
            get_running_tasks as _get_exp_running,
        )
        _exp_state = _load_exp_state(self.ws.active_root)
        _exp_running = _get_exp_running(_exp_state)
        _running_gpus = get_running_gpu_ids(self.ws.active_root)

        if _exp_running or _running_gpus:
            # Experiments already launched — check if any new tasks are schedulable
            _completed_set, _, _, _ = _load_progress(self.ws.active_root)
            # Sync completed tasks from gpu_progress to experiment_state
            import datetime as _dt_batch
            _changed = False
            for tid in list(_exp_running):
                if tid in _completed_set:
                    _exp_state.tasks[tid]["status"] = "completed"
                    _exp_state.tasks[tid]["completed_at"] = _dt_batch.datetime.now().isoformat()
                    _changed = True
            if _changed:
                from sibyl.experiment_recovery import save_experiment_state as _save_exp_state
                _save_exp_state(self.ws.active_root, _exp_state)
                # Re-check after sync
                _exp_running = _get_exp_running(_exp_state)
                _running_gpus = get_running_gpu_ids(self.ws.active_root)

            # If tasks still running, check for dispatchable new tasks
            if _exp_running or _running_gpus:
                # Exclude GPUs occupied by running tasks
                _occupied = set(_running_gpus)
                _free_gpus = [g for g in range(self.config.max_gpus) if g not in _occupied]
                if not _free_gpus:
                    # All GPUs occupied → can only wait
                    return self._experiment_wait_action(stage, _exp_running, _running_gpus)
                _info = get_batch_info(
                    self.ws.active_root, _free_gpus, mode,
                    gpus_per_task=self.config.gpus_per_task,
                )
                _has_pending = _info is not None and len(_info["batch"]) > 0
                if not _has_pending:
                    # No new tasks to schedule → return experiment_wait action
                    return self._experiment_wait_action(stage, _exp_running, _running_gpus)

        # --- GPU availability check (shared server) ---
        if self.config.gpu_poll_enabled:
            # Check if a previous poll found free GPUs
            free_gpus = read_poll_result(project_marker_file(self.ws.name, "gpu_free"))
            if free_gpus is not None and len(free_gpus) > 0:
                # Use polled free GPUs, capped by max_gpus
                effective_gpu_ids = free_gpus[:self.config.max_gpus]
            else:
                # No poll result or empty → start polling
                return self._gpu_poll_action(stage)
        else:
            # No polling: assume GPUs 0..max_gpus-1 are available
            effective_gpu_ids = list(range(self.config.max_gpus))
            # Local mode with max_gpus=0: create virtual CPU slot for CPU-only tasks
            if not effective_gpu_ids and self.config.experiment_mode == "local":
                effective_gpu_ids = [0]

        # --- Auto-recovery: check for stale running tasks ---
        from sibyl.experiment_recovery import (
            load_experiment_state, save_experiment_state,
            get_running_tasks, migrate_from_gpu_progress,
        )
        # _load_progress already imported at top of method
        exp_state = load_experiment_state(self.ws.active_root)
        # Backward compat: migrate from gpu_progress if no experiment_state
        if not exp_state.tasks:
            _, running_ids, _, _ = _load_progress(self.ws.active_root)
            if running_ids:
                exp_state = migrate_from_gpu_progress(self.ws.active_root)
                save_experiment_state(self.ws.active_root, exp_state)

        running_tasks = get_running_tasks(exp_state)
        if running_tasks:
            # Local sync: check if gpu_progress already marked some as completed
            completed_set, _, _, _ = _load_progress(self.ws.active_root)
            import datetime
            changed = False
            for tid in running_tasks:
                if tid in completed_set:
                    exp_state.tasks[tid]["status"] = "completed"
                    exp_state.tasks[tid]["completed_at"] = datetime.datetime.now().isoformat()
                    changed = True
            if changed:
                save_experiment_state(self.ws.active_root, exp_state)

        # Validate task plan completeness before scheduling
        task_plan_path = self.ws.active_path("plan/task_plan.json")
        if task_plan_path.exists():
            try:
                plan = json.loads(task_plan_path.read_text(encoding="utf-8"))
                tasks = plan.get("tasks", [])
                if tasks:
                    incomplete = validate_task_plan(tasks)
                    if incomplete:
                        ids_str = ", ".join(incomplete[:5])
                        remaining = len(incomplete) - 5
                        suffix = f" 等 {len(incomplete)} 个任务" if remaining > 0 else ""
                        return Action(
                            action_type="skill",
                            skills=[{
                                "name": "sibyl-planner",
                                "args": pack_skill_args(ws, "fix-gpu"),
                            }],
                            description=(
                                f"task_plan.json 中 {ids_str}{suffix} 缺少 gpu_count/estimated_minutes，"
                                f"需要 planner 补全后才能调度实验"
                            ),
                            stage=stage,
                        )
            except (json.JSONDecodeError, OSError):
                pass

        info = get_batch_info(
            self.ws.active_root, effective_gpu_ids, mode,
            gpus_per_task=self.config.gpus_per_task,
        )

        # No task_plan or all tasks complete → single-agent fallback
        if info is None:
            return self._experiment_skill(mode, ws, effective_gpu_ids, stage)

        batch = info["batch"][:self.config.max_parallel_tasks]

        # All tasks blocked by deps (shouldn't happen with valid DAG)
        if len(batch) == 0:
            return Action(
                action_type="bash",
                bash_command='echo "All experiment tasks blocked by dependencies"',
                description="实验任务被依赖阻塞",
                stage=stage,
            )

        est_min = info["estimated_minutes"]
        remaining = info["remaining_count"]
        total = info["total_count"]

        # Build parallel skills, one per GPU assignment
        skills = []
        for assignment in batch:
            task_ids = ",".join(assignment["task_ids"])
            gpu_ids = assignment["gpu_ids"]
            skills.append(
                self._experiment_skill_dict(mode, ws, gpu_ids, task_ids)
            )

        progress_str = f"[{total - remaining}/{total}]"
        gpu_summary = ", ".join(
            f"{a['task_ids'][0]}→GPU{a['gpu_ids']}" for a in batch
        )
        calibrated = info.get("calibrated", False)
        ratio = info.get("calibration_ratio", 1.0)
        cal_hint = f" (校准×{ratio})" if calibrated else ""
        desc = (
            f"{progress_str} 并行 {len(skills)} 任务 ({mode}), "
            f"预计 {est_min}min{cal_hint}: {gpu_summary}"
        )

        # Register dispatched tasks as running in gpu_progress.json
        from sibyl.gpu_scheduler import register_running_tasks
        task_gpu_map = {}
        all_task_ids = []
        for assignment in batch:
            for tid in assignment["task_ids"]:
                task_gpu_map[tid] = assignment["gpu_ids"]
                all_task_ids.append(tid)
        register_running_tasks(self.ws.active_root, task_gpu_map)

        # Register in experiment_state.json (authoritative lifecycle)
        from sibyl.experiment_recovery import (
            load_experiment_state, save_experiment_state, register_task as register_exp_task,
        )
        exp_state = load_experiment_state(self.ws.active_root)
        remote_dir = f"{self.config.remote_base}/projects/{self.ws.name}"
        for tid, gpus in task_gpu_map.items():
            pid_file = f"{remote_dir}/exp/results/{tid}.pid"
            register_exp_task(exp_state, tid, gpu_ids=gpus, pid_file=pid_file)
        save_experiment_state(self.ws.active_root, exp_state)

        # Build experiment monitor for background progress tracking
        monitor = self._build_experiment_monitor(all_task_ids, est_min)

        action_type = "skills_parallel" if len(skills) > 1 else "skill"
        return Action(
            action_type=action_type,
            skills=skills,
            description=desc,
            stage=stage,
            estimated_minutes=est_min,
            experiment_monitor=monitor,
        )

    def _experiment_skill_dict(self, mode: str, ws: str, gpu_ids: list[int],
                                task_ids: str = "") -> dict:
        """Build a single experimenter skill dict."""
        gpu_ids_str = ",".join(str(g) for g in gpu_ids)
        env_cmd = self.config.get_remote_env_cmd(self.ws.name)
        if self.config.experiment_mode == "local":
            arg_parts = [
                ws,
                mode,
                "local",   # ssh_server = local → triggers direct Bash execution
                ".",       # remote_base = current dir
                env_cmd,
                "",        # no GPU IDs for CPU-only local execution
            ]
            if task_ids:
                arg_parts.append(f"--tasks={task_ids}")
            return {
                "name": "sibyl-experimenter",
                "args": pack_skill_args(*arg_parts),
            }
        if self.config.experiment_mode in ("server_codex", "server_claude"):
            arg_parts = [
                ws,
                mode,
                self.config.ssh_server,
                self.config.remote_base,
                env_cmd,
                gpu_ids_str,
                self.config.experiment_mode,
                self.config.server_codex_path,
                self.config.server_claude_path,
            ]
            if task_ids:
                arg_parts.append(f"--tasks={task_ids}")
            return {
                "name": "sibyl-server-experimenter",
                "args": pack_skill_args(*arg_parts),
            }
        arg_parts = [
            ws,
            mode,
            self.config.ssh_server,
            self.config.remote_base,
            env_cmd,
            gpu_ids_str,
        ]
        if task_ids:
            arg_parts.append(f"--tasks={task_ids}")
        return {
            "name": "sibyl-experimenter",
            "args": pack_skill_args(*arg_parts),
        }

    def _experiment_skill(self, mode: str, ws: str, gpu_ids: list[int],
                          stage: str) -> Action:
        """Single-agent experiment action (fallback when no task_plan)."""
        skill = self._experiment_skill_dict(mode, ws, gpu_ids)
        is_server = self.config.experiment_mode in ("server_codex", "server_claude")
        return Action(
            action_type="skill",
            skills=[skill],
            description=(
                f"Run {mode.lower()} experiments"
                + (f" on server ({self.config.experiment_mode})" if is_server else "")
            ),
            stage=stage,
        )

    def _build_experiment_monitor(self, task_ids: list[str],
                                    estimated_minutes: int) -> dict:
        """Build experiment monitor config for background progress tracking.

        Returns a dict that the main session uses to start a background
        bash process monitoring task completion on the remote server.
        """
        from sibyl.gpu_scheduler import experiment_monitor_script

        project_name = self.ws.name
        remote_dir = f"{self.config.remote_base}/projects/{project_name}"
        # Timeout = 2x estimated or minimum 30 min
        timeout_min = max(30, estimated_minutes * 2) if estimated_minutes > 0 else 0
        timeout_min = max(timeout_min, max(1, self.config.experiment_timeout // 60))
        # Poll every 5 min (or 2 min for short tasks)
        poll_sec = 120 if estimated_minutes <= 15 else 300
        marker = project_marker_file(project_name, "exp_monitor")

        script = experiment_monitor_script(
            ssh_server=self.config.ssh_server,
            remote_project_dir=remote_dir,
            task_ids=task_ids,
            poll_interval_sec=poll_sec,
            timeout_minutes=timeout_min,
            marker_file=marker,
        )

        # SSH MCP check command: returns task_id:DONE/PENDING per line
        done_checks = " && ".join(
            f'test -f {remote_dir}/exp/results/{tid}_DONE && echo "{tid}:DONE" || echo "{tid}:PENDING"'
            for tid in task_ids
        )

        return {
            "script": script,
            "marker_file": marker,
            "task_ids": task_ids,
            "timeout_minutes": timeout_min,
            "poll_interval_sec": poll_sec,
            # SSH MCP polling fields
            "ssh_connection": self.config.ssh_server,
            "check_cmd": done_checks,
            "remote_dir": remote_dir,
            # Dynamic dispatch: when a task completes, dispatch next queued task
            "dynamic_dispatch": True,
            "dispatch_cmd": build_repo_python_cli_command(
                "dispatch",
                self.workspace_path,
            ),
        }

    def _gpu_poll_action(self, stage: str) -> Action:
        """Return a gpu_poll action for the main session to execute.

        The main session should:
        1. Prefer executing the provided script verbatim; it already honors
           interval_sec, aggressive mode, and max_attempts consistently.
        2. If implementing the loop manually, run query_cmd via ssh_connection,
           parse free GPUs, and stop after max_attempts when non-zero.
        3. If free GPUs are found, write marker_file and re-call cli_next().
        4. If polling times out, increase interval and continue polling (never pause).
        """
        from sibyl.gpu_scheduler import nvidia_smi_query_cmd, gpu_poll_wait_script
        aggressive = self.config.gpu_aggressive_mode
        interval_min = self.config.gpu_poll_interval_sec // 60
        marker_file = project_marker_file(self.ws.name, "gpu_free")
        mode_desc = (f"（流氓模式：<{self.config.gpu_aggressive_threshold_pct}% 显存占用也抢）"
                     if aggressive else "")
        return Action(
            action_type="gpu_poll",
            gpu_poll={
                "ssh_connection": self.config.ssh_server,
                "query_cmd": nvidia_smi_query_cmd(include_total=aggressive),
                "script": gpu_poll_wait_script(
                    ssh_server=self.config.ssh_server,
                    candidate_gpu_ids=list(range(self.config.max_gpus)),
                    threshold_mb=self.config.gpu_free_threshold_mb,
                    poll_interval_sec=self.config.gpu_poll_interval_sec,
                    max_polls=self.config.gpu_poll_max_attempts,
                    marker_file=marker_file,
                    aggressive_mode=aggressive,
                    aggressive_threshold_pct=self.config.gpu_aggressive_threshold_pct,
                ),
                "max_gpus": self.config.max_gpus,
                "threshold_mb": self.config.gpu_free_threshold_mb,
                "interval_sec": self.config.gpu_poll_interval_sec,
                "marker_file": marker_file,
                "aggressive_mode": aggressive,
                "aggressive_threshold_pct": self.config.gpu_aggressive_threshold_pct,
                "max_attempts": self.config.gpu_poll_max_attempts,
            },
            description=(
                f"轮询等待空闲 GPU（最多 {self.config.max_gpus} 张，"
                f"每 {interval_min}min 通过 SSH MCP 检查，"
                f"{'无限等待' if self.config.gpu_poll_max_attempts == 0 else f'最多 {self.config.gpu_poll_max_attempts} 次'}）{mode_desc}"
            ),
            stage=stage,
        )

    def _experiment_wait_action(self, stage: str, running_tasks: list[str],
                                running_gpus: list[int]) -> Action:
        """Return an experiment_wait action when experiments are running.

        Unlike gpu_poll (which waits for FREE GPUs to launch experiments),
        this action monitors RUNNING experiments until they complete.
        The main session should periodically check status via SSH and print
        the experiment status banner, without pausing the project.

        Poll interval is adaptive:
        - Estimate remaining time from experiment_state / gpu_progress
        - Short remaining (<30 min): poll every 2 min
        - Medium remaining (30-120 min): poll every 5 min
        - Long remaining (>120 min): poll every 10 min
        """
        import datetime as _dt_wait
        from sibyl.gpu_scheduler import _load_progress
        from sibyl.experiment_recovery import load_experiment_state

        exp_state = load_experiment_state(self.ws.active_root)
        _, _, running_map, _ = _load_progress(self.ws.active_root)

        # Compute canonical running task list: prefer experiment_state, fallback gpu_progress
        all_running = running_tasks if running_tasks else list(running_map.keys())

        # Guard: if both sources are empty, nothing to wait for
        if not all_running:
            return Action(
                action_type="bash",
                bash_command='echo "experiment_wait: no running tasks detected, ready to advance"',
                description="实验已完成，可以推进",
                stage=stage,
            )

        # Estimate max remaining time across all running tasks
        task_plan_path = self.ws.active_path("plan/task_plan.json")
        task_estimates: dict[str, int] = {}
        if task_plan_path.exists():
            try:
                plan = json.loads(task_plan_path.read_text(encoding="utf-8"))
                for t in plan.get("tasks", []):
                    task_estimates[t["id"]] = t.get("estimated_minutes", 0)
            except (json.JSONDecodeError, OSError):
                pass

        max_remaining_min = 0
        task_status_lines = []
        for tid in all_running:
            est = task_estimates.get(tid, 0)
            started = ""
            gpus = []
            if tid in running_map:
                started = running_map[tid].get("started_at", "")
                gpus = running_map[tid].get("gpu_ids", [])
            elif tid in exp_state.tasks:
                started = exp_state.tasks[tid].get("started_at", "")
                gpus = exp_state.tasks[tid].get("gpu_ids", [])

            elapsed_min = 0
            if started:
                try:
                    start_dt = _dt_wait.datetime.fromisoformat(started)
                    elapsed_min = int(
                        (_dt_wait.datetime.now() - start_dt).total_seconds() / 60
                    )
                except (ValueError, TypeError):
                    pass

            remaining = max(0, est - elapsed_min) if est > 0 else 60  # default 1h if unknown
            max_remaining_min = max(max_remaining_min, remaining)
            gpu_str = ",".join(str(g) for g in gpus) if gpus else "?"
            task_status_lines.append(
                f"{tid} -> GPU[{gpu_str}] (elapsed {elapsed_min}min"
                + (f", ~{remaining}min left" if est > 0 else "")
                + ")"
            )

        # Adaptive poll interval: keep visibility high during long waits.
        # For multi-hour runs like ttt-dlm full-scale jobs, 30min gaps look
        # indistinguishable from "not polling" to the operator.
        if max_remaining_min <= 30:
            poll_interval_sec = 120    # 2 min
        elif max_remaining_min <= 120:
            poll_interval_sec = 300    # 5 min
        else:
            poll_interval_sec = 600    # 10 min

        # Build SSH check commands
        remote_dir = f"{self.config.remote_base}/projects/{self.ws.name}"
        done_checks = " && ".join(
            f'test -f {remote_dir}/exp/results/{tid}_DONE && echo "{tid}:DONE" || echo "{tid}:PENDING"'
            for tid in all_running
        )
        # Also check if processes are alive (via PID files)
        pid_checks = " && ".join(
            f'pid=$(cat {remote_dir}/exp/results/{tid}.pid 2>/dev/null) && '
            f'(ps -p $pid > /dev/null 2>&1 && echo "{tid}:ALIVE:$pid" || echo "{tid}:DEAD:$pid") || '
            f'echo "{tid}:NO_PID"'
            for tid in all_running
        )

        # Progress check (read _PROGRESS.json files)
        progress_checks = " && ".join(
            f'cat {remote_dir}/exp/results/{tid}_PROGRESS.json 2>/dev/null || echo "null"'
            for tid in all_running
        )

        task_detail = "; ".join(task_status_lines[:5])  # cap at 5 for readability
        desc = (
            f"实验运行中（{len(all_running)} 个任务），"
            f"预计剩余 ~{max_remaining_min}min，"
            f"每 {poll_interval_sec // 60}min 轮询一次\n"
            f"  {task_detail}"
        )

        return Action(
            action_type="experiment_wait",
            description=desc,
            stage=stage,
            estimated_minutes=max_remaining_min,
            experiment_monitor={
                "ssh_connection": self.config.ssh_server,
                "check_cmd": done_checks,
                "pid_check_cmd": pid_checks,
                "progress_check_cmd": progress_checks,
                "remote_dir": remote_dir,
                "task_ids": all_running,
                "poll_interval_sec": poll_interval_sec,
                "max_remaining_min": max_remaining_min,
                "task_status": task_status_lines,
                "dynamic_dispatch": True,
                "dispatch_cmd": build_repo_python_cli_command(
                    "dispatch", self.workspace_path,
                ),
                "status_cmd": build_repo_python_cli_command(
                    "experiment_status", self.workspace_path,
                ),
            },
        )

    def _action_result_debate(self, ws: str) -> Action:
        """Agent Team: 6 teammates analyze results from diverse angles, then synthesize.

        Six perspectives ensure thorough result evaluation:
        - Optimist: positive findings, extensions, silver linings
        - Skeptic: statistical concerns, confounds, missing evidence
        - Strategist: next steps, resource allocation, pivot/proceed
        - Methodologist: evaluation protocol audit, reproducibility, baseline fairness
        - Comparativist: SOTA comparison, contribution margin, novelty assessment
        - Revisionist: hypothesis revision, mental model updates, reframing
        """
        # Checkpoint: track per-analyst progress
        result_roles = ["optimist", "skeptic", "strategist", "methodologist",
                        "comparativist", "revisionist"]
        steps = {role: f"idea/result_debate/{role}.md" for role in result_roles}
        cp_info = self._get_or_create_checkpoint("result_debate", steps)

        if cp_info and cp_info["all_complete"]:
            return Action(
                action_type="bash",
                bash_command="echo 'All result analyses already written (checkpoint valid)'",
                description="所有结果分析已完成（checkpoint 校验通过），可直接 record",
                stage="result_debate",
                checkpoint_info=cp_info,
            )

        remaining = set(cp_info["remaining_steps"]) if cp_info else set(result_roles)

        team_prompt = (
            f"Create an agent team to debate experiment results.\n\n"
            f"Workspace: {ws}\n"
            f"Read experiment results from {ws}/exp/results/\n\n"
            f"Spawn teammates for remaining perspectives:\n"
            + "\n".join(f"- {role}" for role in result_roles if role in remaining) + "\n\n"
            f"Have them debate each other's positions. The skeptic and methodologist "
            f"should challenge the optimist's claims. The comparativist grounds the "
            f"discussion in external context. The revisionist updates our mental model. "
            f"The strategist synthesizes into actionable next steps.\n\n"
            f"Each teammate writes analysis to {ws}/idea/result_debate/ROLE.md\n"
            f"Run exactly {self.config.debate_rounds} debate rounds before the strategist synthesizes the final view.\n"
            f"{non_paper_output_requirement(self.config.language)}"
        )

        all_teammates = [
            {"name": "optimist", "skill": "sibyl-optimist", "args": ws},
            {"name": "skeptic", "skill": "sibyl-skeptic", "args": ws},
            {"name": "strategist", "skill": "sibyl-strategist", "args": ws},
            {"name": "methodologist", "skill": "sibyl-methodologist", "args": ws},
            {"name": "comparativist", "skill": "sibyl-comparativist", "args": ws},
            {"name": "revisionist", "skill": "sibyl-revisionist", "args": ws},
        ]
        teammates = [t for t in all_teammates if t["name"] in remaining]

        post_steps = [
            {"type": "skill", "skill": "sibyl-result-synthesizer", "args": ws},
        ]
        if self.config.codex_enabled:
            post_steps.append({
                "type": "codex",
                "skill": "sibyl-codex-reviewer",
                "args": self._codex_reviewer_args("result_debate", ws),
            })

        team_dict = {
            "team_name": "sibyl-result-debate",
            "teammates": teammates,
            "post_steps": post_steps,
            "prompt": team_prompt,
        }

        return Action(
            action_type="team",
            team=team_dict,
            description=f"Agent Team: {len(teammates)}人辩论实验结果"
                        + (f"（恢复：已完成 {len(cp_info['completed_steps'])}/6）"
                           if cp_info and cp_info["resuming"] else
                           "（乐观者+怀疑论者+战略家+方法论者+比较分析者+修正主义者）")
                        + " → 综合裁决"
                        + (" + Codex 独立审查" if self.config.codex_enabled else ""),
            stage="result_debate",
            checkpoint_info=cp_info,
        )

    def _action_experiment_decision(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-supervisor-decision", "args": ws}],
            description="Supervisor analyzes results and decides PIVOT or PROCEED",
            stage="experiment_decision",
        )

    def _action_writing_outline(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-outline-writer", "args": ws}],
            description="Generate paper outline",
            stage="writing_outline",
        )

    def _action_writing_sections(self, ws: str) -> Action:
        mode = self.config.writing_mode
        codex_fallback = mode == "codex" and not self.config.codex_enabled
        if codex_fallback:
            mode = "parallel"

        # Checkpoint: track per-section progress (all writing modes)
        steps = {sid: f"writing/sections/{sid}.md" for sid, _ in PAPER_SECTIONS}
        cp_info = self._get_or_create_checkpoint("writing_sections", steps)

        if cp_info and cp_info["all_complete"]:
            return Action(
                action_type="bash",
                bash_command="echo 'All sections already written (checkpoint valid)'",
                description="所有章节已完成（checkpoint 校验通过），可直接 record",
                stage="writing_sections",
                checkpoint_info=cp_info,
            )

        if mode == "sequential":
            action = Action(
                action_type="skill",
                skills=[{"name": "sibyl-sequential-writer", "args": ws}],
                description="顺序撰写论文各章节（确保行文一致性）",
                stage="writing_sections",
                checkpoint_info=cp_info,
            )
            return action
        elif mode == "codex":
            return Action(
                action_type="skill",
                skills=[{"name": "sibyl-codex-writer", "args": self._codex_writer_args(ws)}],
                description="使用 Codex (GPT-5) 撰写论文各章节",
                stage="writing_sections",
                checkpoint_info=cp_info,
            )
        else:  # "parallel" — 保留现有 team 模式
            remaining = set(cp_info["remaining_steps"]) if cp_info else None
            sections_info = "\n".join(
                f"- {name} (section id: {sid}): write to {ws}/writing/sections/{sid}.md"
                for sid, name in PAPER_SECTIONS
                if remaining is None or sid in remaining
            )
            team_prompt = (
                f"Create an agent team to write paper sections in parallel.\n\n"
                f"Workspace: {ws}\n"
                f"Read outline from {ws}/writing/outline.md\n"
                f"Read experiment results from {ws}/exp/results/\n\n"
                f"Spawn teammates for remaining sections:\n{sections_info}\n\n"
                f"Teammates should coordinate for consistency — share key definitions, "
                f"notation, and cross-references between sections.\n"
                f"{paper_writing_requirement()}"
            )
            teammates = [
                {
                    "name": f"writer-{sid}",
                    "skill": "sibyl-section-writer",
                    "args": pack_skill_args(ws, name, sid),
                }
                for sid, name in PAPER_SECTIONS
                if remaining is None or sid in remaining
            ]
            team_dict = {
                "team_name": "sibyl-writing-sections",
                "teammates": teammates,
                "post_steps": [],
                "prompt": team_prompt,
            }
            return Action(
                action_type="team",
                team=team_dict,
                description=f"Agent Team: {len(teammates)}人并行撰写论文章节"
                            + ("（Codex 未启用，已自动回退）" if codex_fallback else "")
                            + (f"（恢复：已完成 {len(cp_info['completed_steps'])}/6）"
                               if cp_info and cp_info["resuming"] else ""),
                stage="writing_sections",
                checkpoint_info=cp_info,
            )

    def _action_writing_critique(self, ws: str) -> Action:
        # Checkpoint: track per-section critique progress
        steps = {sid: f"writing/critique/{sid}_critique.md" for sid, _ in PAPER_SECTIONS}
        cp_info = self._get_or_create_checkpoint("writing_critique", steps)

        if cp_info and cp_info["all_complete"]:
            return Action(
                action_type="bash",
                bash_command="echo 'All critiques already written (checkpoint valid)'",
                description="所有批评已完成（checkpoint 校验通过），可直接 record",
                stage="writing_critique",
                checkpoint_info=cp_info,
            )

        remaining = set(cp_info["remaining_steps"]) if cp_info else None
        sections_info = "\n".join(
            f"- Critic for {name}: read {ws}/writing/sections/{sid}.md, "
            f"write critique to {ws}/writing/critique/{sid}_critique.md"
            for sid, name in PAPER_SECTIONS
            if remaining is None or sid in remaining
        )
        team_prompt = (
            f"Create an agent team to critique paper sections.\n\n"
            f"Workspace: {ws}\n\n"
            f"Spawn teammates for remaining critiques:\n{sections_info}\n\n"
            f"Critics should cross-reference other sections for consistency issues. "
            f"Score each section 1-10 and provide specific improvement suggestions.\n"
            f"{paper_writing_requirement()}"
        )
        teammates = [
            {
                "name": f"critic-{sid}",
                "skill": "sibyl-section-critic",
                "args": pack_skill_args(ws, name, sid),
            }
            for sid, name in PAPER_SECTIONS
            if remaining is None or sid in remaining
        ]
        team_dict = {
            "team_name": "sibyl-writing-critique",
            "teammates": teammates,
            "post_steps": [],
            "prompt": team_prompt,
        }
        return Action(
            action_type="team",
            team=team_dict,
            description=f"Agent Team: {len(teammates)}人并行批评论文章节"
                        + (f"（恢复：已完成 {len(cp_info['completed_steps'])}/6）"
                           if cp_info and cp_info["resuming"] else ""),
            stage="writing_critique",
            checkpoint_info=cp_info,
        )

    def _action_writing_integrate(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-editor", "args": ws}],
            description="Integrate all sections into coherent paper",
            stage="writing_integrate",
        )

    def _action_writing_final_review(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-final-critic", "args": ws}],
            description="Top-tier conference-level paper review",
            stage="writing_final_review",
        )

    def _action_writing_latex(self, ws: str) -> Action:
        return Action(
            action_type="skill",
            skills=[{
                "name": "sibyl-latex-writer",
                "args": pack_skill_args(ws, self.config.ssh_server, self.config.remote_base),
            }],
            description="将论文转为 NeurIPS LaTeX 格式并编译 PDF",
            stage="writing_latex",
        )

    def _action_review(self, ws: str) -> Action:
        """Parallel review: critic + supervisor + optional codex."""
        skills = [
            {"name": "sibyl-critic", "args": ws},
            {"name": "sibyl-supervisor", "args": ws},
        ]
        if self.config.codex_enabled:
            skills.append({"name": "sibyl-codex-reviewer", "args": self._codex_reviewer_args("review", ws)})
        return Action(
            action_type="skills_parallel",
            skills=skills,
            description="并行审查：批评 + 监督" + (" + Codex" if self.config.codex_enabled else ""),
            stage="review",
        )

    def _action_reflection(self, ws: str, iteration: int) -> Action:
        return Action(
            action_type="skill",
            skills=[{"name": "sibyl-reflection", "args": pack_skill_args(ws, iteration)}],
            description="Reflection agent: classify issues, generate improvement plan and lessons",
            stage="reflection",
        )


    def _is_pipeline_done(self) -> tuple[bool, float, float, int, int]:
        """Determine if the pipeline should terminate.

        Returns (is_done, score, threshold, max_iters, iteration).
        """
        score, threshold, max_iters = self._parse_quality_gate_params()
        iteration = self.ws.get_status().iteration
        done = (score >= threshold and iteration >= 2) or iteration >= max_iters
        return done, score, threshold, max_iters, iteration

    def _action_quality_gate(self) -> Action:
        """Pure computation — no side effects. Side effects in _get_next_stage."""
        is_done, score, threshold, max_iters, iteration = self._is_pipeline_done()
        action_plan = _load_workspace_action_plan(self.ws, persist_normalized=True) or {}
        trajectory = action_plan.get("quality_trajectory", "")
        focus = ""
        recommended_focus = action_plan.get("recommended_focus", [])
        if recommended_focus:
            focus = str(recommended_focus[0])[:120]
        extra_parts = []
        if trajectory:
            extra_parts.append(f"trajectory={trajectory}")
        if focus:
            extra_parts.append(f"focus={focus}")
        extra = f" ({'; '.join(extra_parts)})" if extra_parts else ""

        if is_done:
            return Action(
                action_type="done",
                description=(
                    f"Pipeline complete (score={score}, threshold={threshold}, "
                    f"iter={iteration}/{max_iters}).{extra}"
                ),
                stage="done",
            )
        else:
            return Action(
                action_type="bash",
                bash_command=f"echo 'Starting iteration {iteration + 1}'",
                description=(
                    f"Quality gate: score={score} < {threshold}, "
                    f"starting iteration {iteration + 1}{extra}"
                ),
                stage="quality_gate",
            )

    # ══════════════════════════════════════════════
    # Post-reflection hook (processes reflection agent outputs)
    # ══════════════════════════════════════════════

    def _post_reflection_hook(self):
        """Process reflection agent outputs: log iteration, record evolution, generate overlay."""
        from sibyl.reflection import IterationLogger
        from sibyl.evolution import EvolutionEngine, IssueCategory, normalize_issue_entry

        iteration = self.ws.get_status().iteration
        logger = IterationLogger(self.ws.root)

        # Read reflection agent's structured output and normalize it back onto disk
        action_plan = _load_workspace_action_plan(
            self.ws,
            "reflection/action_plan.json",
            persist_normalized=True,
        ) or {}
        classified_issues = [
            issue
            for issue in action_plan.get("issues_classified", [])
            if issue.get("status") != "fixed"
        ]
        success_patterns = action_plan.get("success_patterns", [])
        issues_fixed = list(action_plan.get("issues_fixed", []))
        quality_trajectory = action_plan.get("quality_trajectory", "stagnant")

        # Fallback: read supervisor issues if reflection agent didn't produce classified issues
        if not classified_issues:
            issues_raw = self.ws.read_file("supervisor/issues.json")
            if issues_raw:
                try:
                    issues_data = json.loads(issues_raw)
                    for issue in issues_data:
                        normalized_issue = normalize_issue_entry(
                            {
                                "description": issue.get("description", ""),
                                "category": IssueCategory.classify(
                                    issue.get("description", "")
                                ).value,
                                "severity": issue.get("severity", "medium"),
                                "status": issue.get("status", "new"),
                            }
                        )
                        if normalized_issue is not None and normalized_issue.get("status") != "fixed":
                            classified_issues.append(normalized_issue)
                except (json.JSONDecodeError, TypeError):
                    pass

        issues_found = [
            issue.get("description", "")
            for issue in classified_issues
            if issue.get("description")
        ]

        # Detect issues_fixed: compare with previous iteration's issues
        prev_plan = _load_workspace_action_plan(
            self.ws,
            "reflection/prev_action_plan.json",
        ) or {}
        prev_issues_by_key: dict[str, str] = {}
        for issue in prev_plan.get("issues_classified", []):
            if issue.get("status") == "fixed":
                continue
            issue_key = str(issue.get("issue_key", "")).strip()
            description = str(issue.get("description", "")).strip()
            if issue_key and description and issue_key not in prev_issues_by_key:
                prev_issues_by_key[issue_key] = description
        current_issue_keys = {
            str(issue.get("issue_key", "")).strip()
            for issue in classified_issues
            if issue.get("issue_key")
        }
        inferred_fixed = [
            description
            for issue_key, description in prev_issues_by_key.items()
            if issue_key not in current_issue_keys
        ]
        issues_fixed = list(dict.fromkeys([*issues_fixed, *inferred_fixed]))

        # Extract score
        supervisor_review = self.ws.read_file("supervisor/review_writing.md") or ""
        score = 5.0
        score_match = re.search(r'(?:score|rating|quality)[:\s]*(\d+(?:\.\d+)?)(?!\w)',
                                supervisor_review, re.IGNORECASE)
        if score_match:
            score = min(max(float(score_match.group(1)), 0.0), 10.0)

        # 1. Log iteration with classified issues and fixed tracking
        try:
            logger.log_iteration(
                iteration=iteration,
                stage="reflection",
                changes=[f"Iteration {iteration} complete"],
                issues_found=issues_found[:10],
                issues_fixed=issues_fixed[:10],
                quality_score=score,
                notes=json.dumps(
                    {
                        "classified_issues": classified_issues[:10],
                        "quality_trajectory": quality_trajectory,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            self.ws.add_error(f"Reflection logging failed: {e}")

        # 2. Research diary
        try:
            critic_feedback = self.ws.read_file("critic/critique_writing.md") or ""
            reflection_md = self.ws.read_file("reflection/reflection.md") or ""
            fixed_str = f"**Fixed**: {len(issues_fixed)}\n" if issues_fixed else ""
            trajectory_str = f"**Trajectory**: {quality_trajectory}\n" if quality_trajectory else ""
            diary_entry = (
                f"# Iteration {iteration}\n\n"
                f"**Score**: {score}/10\n"
                f"**Issues**: {len(issues_found)}\n"
                f"{fixed_str}"
                f"{trajectory_str}\n"
                f"## Reflection\n{reflection_md[:1000]}\n\n"
                f"## Review Summary\n{supervisor_review[:500]}\n\n"
                f"## Critique Summary\n{critic_feedback[:500]}\n"
            )
            existing_diary = self.ws.read_file("logs/research_diary.md") or ""
            self.ws.write_file("logs/research_diary.md", existing_diary + "\n\n" + diary_entry)
        except Exception as e:
            self.ws.add_error(f"Diary update failed: {e}")

        # 2b. Emit iteration_complete event
        try:
            el = EventLogger(self.ws.root)
            el.iteration_complete(
                iteration=iteration, score=score,
                issues_count=len(issues_found),
            )
        except Exception:
            pass

        # 3. Evolution recording — pass classified_issues directly for proper agent routing
        try:
            if self.config.evolution_enabled:
                engine = EvolutionEngine()
                engine.record_outcome(
                    project=self.ws.name,
                    stage="reflection",
                    issues=issues_found,
                    score=score,
                    notes=f"Iteration {iteration}; trajectory={quality_trajectory}",
                    classified_issues=classified_issues[:10],
                    success_patterns=success_patterns[:10],
                )
                engine.generate_lessons_overlay()
        except Exception as e:
            self.ws.add_error(f"Evolution recording failed: {e}")

        # 4. Quality trend — write project-level trend for reflection agent
        try:
            if self.config.evolution_enabled:
                engine = EvolutionEngine()
                trend = engine.get_quality_trend(project=self.ws.name)
                if trend:
                    trend_lines = ["# 质量趋势\n"]
                    for entry in trend[-10:]:  # last 10 entries
                        trend_lines.append(
                            f"- {entry['timestamp']}: score={entry['score']}"
                        )
                    scores = [e["score"] for e in trend]
                    if len(scores) >= 2:
                        delta = scores[-1] - scores[-2]
                        direction = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
                        trend_lines.append(f"\n趋势: {direction} (Δ={delta:+.1f})")
                    self.ws.write_file("logs/quality_trend.md", "\n".join(trend_lines))
        except Exception as e:
            self.ws.add_error(f"Quality trend recording failed: {e}")

        # 5. Self-check diagnostics — auto-evaluate system health
        try:
            if self.config.evolution_enabled:
                engine = EvolutionEngine()
                diagnostics = engine.get_self_check_diagnostics(project=self.ws.name)
                if diagnostics:
                    self.ws.write_file(
                        "logs/self_check_diagnostics.json",
                        json.dumps(diagnostics, indent=2, ensure_ascii=False),
                    )
                else:
                    # Clear stale diagnostics if system is healthy
                    diag_path = self.ws.project_path("logs/self_check_diagnostics.json")
                    if diag_path.exists():
                        diag_path.unlink()
        except Exception as e:
            self.ws.add_error(f"Self-check diagnostics failed: {e}")

    # ══════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════

    def _parse_quality_gate_params(self) -> tuple[float, float, int]:
        """Parse quality gate parameters from supervisor review and reflection action plan.

        Returns (score, threshold, max_iters).
        """
        review = self.ws.read_file("supervisor/review_writing.md") or ""
        match = re.search(r"(?:score|rating|quality)[:\s]*(\d+(?:\.\d+)?)(?!\w)",
                          review, re.IGNORECASE)
        score = min(max(float(match.group(1)), 0.0), 10.0) if match else 5.0
        threshold = 8.0
        max_iters = self.config.max_iterations
        max_iters_cap = self.config.max_iterations_cap
        if max_iters_cap > 0:
            max_iters_cap = max(max_iters_cap, max_iters)
        action_plan = _load_workspace_action_plan(self.ws, persist_normalized=True)
        if action_plan:
            v = action_plan.get("suggested_threshold_adjustment")
            if isinstance(v, (int, float)) and 1.0 <= v <= 10.0:
                threshold = float(v)
            v = action_plan.get("suggested_max_iterations")
            if (
                isinstance(v, int)
                and v >= 2
                and (max_iters_cap <= 0 or v <= max_iters_cap)
            ):
                max_iters = v
        return score, threshold, max_iters

    def _get_next_stage(self, current_stage: str, result: str = "",
                        score: float | None = None) -> tuple[str, int | None]:
        """Determine the next stage based on current stage and result.

        Returns (next_stage, new_iteration). new_iteration is non-None only
        when the quality gate loops back for a new iteration.
        """
        return self._natural_next_stage(current_stage, result, score)

    def _natural_next_stage(self, current_stage: str, result: str = "",
                            score: float | None = None) -> tuple[str, int | None]:
        """Compute the next stage.

        Contains all branching logic: experiment loops, PIVOT, writing
        revisions, quality gate side effects, etc.
        """
        # experiment_decision: PIVOT loops back to idea_debate
        if current_stage == "experiment_decision":
            decision = self.ws.read_file("supervisor/experiment_analysis.md")
            if decision is None:
                self.ws.add_error("PIVOT check: supervisor/experiment_analysis.md not found")
                decision = ""
            if "DECISION: PIVOT" in decision.upper():
                cycle = self._get_current_cycle()
                if cycle < self.config.idea_exp_cycles:
                    iteration = self.ws.get_status().iteration
                    self.ws.write_file(
                        f"logs/idea_exp_cycle_{cycle + 1}.marker",
                        f"PIVOT at iteration {iteration}",
                    )
                    self._prepare_idea_refinement_round(
                        f"experiment_decision pivot round {cycle + 1}"
                    )
                    return ("idea_debate", None)
                else:
                    self.ws.add_error(
                        f"PIVOT requested but cycle limit reached ({cycle}/{self.config.idea_exp_cycles})"
                    )

        if current_stage == "idea_validation_decision":
            payload = self._load_idea_validation_decision()
            decision = str(payload.get("decision", "ADVANCE")).upper() or "ADVANCE"
            selected_candidate_id = str(payload.get("selected_candidate_id", "")).strip()
            if decision not in {"ADVANCE", "REFINE", "PIVOT"}:
                self.ws.add_error(
                    f"Unknown idea validation decision '{decision}', falling back to ADVANCE"
                )
                decision = "ADVANCE"

            if decision in {"REFINE", "PIVOT"}:
                validation_round = self._get_current_validation_round()
                if (
                    self.config.idea_validation_rounds > 0
                    and validation_round >= self.config.idea_validation_rounds
                ):
                    self.ws.add_error(
                        "Idea validation requested more refinement rounds than allowed "
                        f"({validation_round}/{self.config.idea_validation_rounds}); "
                        "advancing with current best candidate"
                    )
                else:
                    next_round = validation_round + 1
                    self.ws.write_file(
                        f"logs/idea_validation_round_{next_round}.marker",
                        (
                            f"{decision} after pilot validation round {next_round} "
                            f"(selected={selected_candidate_id or 'none'})"
                        ),
                    )
                    self._prepare_idea_refinement_round(
                        f"idea_validation_decision {decision.lower()} round {next_round}"
                    )
                    return ("idea_debate", None)

            self._apply_candidate_selection(selected_candidate_id)
            return ("experiment_cycle", None)

        # experiment stages: loop if more batches remain OR tasks still running
        if current_stage in ("pilot_experiments", "experiment_cycle"):
            from sibyl.gpu_scheduler import get_batch_info, get_running_gpu_ids
            exp_mode = "PILOT" if current_stage == "pilot_experiments" else "FULL"
            # Check experiment_state.json for running tasks (authoritative)
            from sibyl.experiment_recovery import (
                load_experiment_state, get_running_tasks as get_exp_running,
            )
            exp_state = load_experiment_state(self.ws.active_root)
            exp_running = get_exp_running(exp_state)
            if exp_running:
                return (current_stage, None)  # wait for running tasks
            # Check if any tasks are still running
            running_gpus = get_running_gpu_ids(self.ws.active_root)
            if running_gpus:
                return (current_stage, None)  # wait for running tasks
            info = get_batch_info(
                self.ws.active_root, list(range(self.config.max_gpus)), exp_mode,
                gpus_per_task=self.config.gpus_per_task,
            )
            if info is not None and len(info["batch"]) > 0:
                return (current_stage, None)  # stay in current stage for next batch

        # writing_final_review: low score loops back to writing_integrate (with round limit)
        if current_stage == "writing_final_review":
            review = self.ws.read_file("writing/review.md") or ""
            match = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)", review, re.IGNORECASE)
            review_score = float(match.group(1)) if match else 5.0
            critique_dir = self.ws.active_path("writing/critique")
            if critique_dir.exists():
                revision_rounds = len([
                    f for f in critique_dir.iterdir()
                    if f.is_file() and f.name.startswith("revision_round_")
                ])
            else:
                revision_rounds = 0
            max_revisions = self.config.writing_revision_rounds
            if review_score < 7.0 and revision_rounds < max_revisions:
                self.ws.write_file(
                    f"writing/critique/revision_round_{revision_rounds + 1}.marker",
                    f"Revision round {revision_rounds + 1}, score={review_score}",
                )
                return ("writing_integrate", None)

        # init is transient: always advance to literature_search
        if current_stage == "init":
            return ("literature_search", None)

        # lark stages: skip if lark disabled (direct pipeline path)
        if current_stage == "reflection" and not self.config.lark_enabled:
            return ("quality_gate", None)

        if current_stage == "writing_latex" and not self.config.review_enabled:
            return ("reflection", None)

        if current_stage == "pilot_experiments":
            self._reset_experiment_runtime_state()
            if self.config.idea_validation_rounds > 0:
                return ("idea_validation_decision", None)
            return ("experiment_cycle", None)

        # quality_gate: execute side effects and determine next stage
        if current_stage == "quality_gate":
            is_done, qg_score, threshold, max_iters, iteration = self._is_pipeline_done()
            if is_done:
                self.ws.git_tag(
                    f"v{iteration}",
                    f"Iteration {iteration} complete, score={qg_score}",
                )
                return ("done", None)
            else:
                self.ws.git_tag(
                    f"iter-{iteration}",
                    f"End of iteration {iteration}, score={qg_score}",
                )
                try:
                    self.ws.archive_iteration(iteration)
                except OSError as e:
                    self.ws.add_error(f"Archive failed for iteration {iteration}: {e}")
                if self.ws.get_status().iteration_dirs:
                    self.ws.start_new_iteration(iteration + 1)
                else:
                    self._clear_iteration_artifacts(iteration)
                return ("literature_search", iteration + 1)

        try:
            idx = self.STAGES.index(current_stage)
            if idx + 1 < len(self.STAGES):
                return (self.STAGES[idx + 1], None)
        except ValueError:
            self.ws.add_error(f"Unknown stage '{current_stage}', forcing done")
            return ("done", None)
        return (current_stage, None)

    def _clear_iteration_artifacts(self, iteration: int = 0):
        """Clear stale working-directory artifacts between iterations.

        Called after archive_iteration to prevent data pollution
        (e.g., revision markers, supervisor scores) from leaking into the next iteration.

        Preserves: reflection/lessons_learned.md (carried forward for next iteration's agents)
        """
        import shutil

        # Preserve lessons_learned.md before clearing reflection/
        lessons_path = self.ws.active_path("reflection/lessons_learned.md")
        lessons_content = None
        if lessons_path.exists():
            lessons_content = lessons_path.read_text(encoding="utf-8")

        # Preserve action_plan.json for issues_fixed tracking
        action_plan_path = self.ws.active_path("reflection/action_plan.json")
        action_plan_content = None
        if action_plan_path.exists():
            action_plan_content = action_plan_path.read_text(encoding="utf-8")

        dirs_to_clear = [
            "idea/perspectives", "idea/debate", "idea/result_debate",
            "plan",
            "writing/sections", "writing/critique",
            "supervisor", "critic", "reflection",
        ]
        for subdir in dirs_to_clear:
            target = self.ws.active_path(subdir)
            if target.exists():
                try:
                    shutil.rmtree(target)
                    target.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass  # best-effort cleanup
        # Clear GPU progress tracking
        gpu_progress = self.ws.active_path("exp/gpu_progress.json")
        if gpu_progress.exists():
            try:
                gpu_progress.unlink()
            except OSError:
                pass
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
        # Clear idea iteration markers (per-iteration budgets)
        logs_dir = self.ws.project_path("logs")
        if logs_dir.exists():
            for marker in logs_dir.glob("idea_exp_cycle_*.marker"):
                try:
                    marker.unlink()
                except OSError:
                    pass
            for marker in logs_dir.glob("idea_validation_round_*.marker"):
                try:
                    marker.unlink()
                except OSError:
                    pass

        # Clear checkpoint files
        for cp_dir in CHECKPOINT_DIRS.values():
            self.ws.clear_checkpoint(cp_dir)

        # Restore preserved files for next iteration
        if lessons_content:
            self.ws.write_file("reflection/lessons_learned.md", lessons_content)
        if action_plan_content:
            self.ws.write_file("reflection/prev_action_plan.json", action_plan_content)

    def _reset_experiment_runtime_state(self):
        """Clear transient experiment scheduler state before the full stage."""
        gpu_progress = self.ws.active_path("exp/gpu_progress.json")
        if gpu_progress.exists():
            try:
                gpu_progress.unlink()
            except OSError:
                pass

        results_dir = self.ws.active_path("exp/results")
        if results_dir.exists():
            for marker in results_dir.glob("*_DONE"):
                try:
                    marker.unlink()
                except OSError:
                    pass

        for suffix in ("exp_monitor", "gpu_free"):
            marker_path = Path(project_marker_file(self.ws.name, suffix))
            try:
                marker_path.unlink()
            except OSError:
                pass

        exp_state_path = self.ws.active_path("exp/experiment_state.json")
        if exp_state_path.exists():
            try:
                exp_state_path.unlink()
            except OSError:
                pass

    def _get_current_cycle(self) -> int:
        """Get current idea-experiment cycle number."""
        logs_dir = self.ws.project_path("logs")
        if not logs_dir.exists():
            return 0
        return len(list(logs_dir.glob("idea_exp_cycle_*.marker")))

    def _get_current_validation_round(self) -> int:
        """Get current pilot-guided idea refinement round count."""
        logs_dir = self.ws.project_path("logs")
        if not logs_dir.exists():
            return 0
        return len(list(logs_dir.glob("idea_validation_round_*.marker")))

    def _prepare_idea_refinement_round(self, reason: str) -> None:
        """Clear idea-debate transient artifacts so a refinement round can rerun."""
        for subdir in ("idea/perspectives", "idea/debate"):
            target = self.ws.active_path(subdir)
            try:
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        cp_dir = CHECKPOINT_DIRS.get("idea_debate")
        if cp_dir:
            self.ws.clear_checkpoint(cp_dir)
        self.ws.write_file("logs/idea_refinement_state.txt", reason)

    def _load_json_artifact(self, relative_path: str) -> dict | None:
        """Best-effort JSON loader for workspace artifacts."""
        content = self.ws.read_file(relative_path)
        if not content:
            return None
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            self.ws.add_error(f"Failed to parse JSON artifact: {relative_path}")
            return None
        return data if isinstance(data, dict) else None

    def _load_idea_validation_decision(self) -> dict:
        """Load idea validation decision with JSON-first, markdown-fallback parsing."""
        payload = self._load_json_artifact("supervisor/idea_validation_decision.json") or {}

        decision = str(payload.get("decision", "")).upper()
        if decision in {"ADVANCE", "REFINE", "PIVOT"}:
            return payload

        content = self.ws.read_file("supervisor/idea_validation_decision.md") or ""
        match = re.search(r"DECISION:\s*(ADVANCE|REFINE|PIVOT)", content, re.IGNORECASE)
        if match:
            payload["decision"] = match.group(1).upper()
        selected = re.search(
            r"SELECTED_CANDIDATE:\s*([A-Za-z0-9_.-]+)", content, re.IGNORECASE
        )
        if selected and "selected_candidate_id" not in payload:
            payload["selected_candidate_id"] = selected.group(1)
        confidence = re.search(r"CONFIDENCE:\s*(\d+(?:\.\d+)?)", content, re.IGNORECASE)
        if confidence and "confidence" not in payload:
            payload["confidence"] = float(confidence.group(1))
        return payload

    def _task_matches_candidate(self, task: dict, selected_candidate_id: str) -> bool:
        """Return True when a task should survive candidate selection."""
        if not selected_candidate_id:
            return True
        candidate_id = task.get("candidate_id")
        if not candidate_id:
            return True
        if isinstance(candidate_id, list):
            return (
                selected_candidate_id in candidate_id
                or "shared" in candidate_id
            )
        return candidate_id in {selected_candidate_id, "shared"}

    def _apply_candidate_selection(self, selected_candidate_id: str) -> None:
        """Filter task_plan.json down to the chosen candidate plus shared tasks."""
        if not selected_candidate_id:
            return

        task_plan_path = self.ws.active_path("plan/task_plan.json")
        if not task_plan_path.exists():
            return

        try:
            plan = json.loads(task_plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.ws.add_error("Failed to parse plan/task_plan.json during candidate selection")
            return

        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list) or not tasks:
            return

        filtered = [
            task for task in tasks
            if isinstance(task, dict) and self._task_matches_candidate(task, selected_candidate_id)
        ]
        if not filtered:
            self.ws.add_error(
                "Candidate selection produced an empty task plan; keeping the original plan"
            )
            return

        kept_ids = {task.get("id") for task in filtered if task.get("id")}
        for task in filtered:
            deps = task.get("depends_on")
            if isinstance(deps, list):
                task["depends_on"] = [dep for dep in deps if dep in kept_ids]

        plan["tasks"] = filtered
        self.ws.write_file(
            "plan/task_plan.json",
            json.dumps(plan, indent=2, ensure_ascii=False),
        )
        self.ws.write_file(
            "plan/selected_candidate.json",
            json.dumps(
                {
                    "selected_candidate_id": selected_candidate_id,
                    "kept_task_count": len(filtered),
                },
                indent=2,
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _slugify(text: str) -> str:
        slug = text.lower().strip()
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        return slug[:60]


# ══════════════════════════════════════════════
# CLI helpers for Bash invocation
# ══════════════════════════════════════════════

def cli_init(topic: str, project_name: str | None = None,
             config_path: str | None = None):
    """CLI: Initialize a project."""
    result = FarsOrchestrator.init_project(topic, project_name, config_path)
    print(json.dumps(result, indent=2))
    try:
        el = EventLogger(Path(result["workspace_path"]))
        el.project_init(topic=topic, project_name=result.get("project_name", ""))
    except Exception:
        pass


def _write_sentinel_heartbeat(workspace_path: str, stage: str, action: str):
    """Write heartbeat file for Sentinel watchdog (best-effort)."""
    hb_path = Path(workspace_path) / "sentinel_heartbeat.json"
    hb_path.write_text(json.dumps({
        "ts": time.time(),
        "stage": stage,
        "action": action,
    }))


_LOOP_ACTION_TYPES = {"experiment_wait", "gpu_poll"}


def _write_breadcrumb(workspace_path: str, action_dict: dict | None = None,
                      stage: str = "", completed: bool = False):
    """Write breadcrumb file for context recovery after compaction/restart.

    Called by cli_next (with action_dict) and cli_record (with completed=True).
    """
    bc_path = Path(workspace_path) / "breadcrumb.json"
    if action_dict:
        action_type = action_dict.get("action_type", "")
        bc = {
            "ts": time.time(),
            "stage": action_dict.get("stage", stage),
            "action_type": action_type,
            "iteration": action_dict.get("iteration", 0),
            "workspace_path": workspace_path,
            "in_loop": action_type in _LOOP_ACTION_TYPES,
            "loop_type": action_type if action_type in _LOOP_ACTION_TYPES else "",
            "description": action_dict.get("description", "")[:200],
        }
    else:
        # cli_record: stage completed, no longer in a loop
        bc = {
            "ts": time.time(),
            "stage": stage,
            "action_type": "completed",
            "workspace_path": workspace_path,
            "in_loop": False,
            "loop_type": "",
            "description": f"Stage '{stage}' completed, advancing to next",
        }
    bc_path.write_text(json.dumps(bc, ensure_ascii=False))


def cli_next(workspace_path: str):
    """CLI: Get next action."""
    o = FarsOrchestrator(workspace_path)
    action = o.get_next_action()
    print(json.dumps(action, indent=2))
    try:
        _write_sentinel_heartbeat(workspace_path, action.get("stage", ""), "cli_next")
        _write_breadcrumb(workspace_path, action_dict=action)
        # Emit stage_start event (best-effort)
        action_type = action.get("action_type", "")
        if action_type not in ("done", "stopped", "gpu_poll", "experiment_wait"):
            el = EventLogger(Path(workspace_path))
            el.stage_start(
                stage=action.get("stage", ""),
                iteration=action.get("iteration", 0),
                action_type=action_type,
                description=action.get("description", "")[:200],
            )
    except Exception:
        pass  # Heartbeat + breadcrumb + events are best-effort


def cli_record(workspace_path: str, stage: str, result: str = "",
               score: float | None = None):
    """CLI: Record stage result."""
    o = FarsOrchestrator(workspace_path)
    # Capture stage_started_at before advancing
    prev_status = o.ws.get_status()
    stage_started_at = prev_status.stage_started_at

    o.record_result(stage, result, score)
    new_status = o.ws.get_status()
    output = {"status": "ok", "new_stage": new_status.stage}
    # Signal main session to launch background sync agent
    _NO_SYNC_TRIGGER = {"init", "quality_gate", "done", "lark_sync"}
    if o.config.lark_enabled and stage not in _NO_SYNC_TRIGGER:
        output["sync_requested"] = True
    print(json.dumps(output))
    try:
        _write_sentinel_heartbeat(workspace_path, stage, "cli_record")
        _write_breadcrumb(workspace_path, stage=stage, completed=True)
        # Emit stage_end event (best-effort)
        el = EventLogger(Path(workspace_path))
        duration = (time.time() - stage_started_at) if stage_started_at else None
        el.stage_end(
            stage=stage,
            iteration=prev_status.iteration,
            duration_sec=duration,
            score=score,
            next_stage=new_status.stage,
        )
    except Exception:
        pass


def cli_pause(workspace_path: str, reason: str = "rate_limit"):
    """CLI: Write a legacy pause marker or manual stop marker."""
    o = FarsOrchestrator(workspace_path)
    o.ws.pause(reason)
    status = o.ws.get_status()
    status_value = "stopped" if reason == "user_stop" else "paused"
    print(json.dumps({"status": status_value, "stage": status.stage}))
    try:
        EventLogger(Path(workspace_path)).pause(
            reason=reason, stage=status.stage, iteration=status.iteration)
    except Exception:
        pass


def cli_resume(workspace_path: str):
    """CLI: Clear stop/pause markers and resume a project."""
    o = FarsOrchestrator(workspace_path)
    o.ws.resume()
    status = o.ws.get_status()
    print(json.dumps({"status": "resumed", "stage": status.stage}))
    try:
        EventLogger(Path(workspace_path)).resume(
            stage=status.stage, iteration=status.iteration)
    except Exception:
        pass


def cli_status(workspace_path: str):
    """CLI: Get project status."""
    o = FarsOrchestrator(workspace_path)
    status = o.get_status()
    # Include Feishu sync status if available
    sync_status_path = Path(workspace_path) / "lark_sync" / "sync_status.json"
    if sync_status_path.exists():
        try:
            status["lark_sync_status"] = json.loads(sync_status_path.read_text())
        except (json.JSONDecodeError, OSError):
            status["lark_sync_status"] = {"error": "corrupted sync_status.json"}
    print(json.dumps(status, indent=2))


def cli_checkpoint(workspace_path: str, stage: str, step_id: str):
    """CLI: Mark a checkpoint sub-step as completed."""
    cp_dir = CHECKPOINT_DIRS.get(stage)
    if cp_dir is None:
        print(json.dumps({"status": "error", "message": f"No checkpoint support for stage '{stage}'"}))
        return
    ws_path = Path(workspace_path)
    ws = Workspace(ws_path.parent, ws_path.name)
    artifacts: list[str] | None = None
    has_figures_block = True
    if stage == "writing_sections":
        section_md = ws.read_file(f"writing/sections/{step_id}.md") or ""
        artifacts, has_figures_block = extract_section_figure_artifacts(section_md)
        if not has_figures_block:
            artifacts = None

    result = ws.complete_checkpoint_step(
        cp_dir,
        step_id,
        artifacts=artifacts,
        require_artifacts_metadata=(stage == "writing_sections"),
    )

    payload = {
        "status": "ok",
        "stage": stage,
        "step": step_id,
        "completed": result["completed"],
    }
    if stage == "writing_sections" and not has_figures_block:
        payload["message"] = (
            "section 缺少 <!-- FIGURES --> block，checkpoint 未标记完成"
        )
    elif not result["completed"]:
        payload["message"] = "checkpoint 未标记完成，请补齐缺失产物后重试"
    if result["missing_files"]:
        payload["missing_files"] = result["missing_files"]
    print(json.dumps(payload))
    try:
        status = ws.get_status()
        EventLogger(ws.root).checkpoint_step(
            stage=stage, step_id=step_id, iteration=status.iteration)
    except Exception:
        pass


def cli_experiment_status(workspace_path: str = ""):
    """CLI: Check experiment status with rich progress information.

    Combines monitor status, gpu_progress.json, and task_plan.json to
    produce a comprehensive status report for user display.

    Output JSON includes:
        status, completed, running, pending, total,
        elapsed_min, estimated_remaining_min,
        display (formatted string for direct output to user)

    Note: The caller should extract the 'display' field from the JSON
    and output it as a text message to the user. Do NOT rely on Bash
    stdout for display — Claude Code's UI collapses long Bash output.
    """
    import datetime as _dt
    from sibyl.gpu_scheduler import read_monitor_result, _load_progress

    if not workspace_path:
        monitor = read_monitor_result()
        result = dict(monitor) if monitor else {"status": "no_monitor"}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    project_root = resolve_workspace_root(workspace_path)
    active_root = resolve_active_workspace_path(workspace_path)
    monitor = read_monitor_result(project_marker_file(project_root.name, "exp_monitor"))
    result = dict(monitor) if monitor else {"status": "no_monitor"}
    completed, running_ids, running_map, timings = _load_progress(active_root)

    # Task plan info
    task_plan_path = active_root / "plan" / "task_plan.json"
    total_tasks = 0
    task_names: dict[str, str] = {}
    task_estimates: dict[str, int] = {}
    if task_plan_path.exists():
        try:
            plan = json.loads(task_plan_path.read_text(encoding="utf-8"))
            for t in plan.get("tasks", []):
                total_tasks += 1
                task_names[t["id"]] = t.get("name", t["id"])
                task_estimates[t["id"]] = t.get("estimated_minutes", 0)
        except (json.JSONDecodeError, OSError):
            pass

    pending_count = max(0, total_tasks - len(completed) - len(running_ids))
    elapsed_sec = result.get("elapsed_sec", 0)
    elapsed_min = elapsed_sec // 60 if elapsed_sec else 0

    # Per-task elapsed and remaining estimates
    max_remaining_sec = 0
    task_lines = []
    for tid, info in running_map.items():
        gpus = info.get("gpu_ids", [])
        name = task_names.get(tid, tid)
        started = info.get("started_at", "")
        task_elapsed_min = 0
        if started:
            try:
                start_dt = _dt.datetime.fromisoformat(started)
                task_elapsed_min = int(
                    (_dt.datetime.now() - start_dt).total_seconds() / 60
                )
            except (ValueError, TypeError):
                pass
        est = task_estimates.get(tid, 0)
        if est > 0:
            remaining = max(0, est * 60 - task_elapsed_min * 60)
            max_remaining_sec = max(max_remaining_sec, remaining)

        gpu_str = ",".join(str(g) for g in gpus)
        task_lines.append(f"    {name} -> GPU[{gpu_str}] ({task_elapsed_min}min)")

    est_remaining_min = int(max_remaining_sec / 60)

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

    # Build display string
    lines = []
    lines.append("")
    lines.append(
        "+-----------------------------------------+"
    )
    lines.append(
        "|      SIBYL - Experiment Monitor          |"
    )
    lines.append(
        "+-----------------------------------------+"
    )

    # Progress bar
    if total_tasks > 0:
        done_pct = len(completed) / total_tasks
        bar_w = 20
        filled = int(bar_w * done_pct)
        bar = "#" * filled + "." * (bar_w - filled)
        pct_str = f"{int(done_pct * 100)}%"
        lines.append(
            f"|  [{bar}] {len(completed)}/{total_tasks} ({pct_str})"
        )

    # Status
    status_label = {
        "all_complete": "ALL DONE",
        "monitoring": "RUNNING",
        "timeout": "TIMEOUT",
        "no_monitor": "INITIALIZING",
    }.get(result["status"], result["status"])
    lines.append(f"|  Status: {status_label}")

    # Running tasks
    if task_lines:
        lines.append("|  Running:")
        for tl in task_lines:
            lines.append(f"|  {tl}")

    # Pending
    if pending_count > 0:
        lines.append(f"|  Queued: {pending_count} tasks waiting")

    # Time
    time_parts = []
    if elapsed_min > 0:
        time_parts.append(f"elapsed {elapsed_min}min")
    if est_remaining_min > 0:
        time_parts.append(f"~{est_remaining_min}min remaining")
    if time_parts:
        lines.append(f"|  Time: {', '.join(time_parts)}")

    lines.append("|")
    lines.append("|  System running, please wait...")
    lines.append(
        "+-----------------------------------------+"
    )
    lines.append("")

    result["display"] = "\n".join(lines)
    result["completed_count"] = len(completed)
    result["running_count"] = len(running_ids)
    result["pending_count"] = pending_count
    result["total_tasks"] = total_tasks
    result["elapsed_min"] = elapsed_min
    result["estimated_remaining_min"] = est_remaining_min

    print(json.dumps(result, indent=2, ensure_ascii=False))

    # Persist monitor snapshot to workspace for dashboard access
    try:
        monitor_persist = {
            k: v for k, v in result.items() if k != "display"
        }
        monitor_persist["snapshot_at"] = time.time()
        persist_path = active_root / "exp" / "monitor_status.json"
        persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = persist_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(monitor_persist, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        tmp.replace(persist_path)
    except Exception:
        pass


def cli_sentinel_session(workspace_path: str, session_id: str):
    """CLI: Save Claude Code session ID for Sentinel resume."""
    session_path = Path(workspace_path) / "sentinel_session.json"
    session_path.write_text(json.dumps({
        "session_id": session_id,
        "saved_at": time.time(),
    }))
    print(json.dumps({"status": "ok", "session_id": session_id}))


def cli_sentinel_config(workspace_path: str):
    """CLI: Get Sentinel configuration for watchdog script."""
    ws_path = Path(workspace_path)

    session_data: dict = {}
    session_path = ws_path / "sentinel_session.json"
    if session_path.exists():
        try:
            session_data = json.loads(session_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    heartbeat: dict = {}
    hb_path = ws_path / "sentinel_heartbeat.json"
    if hb_path.exists():
        try:
            heartbeat = json.loads(hb_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Check if project has running experiments
    has_running = False
    exp_state_path = ws_path / "exp" / "experiment_state.json"
    if exp_state_path.exists():
        try:
            exp_data = json.loads(exp_state_path.read_text())
            for t in exp_data.get("tasks", {}).values():
                if t.get("status") == "running":
                    has_running = True
                    break
        except (json.JSONDecodeError, OSError):
            pass

    # Also check gpu_progress running map
    gpu_progress_path = ws_path / "exp" / "gpu_progress.json"
    if not has_running and gpu_progress_path.exists():
        try:
            gp = json.loads(gpu_progress_path.read_text())
            if gp.get("running"):
                has_running = True
        except (json.JSONDecodeError, OSError):
            pass

    # Check project status
    status_path = ws_path / "status.json"
    stage = ""
    paused = False
    stop_requested = False
    if status_path.exists():
        try:
            status = workspace_status_from_data(json.loads(status_path.read_text()))
            stage = status.stage
            paused = status.paused
            stop_requested = status.stop_requested
        except (json.JSONDecodeError, OSError):
            pass

    should_keep_running = (
        not stop_requested and (
            has_running or stage not in {"", "init", "done"}
        )
    )

    print(json.dumps({
        "workspace_path": str(ws_path),
        "session_id": session_data.get("session_id", ""),
        "heartbeat": heartbeat,
        "has_running_experiments": has_running,
        "stage": stage,
        "paused": paused,
        "stop_requested": stop_requested,
        "auto_resume_pending": paused and not stop_requested,
        "should_keep_running": should_keep_running,
    }, indent=2))


def cli_dispatch_tasks(workspace_path: str):
    """CLI: Dynamic dispatch — find free GPUs and return next task assignments.

    Called during experiment monitoring when tasks complete and GPUs become free.
    Checks gpu_progress.json for running tasks, determines freed GPUs,
    and returns new task assignments (skill dicts) for immediate dispatch.

    Output JSON:
        {"dispatch": [...assignments...], "skills": [...skill_dicts...]}
        or {"dispatch": [], "reason": "no_free_gpus|no_ready_tasks|all_done"}
    """
    from sibyl.gpu_scheduler import (
        get_batch_info, get_running_gpu_ids, register_running_tasks,
        read_poll_result,
    )
    o = FarsOrchestrator(workspace_path)
    status = o.ws.get_status()
    stage = status.stage
    if stage not in ("pilot_experiments", "experiment_cycle"):
        print(json.dumps({"dispatch": [], "reason": "not_experiment_stage"}))
        return

    mode = "PILOT" if stage == "pilot_experiments" else "FULL"
    active_root = o.ws.active_root
    active_workspace = str(active_root)

    # Determine available GPUs: all configured minus occupied by running tasks
    if o.config.gpu_poll_enabled:
        polled = read_poll_result(project_marker_file(o.ws.name, "gpu_free"))
        all_gpu_ids = polled if polled else list(range(o.config.max_gpus))
    else:
        all_gpu_ids = list(range(o.config.max_gpus))

    occupied = set(get_running_gpu_ids(active_root))
    free_gpus = [g for g in all_gpu_ids if g not in occupied]

    if not free_gpus:
        print(json.dumps({"dispatch": [], "reason": "no_free_gpus"}))
        return

    info = get_batch_info(
        active_root, free_gpus, mode,
        gpus_per_task=o.config.gpus_per_task,
    )

    if info is None:
        print(json.dumps({"dispatch": [], "reason": "all_done"}))
        return

    batch = info["batch"]
    if not batch:
        print(json.dumps({"dispatch": [], "reason": "no_ready_tasks"}))
        return

    # Register new tasks as running
    task_gpu_map = {}
    for assignment in batch:
        for tid in assignment["task_ids"]:
            task_gpu_map[tid] = assignment["gpu_ids"]
    register_running_tasks(active_root, task_gpu_map)

    # Build skill dicts for each assignment
    skills = []
    for assignment in batch:
        task_ids = ",".join(assignment["task_ids"])
        gpu_ids = assignment["gpu_ids"]
        skills.append(
            o._experiment_skill_dict(mode, active_workspace, gpu_ids, task_ids)
        )

    gpu_summary = ", ".join(
        f"{a['task_ids'][0]}→GPU{a['gpu_ids']}" for a in batch
    )
    print(json.dumps({
        "dispatch": batch,
        "skills": skills,
        "description": f"动态调度: {gpu_summary}",
        "estimated_minutes": info["estimated_minutes"],
    }, indent=2))
    try:
        all_tids = [t for a in batch for t in a["task_ids"]]
        all_gids = [g for a in batch for g in a["gpu_ids"]]
        EventLogger(Path(workspace_path)).task_dispatch(
            task_ids=all_tids, gpu_ids=all_gids, iteration=status.iteration)
    except Exception:
        pass


def cli_recover_experiments(workspace_path: str):
    """CLI: Detect and prepare recovery for interrupted experiments.

    Loads experiment_state.json, checks for running tasks, and if found,
    generates a detection script to run via SSH to determine actual status.

    Output JSON:
        {"status": "no_recovery_needed", "total_tasks": N}
        or {"status": "has_running_tasks", "running_tasks": [...],
            "detection_script": "...", "ssh_server": "...",
            "instructions": "..."}
    """
    from sibyl.experiment_recovery import (
        load_experiment_state, migrate_from_gpu_progress,
        save_experiment_state, get_running_tasks, generate_detection_script,
    )

    active_root = resolve_active_workspace_path(workspace_path)
    state = load_experiment_state(active_root)

    # If no tasks tracked, try migrating from gpu_progress.json
    if not state.tasks:
        state = migrate_from_gpu_progress(active_root)
        if state.tasks:
            save_experiment_state(active_root, state)

    running = get_running_tasks(state)
    if not running:
        print(json.dumps({
            "status": "no_recovery_needed",
            "total_tasks": len(state.tasks),
        }, indent=2))
        return

    o = FarsOrchestrator(workspace_path)
    remote_project_dir = f"{o.config.remote_base}/projects/{o.ws.name}"
    script = generate_detection_script(remote_project_dir, running)

    print(json.dumps({
        "status": "has_running_tasks",
        "running_tasks": running,
        "detection_script": script,
        "ssh_server": o.config.ssh_server,
        "instructions": (
            "Run the detection_script on the remote server via SSH, "
            "then pass the output to cli_apply_recovery."
        ),
    }, indent=2))


def cli_apply_recovery(workspace_path: str, ssh_output: str):
    """CLI: Apply recovery based on SSH detection output.

    Parses the output from a detection script, updates experiment_state,
    syncs to gpu_progress.json, and returns a recovery summary.

    Output JSON:
        {"status": "recovered", "recovered_completed": [...],
         "still_running": [...], "recovered_failed": [...],
         "progress": {...}}
    """
    from sibyl.experiment_recovery import (
        load_experiment_state, save_experiment_state,
        parse_detection_output, recover_from_detection,
        sync_to_gpu_progress,
    )
    from dataclasses import asdict as _asdict

    active_root = resolve_active_workspace_path(workspace_path)
    state = load_experiment_state(active_root)

    detection = parse_detection_output(ssh_output)
    result = recover_from_detection(state, detection)

    save_experiment_state(active_root, state)
    sync_to_gpu_progress(active_root, state)

    output = _asdict(result)
    output["status"] = "recovered"
    print(json.dumps(output, indent=2))


def cli_list_projects(workspaces_dir: str = "workspaces"):
    """CLI: List all projects."""
    ws_dir = Path(workspaces_dir)
    if not ws_dir.exists():
        print(json.dumps([]))
        return
    projects = []
    for d in sorted(ws_dir.iterdir()):
        if d.is_dir() and (d / "status.json").exists():
            try:
                ws = Workspace(ws_dir, d.name)
                meta = ws.get_project_metadata()
                meta["topic"] = ws.read_file("topic.txt") or ""
                projects.append(meta)
            except Exception:
                continue
    print(json.dumps(projects, indent=2))


def cli_init_spec(project_name: str, config_path: str | None = None):
    """CLI: Initialize a project directory for spec editing."""
    config = load_effective_config(config_path=config_path)
    ws = Workspace(
        config.workspaces_dir,
        project_name,
        iteration_dirs=config.iteration_dirs,
    )
    write_project_config(ws, config)

    # Create spec template
    spec_template = f"""# 项目: {project_name}

## 研究主题
<!-- 一句话描述研究主题 -->

## 背景与动机
<!-- 为什么要研究这个？有什么已知的相关工作？ -->

## 初始想法
<!-- 你已有的想法或方向（可选） -->

## 关键参考文献
<!-- 论文 URL、arXiv ID 等 -->
-

## 可用资源
- GPU: {config.max_gpus}x on {config.ssh_server}
- 服务器: {config.ssh_server}
- 远程路径: {config.remote_base}

## 实验约束
- 实验类型: training-free / 轻量训练 / 不限
- 模型规模: 小 (GPT-2, BERT-base, Qwen-0.5B)
- 时间预算:

## 目标产出
- 论文 / 技术报告 / 实验验证

## 特殊需求
<!-- 任何特殊需求 -->
"""
    ws.write_file("spec.md", spec_template)
    print(json.dumps({
        "project_name": project_name,
        "workspace_path": str(ws.root),
        "spec_path": str(ws.root / "spec.md"),
    }, indent=2))


def cli_init_from_spec(spec_path: str, config_path: str | None = None):
    """CLI: Initialize a project from a spec markdown file."""
    spec_file = Path(spec_path)
    if not spec_file.exists():
        print(json.dumps({"error": f"Spec file not found: {spec_path}"}))
        return

    spec_content = spec_file.read_text(encoding="utf-8")

    existing_workspace_root = resolve_workspace_root(spec_file.parent)
    if spec_file.name == "spec.md" and (existing_workspace_root / "status.json").exists():
        project_name = existing_workspace_root.name
        config = load_effective_config(
            workspace_path=existing_workspace_root,
            config_path=config_path,
        )
        iteration_dirs = load_workspace_iteration_dirs(
            existing_workspace_root,
            config.iteration_dirs,
        )
        ws = Workspace(
            existing_workspace_root.parent,
            existing_workspace_root.name,
            iteration_dirs=iteration_dirs,
        )
    else:
        match = re.search(r'^#\s*(?:Project|项目):\s*(.+)', spec_content, re.MULTILINE)
        project_name = match.group(1).strip() if match else spec_file.stem
        project_name = FarsOrchestrator._slugify(project_name)
        config = load_effective_config(config_path=config_path)
        ws = Workspace(
            config.workspaces_dir,
            project_name,
            iteration_dirs=config.iteration_dirs,
        )
    write_project_config(ws, config)

    # Extract topic from spec
    topic_match = re.search(r'##\s*(?:Topic|研究主题)\s*\n+(.+?)(?:\n\n|\n##)', spec_content, re.DOTALL)
    topic = topic_match.group(1).strip() if topic_match else project_name
    # Remove HTML comments
    topic = re.sub(r'<!--.*?-->', '', topic).strip()

    # Save spec and topic
    ws.write_file("spec.md", spec_content)
    ws.write_file("topic.txt", topic)
    ws.update_stage("init")
    ws.git_init()

    # Extract references if present
    refs_match = re.search(r'##\s*(?:Key References|关键参考文献)\s*\n(.+?)(?:\n##|\Z)', spec_content, re.DOTALL)
    if refs_match:
        ws.write_file("idea/references_seed.md", refs_match.group(1).strip())

    # Extract initial ideas if present
    ideas_match = re.search(r'##\s*(?:Initial Ideas|初始想法)\s*\n(.+?)(?:\n##|\Z)', spec_content, re.DOTALL)
    if ideas_match:
        ideas_text = ideas_match.group(1).strip()
        ideas_text = re.sub(r'<!--.*?-->', '', ideas_text).strip()
        if ideas_text:
            ws.write_file("idea/initial_ideas.md", ideas_text)

    print(json.dumps({
        "project_name": project_name,
        "workspace_path": str(ws.root),
        "topic": topic,
        "spec_path": str(ws.root / "spec.md"),
        "config": {
            "ssh_server": config.ssh_server,
            "remote_base": config.remote_base,
            "max_gpus": config.max_gpus,
            "pilot_samples": config.pilot_samples,
            "full_seeds": config.full_seeds,
            "lark_enabled": config.lark_enabled,
        },
    }, indent=2))


def _infer_topic_for_workspace(ws: Workspace) -> str:
    """Infer a reasonable topic when legacy workspaces are missing topic.txt."""
    spec_path = ws.root / "spec.md"
    if spec_path.exists():
        try:
            spec_text = spec_path.read_text(encoding="utf-8")
            match = re.search(r'^#\s*(.+)', spec_text, re.MULTILINE)
            if match:
                return match.group(1).strip()
        except OSError:
            pass

    proposal = ws.read_file("idea/proposal.md")
    if proposal:
        title_match = re.search(r'^#\s*(.+)', proposal, re.MULTILINE)
        if title_match:
            return title_match.group(1).strip()

    topic = ws.read_file("topic.txt")
    if topic:
        return topic.strip()

    return ws.name.replace("-", " ").title()


def _detect_workspace_iteration_dirs(
    workspace_root: Path,
    raw_status: dict,
    default: bool,
) -> bool:
    """Infer iteration directory mode for legacy workspaces."""
    if "iteration_dirs" in raw_status:
        return bool(raw_status.get("iteration_dirs"))
    current_link = workspace_root / "current"
    if current_link.exists():
        return True
    return any(
        child.is_dir() and re.fullmatch(r"iter_\d{3}", child.name)
        for child in workspace_root.iterdir()
    ) or default


def _strip_leading_title(markdown: str) -> str:
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("#"):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _build_migrated_spec(ws: Workspace, topic: str) -> str:
    """Build a conservative spec.md for a legacy workspace."""
    proposal = ws.read_file("idea/proposal.md") or ""
    proposal_body = _strip_leading_title(proposal)
    lines = [
        f"# 项目: {ws.name}",
        "",
        "## 研究主题",
        topic,
        "",
    ]

    if proposal_body:
        lines.extend([
            "## 背景与当前状态",
            "_以下内容由旧版 `idea/proposal.md` 回填，建议后续继续整理为正式 spec。_",
            "",
            proposal_body,
            "",
        ])
    else:
        lines.extend([
            "## 背景与当前状态",
            "_旧项目迁移自动生成，请补充研究背景、关键约束和目标产出。_",
            "",
            "## 关键约束",
            "- 待补充",
            "",
            "## 目标产出",
            "- 待补充",
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def _ensure_workspace_gitignore(ws: Workspace) -> bool:
    """Ensure runtime-managed paths are ignored inside the workspace repo."""
    gitignore_path = ws.root / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    changed = False
    for line in _RUNTIME_GITIGNORE_LINES:
        if line not in existing_lines:
            existing_lines.append(line)
            changed = True

    if changed or not gitignore_path.exists():
        content = "\n".join(existing_lines).rstrip() + "\n"
        gitignore_path.write_text(content, encoding="utf-8")
    return changed


def _ensure_workspace_git_repo(
    ws: Workspace,
    changes: list[str],
    warnings: list[str],
) -> None:
    """Initialize a per-workspace git repo without clobbering custom ignores."""
    git_was_present = (ws.root / ".git").exists()
    gitignore_changed = _ensure_workspace_gitignore(ws)
    if gitignore_changed:
        changes.append("Updated .gitignore for layered runtime assets")
    elif not (ws.root / ".gitignore").exists():
        changes.append("Created .gitignore for layered runtime assets")

    if git_was_present:
        return

    init_result = subprocess.run(
        ["git", "init"],
        cwd=ws.root,
        capture_output=True,
        text=True,
    )
    if init_result.returncode != 0:
        warnings.append(f"Failed to initialize git repo: {init_result.stderr.strip()}")
        return
    changes.append("Initialized workspace git repository")

    subprocess.run(["git", "add", "."], cwd=ws.root, capture_output=True, text=True)
    commit_result = subprocess.run(
        ["git", "commit", "-m", "feat: initialize Sibyl research project"],
        cwd=ws.root,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode == 0:
        changes.append("Created initial workspace git commit")
        return

    commit_output = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
    if "nothing to commit" not in commit_output:
        warnings.append("Git repo initialized but initial commit failed")


def _merge_pending_sync_jsonl(target_path: Path, legacy_path: Path) -> bool:
    """Merge a legacy pending_sync.jsonl into the canonical workspace path."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_lines = target_path.read_text(encoding="utf-8").splitlines() if target_path.exists() else []
    legacy_lines = legacy_path.read_text(encoding="utf-8").splitlines() if legacy_path.exists() else []

    merged = list(target_lines)
    for line in legacy_lines:
        if line and line not in merged:
            merged.append(line)

    def _sort_key(line: str) -> tuple[str, str]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return ("", line)
        return (str(payload.get("timestamp") or payload.get("at") or ""), line)

    merged.sort(key=_sort_key)
    if merged == target_lines:
        return False

    content = "\n".join(merged).rstrip()
    if content:
        content += "\n"
    target_path.write_text(content, encoding="utf-8")
    return True


def _cleanup_legacy_nested_workspace_dir(
    ws: Workspace,
    changes: list[str],
    warnings: list[str],
) -> None:
    """Flatten supported legacy nested workspace artifacts back into the root."""
    nested_root = ws.root / ws.name
    if not nested_root.exists() or not nested_root.is_dir():
        return

    supported_files = {"lark_sync/pending_sync.jsonl"}
    unsupported_files: list[str] = []
    for path in nested_root.rglob("*"):
        if path.is_file():
            rel_path = path.relative_to(nested_root).as_posix()
            if rel_path not in supported_files:
                unsupported_files.append(rel_path)

    if unsupported_files:
        warnings.append(
            "Legacy nested workspace directory contains unsupported files: "
            + ", ".join(sorted(unsupported_files))
        )
        return

    legacy_pending = nested_root / "lark_sync" / "pending_sync.jsonl"
    if legacy_pending.exists():
        merged = _merge_pending_sync_jsonl(
            ws.root / "lark_sync" / "pending_sync.jsonl",
            legacy_pending,
        )
        if merged:
            changes.append("Merged legacy nested lark_sync/pending_sync.jsonl into workspace root")

    shutil.rmtree(nested_root)
    changes.append("Removed legacy nested workspace directory")


def migrate_workspace(workspace_path: str | Path) -> dict:
    """Migrate one project workspace onto the layered runtime scaffold."""
    ws_path = resolve_workspace_root(workspace_path)
    if not ws_path.exists():
        return {"error": f"Workspace not found: {workspace_path}"}

    raw_status: dict = {}
    status_path = ws_path / "status.json"
    if status_path.exists():
        try:
            raw_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw_status = {}
    default_cfg = load_effective_config(workspace_path=ws_path)
    iteration_dirs = _detect_workspace_iteration_dirs(
        ws_path,
        raw_status,
        default_cfg.iteration_dirs,
    )

    runtime_scaffold_was_ready = all(
        (ws_path / rel_path).exists() or (ws_path / rel_path).is_symlink()
        for rel_path in (
            ".sibyl/system.json",
            ".sibyl/project/MEMORY.md",
            ".sibyl/project/prompt_overlays",
            "CLAUDE.md",
            ".claude/agents",
            ".claude/skills",
            ".claude/settings.local.json",
            ".venv",
        )
    )

    ws = Workspace(ws_path.parent, ws_path.name, iteration_dirs=iteration_dirs)

    changes: list[str] = []
    warnings: list[str] = []

    if not (ws.root / "topic.txt").exists():
        topic = _infer_topic_for_workspace(ws)
        ws.write_file("topic.txt", topic)
        changes.append(f"Created topic.txt: {topic}")
    else:
        topic = (ws.read_file("topic.txt") or "").strip() or _infer_topic_for_workspace(ws)

    if not (ws.root / "config.yaml").exists():
        write_project_config(ws, default_cfg)
        changes.append("Created project config snapshot")

    if not (ws.root / "spec.md").exists():
        ws.write_file("spec.md", _build_migrated_spec(ws, topic))
        changes.append("Created spec.md from legacy workspace state")

    _ensure_workspace_git_repo(ws, changes, warnings)
    _cleanup_legacy_nested_workspace_dir(ws, changes, warnings)

    normalized_status = ws.get_status()
    normalized_status.iteration_dirs = iteration_dirs
    if normalized_status.stage_started_at is None and normalized_status.updated_at:
        normalized_status.stage_started_at = normalized_status.updated_at
        changes.append("Backfilled stage_started_at from updated_at")
    if normalized_status.iteration == 0 and normalized_status.stage == "done":
        normalized_status.iteration = 1
        changes.append("Set iteration to 1 for completed legacy project")

    normalized_payload = asdict(normalized_status)
    if raw_status != normalized_payload:
        ws._save_status(normalized_status)
        changes.append("Normalized status.json to current schema")

    runtime_after = ws.get_runtime_metadata()
    if not runtime_scaffold_was_ready:
        changes.append("Installed layered runtime scaffold")

    warnings.extend(runtime_after["warnings"])

    return {
        "project_name": ws.name,
        "workspace_path": str(ws.root),
        "changes": changes,
        "warnings": warnings,
        "runtime": runtime_after,
        "status": {
            "stage": ws.get_status().stage,
            "iteration": ws.get_status().iteration,
        },
    }


def cli_migrate(workspace_path: str):
    """CLI: Migrate a legacy project to v5 structure."""
    print(json.dumps(migrate_workspace(workspace_path), indent=2, ensure_ascii=False))


def cli_migrate_all(workspaces_dir: str | None = None):
    """CLI: Migrate every detected project under a workspaces directory."""
    cfg = load_effective_config()
    ws_dir = Path(workspaces_dir).expanduser() if workspaces_dir else cfg.workspaces_dir
    ws_dir = ws_dir.resolve()
    if not ws_dir.exists():
        print(json.dumps({"error": f"Workspaces dir not found: {ws_dir}"}))
        return

    results = []
    for project_dir in sorted(ws_dir.iterdir()):
        if not project_dir.is_dir() or not (project_dir / "status.json").exists():
            continue
        results.append(migrate_workspace(project_dir))

    print(json.dumps({
        "workspaces_dir": str(ws_dir),
        "total": len(results),
        "migrated": [
            {
                "project_name": r.get("project_name", ""),
                "changes": r.get("changes", []),
                "warnings": r.get("warnings", []),
                "migration_needed": r.get("runtime", {}).get("migration_needed", False),
            }
            for r in results
        ],
    }, indent=2, ensure_ascii=False))


def cli_migrate_server(project_name: str, ssh_connection: str = "default"):
    """CLI: Generate server migration commands.

    Prints the SSH commands needed to reorganize server-side files
    into the v5 project structure. Execute these via SSH MCP.
    """
    if not re.fullmatch(r'[a-zA-Z0-9_\-]{1,60}', project_name):
        print(json.dumps({"error": f"Invalid project_name: {project_name!r}"}))
        return
    config = Config()
    remote_base = config.remote_base
    project_dir = f"{remote_base}/projects/{project_name}"

    # The migration plan: move flat files into project-specific directory
    commands = [
        f"# === 服务器端 v5 迁移: {project_name} ===",
        f"mkdir -p {project_dir}/{{idea,plan,exp/code,exp/results/pilots,exp/results/full,exp/logs,writing/latex,writing/sections,writing/figures,supervisor,critic,reflection,logs/iterations,lark_sync,shared}}",
        "",
        "# 创建共享资源目录",
        f"mkdir -p {remote_base}/shared/{{datasets,checkpoints}}",
        f'test -f {remote_base}/shared/registry.json || echo \'{{}}\' > {remote_base}/shared/registry.json',
        "",
        "# 迁移实验代码",
        f"cp -r {remote_base}/exp/code/* {project_dir}/exp/code/ 2>/dev/null || true",
        "",
        "# 迁移实验日志",
        f"cp -r {remote_base}/exp/logs/* {project_dir}/exp/logs/ 2>/dev/null || true",
        "",
        "# 迁移研究想法",
        f"cp -r {remote_base}/idea/* {project_dir}/idea/ 2>/dev/null || true",
        "",
        "# 迁移迭代日志",
        f"cp -r {remote_base}/logs/* {project_dir}/logs/ 2>/dev/null || true",
        "",
        "# 迁移论文草稿",
        f"cp -r {remote_base}/writing/* {project_dir}/writing/ 2>/dev/null || true",
        "",
        "# 创建状态文件",
        f'echo \'{{"stage": "done", "started_at": 0, "updated_at": 0, "iteration": 1, "errors": [], "paused": false, "paused_at": null, "stop_requested": false, "stop_requested_at": null, "iteration_dirs": false, "stage_started_at": 0}}\' > {project_dir}/status.json',
        "",
        "# 保留共享资源的符号链接",
        f"ln -sf {remote_base}/models {project_dir}/models 2>/dev/null || true",
        f"ln -sf {remote_base}/src {project_dir}/src 2>/dev/null || true",
        "",
        f"echo '迁移完成: {project_dir}'",
    ]

    print(json.dumps({
        "project_name": project_name,
        "remote_project_dir": project_dir,
        "commands": commands,
        "instructions": (
            "使用 mcp__ssh-mcp-server__execute-command 依次执行上述命令。\n"
            "模型和源码目录通过符号链接共享，避免重复存储。\n"
            "迁移后，新项目将在 projects/ 子目录下创建，互不干扰。"
        ),
    }, indent=2))


# ══════════════════════════════════════════════
# Self-Healing CLI
# ══════════════════════════════════════════════

def cli_self_heal_scan(workspace_path: str = ""):
    """CLI: Scan for errors and generate repair tasks.

    Reads logs/errors.jsonl, deduplicates, filters actionable errors,
    and returns repair tasks with skill routing.
    """
    from sibyl.error_collector import ErrorCollector
    from sibyl.self_heal import SelfHealRouter

    # Determine errors file location
    if workspace_path:
        errors_file = Path(workspace_path) / "logs" / "errors.jsonl"
        state_file = Path(workspace_path) / "logs" / "self_heal_state.json"
    else:
        errors_file = Path("logs") / "errors.jsonl"
        state_file = Path("logs") / "self_heal_state.json"

    collector = ErrorCollector(errors_file)
    router = SelfHealRouter(state_file)

    errors = collector.read_errors(unprocessed_only=True)
    errors = router.deduplicate(errors)
    errors = router.filter_actionable(errors)
    errors = router.prioritize(errors)

    tasks = []
    for err in errors:
        task = router.generate_repair_task(err)
        tasks.append(task)

    print(json.dumps({
        "total_unprocessed": len(collector.read_errors(unprocessed_only=True)),
        "actionable": len(tasks),
        "tasks": tasks,
        "self_heal_status": router.get_status(),
    }, indent=2))


def cli_self_heal_record(
    error_id: str,
    success: bool,
    commit_hash: str = "",
    workspace_path: str = "",
):
    """CLI: Record a self-heal fix attempt result."""
    from sibyl.error_collector import ErrorCollector
    from sibyl.self_heal import SelfHealRouter

    if workspace_path:
        errors_file = Path(workspace_path) / "logs" / "errors.jsonl"
        state_file = Path(workspace_path) / "logs" / "self_heal_state.json"
    else:
        errors_file = Path("logs") / "errors.jsonl"
        state_file = Path("logs") / "self_heal_state.json"

    collector = ErrorCollector(errors_file)
    router = SelfHealRouter(state_file)

    router.record_fix_attempt(error_id, success, commit_hash or None)
    if success:
        collector.mark_processed(error_id)

    print(json.dumps({
        "error_id": error_id,
        "success": success,
        "commit": commit_hash,
        "status": router.get_status(),
    }, indent=2))


def cli_self_heal_status(workspace_path: str = ""):
    """CLI: Show self-heal system status."""
    from sibyl.self_heal import SelfHealRouter

    if workspace_path:
        state_file = Path(workspace_path) / "logs" / "self_heal_state.json"
    else:
        state_file = Path("logs") / "self_heal_state.json"

    router = SelfHealRouter(state_file)
    status = router.get_status()

    print(json.dumps({
        "self_heal": status,
        "summary": {
            "fixed_count": len(status["fixed"]),
            "circuit_broken_count": len(status["circuit_broken"]),
            "in_progress_count": len(status["in_progress"]),
        },
    }, indent=2))


def self_heal_monitor_script(
    workspace_path: str,
    interval_sec: int = 300,
) -> str:
    """Generate a background monitor script for self-healing.

    The script periodically scans for errors and outputs status to
    a project-scoped marker under /tmp for the main session to read.
    """
    status_file = self_heal_status_file(workspace_path)
    scan_cmd = build_repo_python_cli_command("self-heal-scan", workspace_path)
    actionable_cmd = shlex.join([
        sys.executable,
        "-c",
        "import json, sys; data = json.load(sys.stdin); print(data.get('actionable', 0))",
    ])
    return f'''#!/usr/bin/env bash
# Sibyl Self-Heal Monitor — auto-generated
WORKSPACE={shlex.quote(workspace_path)}
ERRORS_FILE="$WORKSPACE/logs/errors.jsonl"
STATUS_FILE={shlex.quote(status_file)}
INTERVAL={interval_sec}

while true; do
    if [ -f "$ERRORS_FILE" ]; then
        RESULT=$({scan_cmd} 2>/dev/null)
        printf '%s\n' "$RESULT" > "$STATUS_FILE"

        # Check if there are actionable tasks
        ACTIONABLE=$({actionable_cmd} <<< "$RESULT" 2>/dev/null || echo "0")

        if [ "$ACTIONABLE" -gt 0 ]; then
            printf '{{"needs_repair": true, "actionable": %s, "timestamp": %s}}\n' "$ACTIONABLE" "$(date +%s)" > "$STATUS_FILE.trigger"
        fi
    fi
    sleep "$INTERVAL"
done
'''


# ══════════════════════════════════════════════
# Event logging & dashboard CLI
# ══════════════════════════════════════════════

def cli_log_agent(workspace_path: str, stage: str, agent_name: str,
                  event: str = "start", model_tier: str = "",
                  status: str = "ok", duration_sec: float | None = None,
                  output_files: str = "", output_summary: str = "",
                  prompt_summary: str = ""):
    """CLI: Log an agent invocation event (start or end).

    Called by main session / skill execution layer before/after each agent.

    Args:
        event: "start" or "end"
        output_files: comma-separated file paths (relative to workspace)
    """
    ws_path = Path(workspace_path)
    ws = Workspace(ws_path.parent, ws_path.name)
    ws_status = ws.get_status()
    # Auto-detect stage from status.json if not provided
    if not stage:
        stage = ws_status.stage
    el = EventLogger(ws.root)

    if event == "start":
        result = el.agent_start(
            stage=stage, agent_name=agent_name, model_tier=model_tier,
            iteration=ws_status.iteration, prompt_summary=prompt_summary,
        )
    else:
        files = [f.strip() for f in output_files.split(",") if f.strip()] if output_files else []
        result = el.agent_end(
            stage=stage, agent_name=agent_name, status=status,
            duration_sec=duration_sec, output_files=files,
            output_summary=output_summary, iteration=ws_status.iteration,
        )
    print(json.dumps(result, ensure_ascii=False))


def cli_dashboard_data(workspace_path: str, events_tail: int = 50):
    """CLI: Aggregate all monitoring data for frontend dashboard."""
    payload = collect_dashboard_data(workspace_path, events_tail=events_tail)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def collect_dashboard_data(workspace_path: str | Path, events_tail: int = 50) -> dict:
    """Aggregate dashboard data for the web UI and CLI consumers."""
    from sibyl.gpu_scheduler import _load_progress

    o = FarsOrchestrator(workspace_path)
    ws = o.ws
    el = EventLogger(ws.root)

    # 1. Project status
    project_status = ws.get_project_metadata()
    project_status["topic"] = ws.read_file("topic.txt") or ""

    # 2. Event-derived analytics
    stage_durations = el.get_stage_durations()
    agent_summary = el.get_agent_summary()
    recent_events = el.tail(events_tail)

    # 3. Experiment progress
    experiment_progress = {}
    try:
        completed, running_ids, running_map, timings = _load_progress(ws.active_root)
        experiment_progress["gpu_progress"] = {
            "completed": sorted(completed),
            "running": sorted(running_ids),
            "running_map": running_map,
            "timings": timings,
        }
    except Exception:
        pass
    exp_state_path = ws.active_path("exp/experiment_state.json")
    if exp_state_path.exists():
        try:
            experiment_progress["experiment_state"] = json.loads(
                exp_state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Include workspace-local monitor status
    monitor_path = ws.active_path("exp/monitor_status.json")
    if monitor_path.exists():
        try:
            experiment_progress["monitor"] = json.loads(
                monitor_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # 4. Checkpoints
    checkpoints = {}
    for stage_name, cp_dir in CHECKPOINT_DIRS.items():
        if ws.has_checkpoint(cp_dir):
            checkpoints[stage_name] = ws.validate_checkpoint(cp_dir)

    # 5. Quality trend
    quality_trend = []
    try:
        from sibyl.reflection import IterationLogger
        il = IterationLogger(ws.root)
        history = il.get_history()
        quality_trend = [
            {"iteration": h["iteration"], "score": h["quality_score"],
             "timestamp": h["timestamp"]}
            for h in history
        ]
    except Exception:
        pass

    # 6. Lark sync status
    lark_sync = None
    sync_path = ws.root / "lark_sync" / "sync_status.json"
    if sync_path.exists():
        try:
            lark_sync = json.loads(sync_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # 7. Recent errors
    errors = []
    errors_path = ws.root / "logs" / "errors.jsonl"
    if errors_path.exists():
        try:
            lines = errors_path.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-20:]:  # last 20 errors
                if line.strip():
                    errors.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "status": project_status,
        "runtime": ws.get_runtime_metadata(),
        "stages": FarsOrchestrator.STAGES,
        "stage_durations": stage_durations,
        "agent_summary": agent_summary,
        "recent_events": recent_events,
        "experiment_progress": experiment_progress,
        "checkpoints": checkpoints,
        "quality_trend": quality_trend,
        "lark_sync_status": lark_sync,
        "errors": errors,
    }
