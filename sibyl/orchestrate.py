"""Sibyl orchestrator for Claude Code native mode.

This module provides a state-machine orchestrator that returns the next action
for the main Claude Code session to execute. It does NOT call claude-agent-sdk.

Usage (called by Skill via Bash):
    python -c "from sibyl.orchestrate import FarsOrchestrator; ..."
"""
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict

from sibyl.config import Config
from sibyl.workspace import Workspace
from sibyl.context_builder import ContextBuilder
from sibyl.experiment_records import ExperimentDB

PAPER_SECTIONS = [
    ("intro", "Introduction"),
    ("related_work", "Related Work"),
    ("method", "Method"),
    ("experiments", "Experiments"),
    ("discussion", "Discussion"),
    ("conclusion", "Conclusion"),
]

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(agent_name: str) -> str:
    """Load an agent prompt from the prompts/ directory, with overlay injection."""
    path = PROMPTS_DIR / f"{agent_name}.md"
    if not path.exists():
        return ""
    base = path.read_text(encoding="utf-8")

    # Global overlay (cross-project experience)
    overlay_path = (
        Path.home() / ".claude" / "sibyl_evolution" / "lessons" / f"{agent_name}.md"
    )
    if overlay_path.exists():
        overlay = overlay_path.read_text(encoding="utf-8")
        base += f"\n\n---\n\n{overlay}"

    return base


def load_common_prompt() -> str:
    """Load the common instructions prompt."""
    return load_prompt("_common")


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
    action_type: str  # "agents_parallel", "agent_single", "bash", "done", "lark_sync", "paused"
    agents: list[dict] | None = None  # for agent actions
    bash_command: str | None = None  # for bash actions
    description: str = ""
    stage: str = ""


class FarsOrchestrator:
    """State-machine orchestrator for Sibyl research pipeline.

    Called by the Sibyl Skill, returns the next action for Claude Code to execute.
    """

    # Pipeline stages in order
    STAGES = [
        "init",
        "literature_search",
        "idea_debate_generate",
        "idea_debate_critique",
        "idea_debate_synthesize",
        "planning",
        "pilot_experiments",
        "experiment_cycle",
        "result_debate",
        "experiment_decision",
        "writing_outline",
        "writing_sections",
        "writing_critique",
        "writing_integrate",
        "writing_final_review",
        "writing_latex",
        "critic_review",
        "supervisor_review",
        "reflection",
        "lark_sync",
        "lark_upload_pdf",
        "quality_gate",
        "done",
    ]

    def __init__(self, workspace_path: str, config: Config | None = None):
        self.config = config or Config()
        self.ws = Workspace(
            self.config.workspaces_dir,
            Path(workspace_path).name,
        )
        self.workspace_path = str(self.ws.root)

    @classmethod
    def init_project(cls, topic: str, project_name: str | None = None,
                     config_path: str | None = None) -> dict:
        """Initialize a new research project. Returns project info."""
        config = Config.from_yaml(config_path) if config_path else Config()

        if project_name is None:
            project_name = cls._slugify(topic)

        ws = Workspace(config.workspaces_dir, project_name)

        # Save topic to status
        status = ws.get_status()
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
                "gpu_ids": config.gpu_ids,
                "pilot_samples": config.pilot_samples,
                "pilot_seeds": config.pilot_seeds,
                "full_seeds": config.full_seeds,
                "debate_rounds": config.debate_rounds,
                "idea_exp_cycles": config.idea_exp_cycles,
                "lark_enabled": config.lark_enabled,
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

    def get_next_action(self) -> dict:
        """Determine and return the next action based on current state."""
        status = self.ws.get_status()

        # Check pause state
        if status.paused_at > 0:
            return asdict(Action(
                action_type="paused",
                description=f"项目已暂停（{time.strftime('%H:%M', time.localtime(status.paused_at))}）。"
                            f"等待额度恢复后自动继续。",
                stage=status.stage,
            ))

        stage = status.stage
        topic = self.ws.read_file("topic.txt") or ""

        action = self._compute_action(stage, topic, status.iteration)

        # Inject model tier info into agents
        if action.agents:
            for agent in action.agents:
                tier, model = self._resolve_model_tier(agent["name"])
                agent["model_tier"] = tier
                agent["model"] = model

        return asdict(action)

    def record_result(self, stage: str, result: str = "",
                      score: float | None = None):
        """Record the result of a completed stage and advance state."""
        # Post-reflection hook: process reflection agent outputs
        if stage == "reflection":
            self._post_reflection_hook()

        next_stage = self._get_next_stage(stage, result, score)
        self.ws.update_stage(next_stage)

        if score is not None:
            self.ws.write_file(
                f"logs/stage_{stage}_score.txt",
                f"{score}"
            )

        # Auto git commit after each stage
        score_str = f" (score={score})" if score is not None else ""
        self.ws.git_commit(f"sibyl: complete {stage}{score_str}")

    def get_status(self) -> dict:
        """Get current project status."""
        meta = self.ws.get_project_metadata()
        meta["topic"] = self.ws.read_file("topic.txt") or ""
        return meta

    def _compute_action(self, stage: str, topic: str, iteration: int) -> Action:
        """Compute the next action based on current stage."""
        ws = self.workspace_path
        common = load_common_prompt()

        # Inject project-level lessons from previous iterations
        lessons = self.ws.read_file("reflection/lessons_learned.md")
        if lessons:
            common += f"\n\n---\n\n# 上一轮迭代经验教训\n\n{lessons}"

        if stage == "init":
            return self._action_literature_search(topic, ws, common)

        elif stage == "literature_search":
            return self._action_idea_debate_generate(topic, ws, common)

        elif stage == "idea_debate_generate":
            return self._action_idea_debate_critique(ws, common)

        elif stage == "idea_debate_critique":
            return self._action_idea_debate_synthesize(topic, ws, common)

        elif stage == "idea_debate_synthesize":
            return self._action_planning(ws, common)

        elif stage == "planning":
            return self._action_pilot_experiments(ws, common)

        elif stage == "pilot_experiments":
            return self._action_experiment_cycle(ws, common, iteration)

        elif stage == "experiment_cycle":
            return self._action_result_debate(ws, common)

        elif stage == "result_debate":
            return self._action_experiment_decision(ws, common)

        elif stage == "experiment_decision":
            # Check if we should proceed to writing or cycle back
            decision = self.ws.read_file("supervisor/experiment_analysis.md") or ""
            if "DECISION: PIVOT" in decision.upper():
                cycle = self._get_current_cycle()
                if cycle < self.config.idea_exp_cycles:
                    return self._action_idea_debate_generate(topic, ws, common)
            return self._action_writing_outline(ws, common)

        elif stage == "writing_outline":
            return self._action_writing_sections(ws, common)

        elif stage == "writing_sections":
            return self._action_writing_critique(ws, common)

        elif stage == "writing_critique":
            return self._action_writing_integrate(ws, common)

        elif stage == "writing_integrate":
            return self._action_writing_final_review(ws, common)

        elif stage == "writing_final_review":
            review = self.ws.read_file("writing/review.md") or ""
            match = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)", review)
            score = float(match.group(1)) if match else 5.0
            if score < 7.0:
                return self._action_writing_integrate(ws, common)
            return self._action_writing_latex(ws, common)

        elif stage == "writing_latex":
            return self._action_critic_review(ws, common)

        elif stage == "critic_review":
            return self._action_supervisor_review(ws, common)

        elif stage == "supervisor_review":
            return self._action_reflection(ws)

        elif stage == "reflection":
            if self.config.lark_enabled:
                return self._action_lark_sync(ws)
            return self._action_quality_gate()

        elif stage == "lark_sync":
            return self._action_lark_upload_pdf(ws)

        elif stage == "lark_upload_pdf":
            return self._action_quality_gate()

        elif stage == "quality_gate":
            return Action(action_type="done", description="Pipeline complete", stage="done")

        else:
            return Action(action_type="done", description="Unknown stage", stage="done")

    # ══════════════════════════════════════════════
    # Action builders
    # ══════════════════════════════════════════════

    def _action_literature_search(self, topic: str, ws: str, common: str) -> Action:
        """Single agent performs literature search via arXiv + WebSearch before idea debate."""
        prompt_template = load_prompt("literature_researcher")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"研究主题: {topic}\n"
            f"Workspace path: {ws}\n\n"
            f"请同时使用 mcp__arxiv-mcp-server__search_papers 和 WebSearch 进行调研，"
            f"将结果写入 {ws}/context/literature.md"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "literature_researcher",
                "prompt": prompt,
                "description": "arXiv + Web 双源文献调研",
            }],
            description="文献调研：arXiv 搜索 + Web 搜索，建立领域现状基础",
            stage="literature_search",
        )

    def _action_idea_debate_generate(self, topic: str, ws: str, common: str) -> Action:
        """3 parallel agents generate ideas independently."""
        # Load spec and initial ideas if available
        spec = self.ws.read_file("spec.md") or ""
        initial_ideas = self.ws.read_file("idea/initial_ideas.md") or ""
        seed_refs = self.ws.read_file("idea/references_seed.md") or ""
        literature = self.ws.read_file("context/literature.md") or ""

        extra_context = ""
        if spec:
            extra_context += f"\n\n## Project Spec\n{spec}"
        if initial_ideas:
            extra_context += f"\n\n## User's Initial Ideas\n{initial_ideas}"
        if seed_refs:
            extra_context += f"\n\n## Seed References (from user)\n{seed_refs}"
        if literature:
            extra_context += f"\n\n## 文献调研报告（请仔细阅读，避免重复已有工作）\n{literature}"

        agents = []
        for role in ["innovator", "pragmatist", "theoretical"]:
            prompt_template = load_prompt(role)
            prompt = (
                f"{common}\n\n{prompt_template}\n\n"
                f"Topic: {topic}\n\n"
                f"Workspace path: {ws}\n\n"
                f"Write your output to {ws}/idea/perspectives/{role}.md"
                f"{extra_context}"
            )
            agents.append({
                "name": role,
                "prompt": prompt,
                "description": f"{role} idea generation",
            })

        return Action(
            action_type="agents_parallel",
            agents=agents,
            description="3 parallel agents generating independent research ideas",
            stage="idea_debate_generate",
        )

    def _action_idea_debate_critique(self, ws: str, common: str) -> Action:
        """Cross-critique: each agent reviews others."""
        agents = []
        roles = ["innovator", "pragmatist", "theoretical"]

        for critic in roles:
            for author in roles:
                if critic == author:
                    continue
                prompt = (
                    f"{common}\n\n"
                    f"You are a {critic} researcher critically evaluating another's idea.\n"
                    f"Be constructive but thorough. Score 1-10.\n\n"
                    f"Read the idea from: {ws}/idea/perspectives/{author}.md\n"
                    f"Write your critique to: {ws}/idea/debate/{critic}_on_{author}.md\n\n"
                    f"Evaluate:\n"
                    f"1. Novelty: Is this truly new?\n"
                    f"2. Feasibility: Can this be implemented with limited compute?\n"
                    f"3. Impact: Would positive results be meaningful?\n"
                    f"4. Risks: Main failure modes?\n"
                    f"5. Suggestions: How to improve?"
                )
                agents.append({
                    "name": f"{critic}_critiques_{author}",
                    "prompt": prompt,
                    "description": f"{critic} critiques {author}",
                })

        return Action(
            action_type="agents_parallel",
            agents=agents,
            description="6 parallel cross-critique agents",
            stage="idea_debate_critique",
        )

    def _action_idea_debate_synthesize(self, topic: str, ws: str, common: str) -> Action:
        prompt_template = load_prompt("synthesizer")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Topic: {topic}\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "synthesizer",
                "prompt": prompt,
                "description": "synthesize ideas into proposal",
            }],
            description="Synthesize ideas and critiques into final proposal",
            stage="idea_debate_synthesize",
        )

    def _action_planning(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("planner")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}\n"
            f"Pilot config: samples={self.config.pilot_samples}, "
            f"seeds={self.config.pilot_seeds}, timeout={self.config.pilot_timeout}s"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "planner",
                "prompt": prompt,
                "description": "design experiment plan",
            }],
            description="Design experiment plan with pilot/full configs",
            stage="planning",
        )

    def _action_pilot_experiments(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("experimenter")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"MODE: PILOT\n"
            f"Workspace path: {ws}\n"
            f"SSH server: {self.config.ssh_server}\n"
            f"Remote base: {self.config.remote_base}\n"
            f"GPU IDs: {self.config.gpu_ids}\n"
            f"Pilot samples: {self.config.pilot_samples}\n"
            f"Pilot seeds: {self.config.pilot_seeds}\n"
            f"Pilot timeout: {self.config.pilot_timeout}s\n\n"
            f"Run PILOT experiments only. Report GO/NO-GO for each task."
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "experimenter_pilot",
                "prompt": prompt,
                "description": "run pilot experiments",
            }],
            description="Run pilot experiments for quick validation",
            stage="pilot_experiments",
        )

    def _action_experiment_cycle(self, ws: str, common: str, iteration: int) -> Action:
        prompt_template = load_prompt("experimenter")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"MODE: FULL\n"
            f"Workspace path: {ws}\n"
            f"SSH server: {self.config.ssh_server}\n"
            f"Remote base: {self.config.remote_base}\n"
            f"GPU IDs: {self.config.gpu_ids}\n"
            f"Full seeds: {self.config.full_seeds}\n"
            f"Iteration: {iteration}\n\n"
            f"Run FULL experiments for tasks that passed pilot."
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "experimenter_full",
                "prompt": prompt,
                "description": "run full experiments",
            }],
            description="Run full experiments with statistical rigor",
            stage="experiment_cycle",
        )

    def _action_result_debate(self, ws: str, common: str) -> Action:
        agents = []
        for role in ["optimist", "skeptic", "strategist"]:
            prompt_template = load_prompt(role)
            prompt = (
                f"{common}\n\n{prompt_template}\n\n"
                f"Workspace path: {ws}"
            )
            agents.append({
                "name": role,
                "prompt": prompt,
                "description": f"{role} result analysis",
            })
        return Action(
            action_type="agents_parallel",
            agents=agents,
            description="3 parallel agents debate experiment results",
            stage="result_debate",
        )

    def _action_experiment_decision(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("supervisor")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}\n\n"
            f"SPECIAL TASK: Analyze experiment results and the debate opinions.\n"
            f"Read:\n"
            f"- {ws}/exp/results/summary.md\n"
            f"- {ws}/idea/result_debate/optimist.md\n"
            f"- {ws}/idea/result_debate/skeptic.md\n"
            f"- {ws}/idea/result_debate/strategist.md\n"
            f"- {ws}/idea/proposal.md\n\n"
            f"Determine: PIVOT or PROCEED?\n"
            f"Write to {ws}/supervisor/experiment_analysis.md\n"
            f"End with exactly: DECISION: PIVOT or DECISION: PROCEED"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "supervisor_decision",
                "prompt": prompt,
                "description": "decide pivot or proceed",
            }],
            description="Supervisor analyzes results and decides PIVOT or PROCEED",
            stage="experiment_decision",
        )

    def _action_writing_outline(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("outline_writer")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "outline_writer",
                "prompt": prompt,
                "description": "generate paper outline",
            }],
            description="Generate paper outline",
            stage="writing_outline",
        )

    def _action_writing_sections(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("section_writer")
        agents = []
        for section_id, section_name in PAPER_SECTIONS:
            prompt = (
                f"{common}\n\n{prompt_template}\n\n"
                f"Section: {section_name}\n"
                f"Section ID: {section_id}\n"
                f"Workspace path: {ws}\n"
                f"Write to: {ws}/writing/sections/{section_id}.md"
            )
            agents.append({
                "name": f"writer_{section_id}",
                "prompt": prompt,
                "description": f"write {section_name} section",
            })
        return Action(
            action_type="agents_parallel",
            agents=agents,
            description="6 parallel agents writing paper sections",
            stage="writing_sections",
        )

    def _action_writing_critique(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("section_critic")
        agents = []
        for section_id, section_name in PAPER_SECTIONS:
            prompt = (
                f"{common}\n\n{prompt_template}\n\n"
                f"Section: {section_name}\n"
                f"Section ID: {section_id}\n"
                f"Workspace path: {ws}\n"
                f"Read: {ws}/writing/sections/{section_id}.md\n"
                f"Write critique to: {ws}/writing/critique/{section_id}_critique.md"
            )
            agents.append({
                "name": f"critic_{section_id}",
                "prompt": prompt,
                "description": f"critique {section_name} section",
            })
        return Action(
            action_type="agents_parallel",
            agents=agents,
            description="6 parallel agents critiquing paper sections",
            stage="writing_critique",
        )

    def _action_writing_integrate(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("editor")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "editor",
                "prompt": prompt,
                "description": "integrate paper sections",
            }],
            description="Integrate all sections into coherent paper",
            stage="writing_integrate",
        )

    def _action_writing_final_review(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("final_critic")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "final_critic",
                "prompt": prompt,
                "description": "final paper review",
            }],
            description="Top-tier conference-level paper review",
            stage="writing_final_review",
        )

    def _action_writing_latex(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("latex_writer")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}\n"
            f"SSH server: {self.config.ssh_server}\n"
            f"Remote base: {self.config.remote_base}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "latex_writer",
                "prompt": prompt,
                "description": "generate NeurIPS LaTeX and compile PDF",
            }],
            description="将论文转为 NeurIPS LaTeX 格式并编译 PDF",
            stage="writing_latex",
        )

    def _action_lark_upload_pdf(self, ws: str) -> Action:
        iteration = self.ws.get_status().iteration
        return Action(
            action_type="lark_upload",
            description=(
                f"上传 PDF 和研究文档到飞书云空间。\n"
                f"飞书文件夹结构:\n"
                f"  西比拉研究项目/\n"
                f"    └── {self.ws.name}/\n"
                f"        ├── 论文/\n"
                f"        │   └── v{iteration}_main.pdf\n"
                f"        ├── 实验报告/\n"
                f"        │   ├── 先导实验报告.md\n"
                f"        │   └── 完整实验报告.md\n"
                f"        ├── 研究日记/\n"
                f"        │   └── research_diary.md\n"
                f"        └── 实验数据/\n"
                f"            └── experiment_table\n\n"
                f"操作步骤:\n"
                f"1. 读取 {ws}/writing/latex/main.pdf\n"
                f"2. 使用 mcp__lark__docx_builtin_import 上传研究日记\n"
                f"3. 使用 mcp__lark__bitable_v1_appTableRecord_create 更新实验数据表\n"
                f"4. 使用 mcp__lark__im_v1_message_create 通知团队:\n"
                f"   「西比拉 [{self.ws.name}] 迭代 {iteration} 完成，PDF 已更新」"
            ),
            stage="lark_upload_pdf",
        )

    def _action_critic_review(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("critic")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "critic",
                "prompt": prompt,
                "description": "comprehensive critic review",
            }],
            description="Harsh but fair academic critique of all outputs",
            stage="critic_review",
        )

    def _action_supervisor_review(self, ws: str, common: str) -> Action:
        prompt_template = load_prompt("supervisor")
        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}"
        )
        return Action(
            action_type="agent_single",
            agents=[{
                "name": "supervisor",
                "prompt": prompt,
                "description": "supervisor quality review",
            }],
            description="Independent supervisor review with quality scoring",
            stage="supervisor_review",
        )

    def _action_reflection(self, ws: str) -> Action:
        common = load_common_prompt()
        prompt_template = load_prompt("reflection")

        # Build prioritized context using ContextBuilder
        ctx = ContextBuilder(budget=6000)
        ctx.add("Supervisor Review",
                self.ws.read_file("supervisor/review_writing.md") or "", priority=10)
        ctx.add("Critic Feedback",
                self.ws.read_file("critic/critique_writing.md") or "", priority=8)
        ctx.add("Experiment Summary",
                self.ws.read_file("exp/results/summary.md") or "", priority=7)
        ctx.add("Previous Lessons",
                self.ws.read_file("reflection/lessons_learned.md") or "", priority=6)
        ctx.add("Research Diary",
                self.ws.read_file("logs/research_diary.md") or "", priority=4)

        iteration = self.ws.get_status().iteration
        context_str = ctx.build()

        prompt = (
            f"{common}\n\n{prompt_template}\n\n"
            f"Workspace path: {ws}\n"
            f"Current iteration: {iteration}\n\n"
            f"## Context\n{context_str}"
        )

        return Action(
            action_type="agent_single",
            agents=[{
                "name": "reflection",
                "prompt": prompt,
                "description": "analyze iteration and generate lessons",
            }],
            description="Reflection agent: classify issues, generate improvement plan and lessons",
            stage="reflection",
        )

    def _action_lark_sync(self, ws: str) -> Action:
        iteration = self.ws.get_status().iteration
        return Action(
            action_type="lark_sync",
            description=f"""同步所有研究数据到飞书云空间。

飞书文件夹: 西比拉研究项目/{self.ws.name}/

## 1. 研究日记
- 读取: {ws}/logs/research_diary.md
- 导入为飞书文档: 研究过程/研究日记.md
- 使用 mcp__lark__docx_builtin_import

## 2. 迭代日志表格
- 读取: {ws}/logs/iterations/master_log.jsonl
- 创建/更新飞书多维表格: 迭代日志/
- 字段: iteration, stage, timestamp, quality_score, issues_found 数量, notes
- 使用 mcp__lark__bitable_v1_appTableRecord_create

## 3. Reflection 报告
- 读取: {ws}/reflection/reflection.md
- 导入为飞书文档: 研究过程/反思报告_v{iteration}.md
- 使用 mcp__lark__docx_builtin_import

## 4. 实验数据表
- 读取: {ws}/exp/experiment_db.jsonl
- 创建/更新飞书多维表格: 实验数据/
- 字段: experiment_id, method, metrics (JSON), status, is_pilot, seed
- 使用 mcp__lark__bitable_v1_appTableRecord_create

## 5. 系统进化记录
- 读取: ~/.claude/sibyl_evolution/outcomes.jsonl
- 读取: ~/.claude/sibyl_evolution/global_lessons.md (如有)
- 导入为飞书文档: 系统进化/全局经验.md
- 使用 mcp__lark__docx_builtin_import

## 6. 团队通知
- 使用 mcp__lark__im_v1_message_create
- 格式: 「西比拉 [{self.ws.name}] 迭代 {iteration} 数据已同步 | 分数: X/10」
""",
            stage="lark_sync",
        )

    def _action_quality_gate(self) -> Action:
        review = self.ws.read_file("supervisor/review_writing.md") or ""
        match = re.search(r"(?:score|rating|quality)[:\s]*(\d+(?:\.\d+)?)",
                          review, re.IGNORECASE)
        score = float(match.group(1)) if match else 5.0
        iteration = self.ws.get_status().iteration

        # Adaptive thresholds from reflection agent's action plan
        threshold = 8.0
        max_iters = 3
        action_plan_raw = self.ws.read_file("reflection/action_plan.json")
        if action_plan_raw:
            try:
                action_plan = json.loads(action_plan_raw)
                threshold = action_plan.get("suggested_threshold_adjustment") or threshold
                max_iters = action_plan.get("suggested_max_iterations") or max_iters
            except (json.JSONDecodeError, TypeError):
                pass

        is_done = (score >= threshold and iteration >= 2) or iteration >= max_iters

        if is_done:
            # Tag final iteration
            self.ws.git_tag(
                f"v{iteration}",
                f"Iteration {iteration} complete, score={score}",
            )

            # Trigger cross-project evolution on completion
            if self.config.evolution_enabled:
                from sibyl.evolution import EvolutionEngine
                EvolutionEngine().run_cross_project_evolution()

            return Action(
                action_type="done",
                description=(
                    f"Pipeline complete (score={score}, threshold={threshold}, "
                    f"iter={iteration}/{max_iters})."
                ),
                stage="done",
            )
        else:
            # Tag end of iteration before starting next
            self.ws.git_tag(
                f"iter-{iteration}",
                f"End of iteration {iteration}, score={score}",
            )
            self.ws.update_iteration(iteration + 1)
            return Action(
                action_type="bash",
                bash_command=f"echo 'Starting iteration {iteration + 1}'",
                description=(
                    f"Quality gate: score={score} < {threshold}, "
                    f"starting iteration {iteration + 1}"
                ),
                stage="init",
            )

    # ══════════════════════════════════════════════
    # Post-reflection hook (processes reflection agent outputs)
    # ══════════════════════════════════════════════

    def _post_reflection_hook(self):
        """Process reflection agent outputs: log iteration, record evolution, generate overlay."""
        from sibyl.reflection import IterationLogger
        from sibyl.evolution import EvolutionEngine

        iteration = self.ws.get_status().iteration
        logger = IterationLogger(self.ws.root)

        # Read reflection agent's structured output
        action_plan_raw = self.ws.read_file("reflection/action_plan.json")
        action_plan = {}
        classified_issues = []
        if action_plan_raw:
            try:
                action_plan = json.loads(action_plan_raw)
                classified_issues = action_plan.get("issues_classified", [])
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: read supervisor issues if reflection agent didn't produce classified issues
        if not classified_issues:
            issues_raw = self.ws.read_file("supervisor/issues.json")
            if issues_raw:
                try:
                    issues_data = json.loads(issues_raw)
                    from sibyl.evolution import IssueCategory
                    classified_issues = [
                        {
                            "description": i.get("description", ""),
                            "category": IssueCategory.classify(i.get("description", "")).value,
                            "severity": i.get("severity", "medium"),
                        }
                        for i in issues_data
                    ]
                except (json.JSONDecodeError, TypeError):
                    pass

        issues_found = [ci.get("description", "") for ci in classified_issues]

        # Extract score
        supervisor_review = self.ws.read_file("supervisor/review_writing.md") or ""
        score = 5.0
        score_match = re.search(r'(?:score|rating|quality)[:\s]*(\d+(?:\.\d+)?)',
                                supervisor_review, re.IGNORECASE)
        if score_match:
            score = float(score_match.group(1))

        # Log iteration with classified issues
        logger.log_iteration(
            iteration=iteration,
            stage="reflection",
            changes=[f"Iteration {iteration} complete"],
            issues_found=issues_found[:10],
            issues_fixed=[],
            quality_score=score,
            notes=json.dumps({"classified_issues": classified_issues[:10]}, ensure_ascii=False),
        )

        # Research diary
        critic_feedback = self.ws.read_file("critic/critique_writing.md") or ""
        reflection_md = self.ws.read_file("reflection/reflection.md") or ""
        diary_entry = (
            f"# Iteration {iteration}\n\n"
            f"**Score**: {score}/10\n"
            f"**Issues**: {len(issues_found)}\n\n"
            f"## Reflection\n{reflection_md[:1000]}\n\n"
            f"## Review Summary\n{supervisor_review[:500]}\n\n"
            f"## Critique Summary\n{critic_feedback[:500]}\n"
        )
        existing_diary = self.ws.read_file("logs/research_diary.md") or ""
        self.ws.write_file("logs/research_diary.md", existing_diary + "\n\n" + diary_entry)

        # Evolution recording with classification
        if self.config.evolution_enabled:
            engine = EvolutionEngine()
            engine.record_outcome(
                project=self.ws.name,
                stage="iteration",
                issues=issues_found,
                score=score,
                notes=f"Iteration {iteration}",
            )
            # Generate per-agent lessons overlay
            engine.generate_lessons_overlay()

    # ══════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════

    def _get_next_stage(self, current_stage: str, result: str = "",
                        score: float | None = None) -> str:
        """Determine the next stage based on current stage and result."""
        try:
            idx = self.STAGES.index(current_stage)
            if idx + 1 < len(self.STAGES):
                return self.STAGES[idx + 1]
        except ValueError:
            pass
        return current_stage

    def _get_current_cycle(self) -> int:
        """Get current idea-experiment cycle number."""
        cycle = 0
        for f in sorted(self.ws.list_files("logs")):
            if "idea_exp_cycle" in f:
                cycle += 1
        return cycle

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


def cli_next(workspace_path: str):
    """CLI: Get next action."""
    o = FarsOrchestrator(workspace_path)
    action = o.get_next_action()
    print(json.dumps(action, indent=2))


def cli_record(workspace_path: str, stage: str, result: str = "",
               score: float | None = None):
    """CLI: Record stage result."""
    o = FarsOrchestrator(workspace_path)
    o.record_result(stage, result, score)
    print(json.dumps({"status": "ok", "new_stage": o.ws.get_status().stage}))


def cli_pause(workspace_path: str, reason: str = "rate_limit"):
    """CLI: Pause a project."""
    o = FarsOrchestrator(workspace_path)
    o.ws.pause(reason)
    print(json.dumps({"status": "paused", "stage": o.ws.get_status().stage}))


def cli_resume(workspace_path: str):
    """CLI: Resume a paused project."""
    o = FarsOrchestrator(workspace_path)
    o.ws.resume()
    print(json.dumps({"status": "resumed", "stage": o.ws.get_status().stage}))


def cli_status(workspace_path: str):
    """CLI: Get project status."""
    o = FarsOrchestrator(workspace_path)
    print(json.dumps(o.get_status(), indent=2))


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
    config = Config.from_yaml(config_path) if config_path else Config()
    ws = Workspace(config.workspaces_dir, project_name)

    # Create spec template
    spec_template = f"""# Project: {project_name}

## Topic
<!-- 一句话描述研究主题 -->

## Background & Motivation
<!-- 为什么要研究这个？有什么已知的相关工作？ -->

## Initial Ideas
<!-- 你已有的想法或方向（可选） -->

## Key References
<!-- 论文 URL、arXiv ID 等 -->
-

## Resources
- GPU: 4x on cs8000d
- Server: {config.ssh_server}
- Remote Base: {config.remote_base}

## Constraints
- Experiment type: training-free / lightweight
- Model size: small (GPT-2, BERT-base, Qwen-0.5B)
- Time budget:

## Target Output
- paper / tech report / experiment validation

## Special Notes
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

    # Extract project name from path or spec
    # Try to get from parent directory name if in workspaces/
    if spec_file.parent.parent.name == "workspaces" or "workspaces" in str(spec_file):
        project_name = spec_file.parent.name
    else:
        # Extract from spec header
        match = re.search(r'^#\s*Project:\s*(.+)', spec_content, re.MULTILINE)
        project_name = match.group(1).strip() if match else spec_file.stem

    project_name = FarsOrchestrator._slugify(project_name)
    config = Config.from_yaml(config_path) if config_path else Config()

    ws = Workspace(config.workspaces_dir, project_name)

    # Extract topic from spec
    topic_match = re.search(r'##\s*Topic\s*\n+(.+?)(?:\n\n|\n##)', spec_content, re.DOTALL)
    topic = topic_match.group(1).strip() if topic_match else project_name
    # Remove HTML comments
    topic = re.sub(r'<!--.*?-->', '', topic).strip()

    # Save spec and topic
    ws.write_file("spec.md", spec_content)
    ws.write_file("topic.txt", topic)
    ws.update_stage("init")

    # Extract references if present
    refs_match = re.search(r'##\s*Key References\s*\n(.+?)(?:\n##|\Z)', spec_content, re.DOTALL)
    if refs_match:
        ws.write_file("idea/references_seed.md", refs_match.group(1).strip())

    # Extract initial ideas if present
    ideas_match = re.search(r'##\s*Initial Ideas\s*\n(.+?)(?:\n##|\Z)', spec_content, re.DOTALL)
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
            "gpu_ids": config.gpu_ids,
            "pilot_samples": config.pilot_samples,
            "full_seeds": config.full_seeds,
            "lark_enabled": config.lark_enabled,
        },
    }, indent=2))


def cli_migrate(workspace_path: str):
    """CLI: Migrate a legacy project to v5 structure."""
    ws_path = Path(workspace_path)
    if not ws_path.exists():
        print(json.dumps({"error": f"Workspace not found: {workspace_path}"}))
        return

    config = Config()
    project_name = ws_path.name
    ws = Workspace(config.workspaces_dir, project_name)

    changes = []

    # Ensure v5 directories exist (Workspace.__init__ handles this)
    changes.append("Ensured v5 directory structure")

    # Create topic.txt if missing
    if not (ws.root / "topic.txt").exists():
        # Try to extract from proposal
        proposal = ws.read_file("idea/proposal.md")
        if proposal:
            # Try to extract title
            title_match = re.search(r'^#\s*(.+)', proposal, re.MULTILINE)
            topic = title_match.group(1).strip() if title_match else project_name
        else:
            topic = project_name.replace("-", " ").title()
        ws.write_file("topic.txt", topic)
        changes.append(f"Created topic.txt: {topic}")

    # Ensure status.json has iteration field
    status = ws.get_status()
    if status.iteration == 0 and status.stage == "done":
        status.iteration = 1
        ws._save_status(status)
        changes.append("Set iteration to 1 (was 0 for completed project)")

    # Create missing subdirectories
    for subdir in ["idea/perspectives", "idea/debate", "idea/result_debate",
                   "plan", "exp/results/pilots", "exp/results/full",
                   "lark_sync", "logs/iterations"]:
        d = ws.root / subdir
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            changes.append(f"Created directory: {subdir}")

    print(json.dumps({
        "project_name": project_name,
        "workspace_path": str(ws.root),
        "changes": changes,
        "status": {
            "stage": ws.get_status().stage,
            "iteration": ws.get_status().iteration,
        },
    }, indent=2))


def cli_migrate_server(project_name: str, ssh_connection: str = "default"):
    """CLI: Generate server migration commands.

    Prints the SSH commands needed to reorganize server-side files
    into the v5 project structure. Execute these via SSH MCP.
    """
    config = Config()
    remote_base = config.remote_base
    project_dir = f"{remote_base}/projects/{project_name}"

    # The migration plan: move flat files into project-specific directory
    commands = [
        f"# === 服务器端 v5 迁移: {project_name} ===",
        f"mkdir -p {project_dir}/{{idea,plan,exp/code,exp/results/pilots,exp/results/full,exp/logs,writing/latex,writing/sections,writing/figures,supervisor,critic,reflection,logs/iterations,lark_sync}}",
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
        f'echo \'{{"stage": "done", "started_at": 0, "updated_at": 0, "iteration": 1, "errors": []}}\' > {project_dir}/status.json',
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
