"""Repo-level control plane contract tests.

These guard cross-layer drift between the Python orchestrator, plugin docs,
prompt files, and Claude runtime assets.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_is_ignored(rel_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_required_claude_agents_exist():
    for rel_path in (
        ".claude/agents/sibyl-heavy.md",
        ".claude/agents/sibyl-standard.md",
        ".claude/agents/sibyl-light.md",
    ):
        assert (REPO_ROOT / rel_path).is_file(), rel_path


def test_orchestrator_skills_have_backing_skill_files():
    orchestrate = (REPO_ROOT / "sibyl/orchestrate.py").read_text(encoding="utf-8")
    skill_names = sorted(
        skill_name
        for skill_name in set(re.findall(r'"(?:name|skill)":\s*"(sibyl-[^"]+)"', orchestrate))
        if skill_name != "sibyl-xxx"
    )

    missing = [
        skill_name
        for skill_name in skill_names
        if not (REPO_ROOT / ".claude" / "skills" / skill_name / "SKILL.md").is_file()
    ]

    assert not missing, missing


def test_claude_runtime_assets_are_not_gitignored():
    assert not _git_is_ignored(".claude/agents/sibyl-standard.md")
    assert not _git_is_ignored(".claude/skills/sibyl-planner/SKILL.md")
    assert _git_is_ignored(".claude/settings.local.json")


def test_plugin_language_defaults_use_zh():
    """Language default 'zh' must appear in orchestration loop or command files."""
    # The language default is now in the shared _orchestration-loop.md
    checked_files = [
        "plugin/commands/_orchestration-loop.md",
        "plugin/commands/start.md",
        "plugin/commands/resume.md",
    ]
    found_zh = any(
        '默认 "zh"' in (REPO_ROOT / f).read_text(encoding="utf-8")
        for f in checked_files
    )
    assert found_zh, "No file contains language default 'zh'"


def test_no_stale_hardcoded_language_clauses():
    banned = (
        "All output in Chinese",
        "All writing in Chinese",
    )
    checked_paths = [
        REPO_ROOT / "sibyl/orchestrate.py",
        *sorted((REPO_ROOT / "sibyl/prompts").glob("*.md")),
        *sorted((REPO_ROOT / "plugin/commands").glob("*.md")),
    ]
    offending = []
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            if phrase in text:
                offending.append(f"{path.relative_to(REPO_ROOT)}: {phrase}")
    assert not offending, offending


def test_writing_prompts_fix_paper_language_contract():
    codex_prompt = (REPO_ROOT / "sibyl/prompts/codex_writer.md").read_text(encoding="utf-8")
    latex_prompt = (REPO_ROOT / "sibyl/prompts/latex_writer.md").read_text(encoding="utf-8")

    assert "English academic section draft" in codex_prompt
    assert "All paper sections must remain in English" in codex_prompt
    assert "已有英文论文草稿" in latex_prompt
    assert "翻译成英文" not in latex_prompt


def test_architecture_docs_describe_project_scoped_gpu_marker():
    architecture_doc = (REPO_ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert "/tmp/sibyl_<project>_gpu_free.json" in architecture_doc


def test_gpu_poll_docs_describe_never_stop_contract():
    """GPU poll docs must describe never-stop behavior (no pause on timeout)."""
    required = {
        "CLAUDE.md": ("action.gpu_poll.script", "永不放弃"),
        "plugin/commands/_orchestration-loop.md": ("gpu_poll", "永不放弃"),
    }

    for rel_path, snippets in required.items():
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in text, f"{rel_path} missing {snippet}"

    # Verify no file tells the system to pause on GPU poll timeout
    for rel_path in ("plugin/commands/_orchestration-loop.md",):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "gpu_poll_timeout" not in text, f"{rel_path} still references gpu_poll_timeout pause"


def test_codex_integration_is_explicit_opt_in_everywhere():
    required = {
        "config.example.yaml": ("codex_enabled: false",),
        "setup.sh": ("codex_enabled: false", "Codex stays disabled by default"),
        "docs/configuration.md": (
            "| `codex_enabled` | bool | `false` |",
            "Requires `codex_enabled: true`; otherwise Sibyl falls back to `parallel`.",
        ),
        "docs/codex-integration.md": ("Default: false",),
        "docs/mcp-servers.md": ("default is `false`",),
    }

    for rel_path, snippets in required.items():
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in text, f"{rel_path} missing {snippet}"


def test_setup_docs_prefer_claude_cli_mcp_registration():
    required = {
        "docs/setup-guide.md": (
            "claude mcp add --scope local ssh-mcp-server",
            "claude mcp add --scope local arxiv-mcp-server",
            ".venv/bin/python3",
        ),
        "docs/mcp-servers.md": (
            "claude mcp add --scope local ssh-mcp-server",
            "claude mcp add --scope local arxiv-mcp-server",
            "Manual JSON fallback",
        ),
        "docs/getting-started.md": (
            "claude mcp add --scope local",
            ".venv/bin/pip install -e .",
        ),
    }

    for rel_path, snippets in required.items():
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in text, f"{rel_path} missing {snippet}"


def test_config_docs_match_runtime_defaults():
    config_ref = (REPO_ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    config_example = (REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert '| `ssh_server` | string | `"default"` |' in config_ref
    assert 'language: zh' in config_example
    assert 'ssh_server: "default"' in config_example
    assert 'language: en' not in config_example


def test_no_unresolved_env_or_pilot_placeholders_in_prompts():
    checked = (
        REPO_ROOT / "sibyl/prompts/_common.md",
        REPO_ROOT / "sibyl/prompts/_common_zh.md",
        REPO_ROOT / "sibyl/prompts/planner.md",
        REPO_ROOT / "sibyl/prompts/experimenter.md",
        REPO_ROOT / "sibyl/prompts/server_experimenter.md",
    )
    for path in checked:
        text = path.read_text(encoding="utf-8")
        assert "{env_cmd}" not in text, path
        assert "{pilot_samples}" not in text, path
        assert "{pilot_timeout}" not in text, path
