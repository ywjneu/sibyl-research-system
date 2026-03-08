"""Shared fixtures for Sibyl tests."""
import tempfile
from pathlib import Path

import pytest

from sibyl.config import Config
from sibyl.orchestrate import FarsOrchestrator
from sibyl.workspace import Workspace


@pytest.fixture
def tmp_ws(tmp_path):
    """Create a temporary workspace with basic setup."""
    ws = Workspace(tmp_path, "test-proj")
    ws.write_file("topic.txt", "test research topic")
    ws.update_stage("init")
    return ws


@pytest.fixture
def make_orchestrator(tmp_path):
    """Factory fixture for creating orchestrators with custom config."""
    def _make(stage="init", iteration=0, **config_overrides):
        config = Config()
        config.workspaces_dir = tmp_path
        for k, v in config_overrides.items():
            setattr(config, k, v)

        o = FarsOrchestrator.__new__(FarsOrchestrator)
        o.config = config
        o.ws = Workspace(tmp_path, "test-proj")
        o.workspace_path = str(o.ws.root)
        o.ws.write_file("topic.txt", "test research topic")
        o.ws.update_stage(stage)
        if iteration > 0:
            o.ws.update_iteration(iteration)
        return o
    return _make
