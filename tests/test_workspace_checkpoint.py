"""Tests for workspace checkpoint functionality."""
import time

import pytest

from sibyl.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path, "test-project")


class TestCheckpointCreation:
    def test_create_checkpoint(self, ws):
        steps = {
            "intro": "writing/sections/intro.md",
            "method": "writing/sections/method.md",
        }
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
        ws.write_file("writing/sections/intro.md", "# Introduction\n" * 100)
        ws.complete_checkpoint_step("writing/sections", "intro")
        cp = ws.load_checkpoint("writing/sections")
        assert cp["steps"]["intro"]["status"] == "completed"
        assert cp["steps"]["intro"]["file_size"] > 0
        assert cp["steps"]["intro"]["file_mtime"] > 0

    def test_complete_step_with_artifacts(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "# Introduction\n" * 100)
        ws.write_file("writing/figures/gen_intro.py", "print('ok')\n")
        ws.write_file("writing/figures/intro.pdf", "%PDF-1.4 mock\n")

        result = ws.complete_checkpoint_step(
            "writing/sections",
            "intro",
            artifacts=[
                "writing/figures/gen_intro.py",
                "writing/figures/intro.pdf",
            ],
        )

        assert result["completed"] is True
        cp = ws.load_checkpoint("writing/sections")
        assert [a["path"] for a in cp["steps"]["intro"]["artifacts"]] == [
            "writing/figures/gen_intro.py",
            "writing/figures/intro.pdf",
        ]

    def test_missing_artifact_prevents_completion(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "# Introduction\n" * 100)

        result = ws.complete_checkpoint_step(
            "writing/sections",
            "intro",
            artifacts=["writing/figures/missing.pdf"],
        )

        assert result["completed"] is False
        assert result["missing_files"] == ["writing/figures/missing.pdf"]
        cp = ws.load_checkpoint("writing/sections")
        assert cp["steps"]["intro"]["status"] == "pending"


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
        cp["steps"]["intro"]["file_mtime"] = (
            ws.root / "writing/sections/intro.md"
        ).stat().st_mtime
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

    def test_missing_artifact_invalidates_completed_step(self, ws):
        steps = {"intro": "writing/sections/intro.md"}
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "content")
        ws.write_file("writing/figures/intro.pdf", "%PDF-1.4 mock\n")
        ws.complete_checkpoint_step(
            "writing/sections",
            "intro",
            artifacts=["writing/figures/intro.pdf"],
        )

        (ws.root / "writing/figures/intro.pdf").unlink()
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

    def test_mixed_completed_and_pending(self, ws):
        """Some steps completed, some still pending."""
        steps = {
            "intro": "writing/sections/intro.md",
            "method": "writing/sections/method.md",
            "conclusion": "writing/sections/conclusion.md",
        }
        ws.create_checkpoint("writing_sections", "writing/sections", steps, iteration=1)
        ws.write_file("writing/sections/intro.md", "done intro")
        ws.complete_checkpoint_step("writing/sections", "intro")
        valid = ws.validate_checkpoint("writing/sections")
        assert valid["completed"] == ["intro"]
        assert set(valid["remaining"]) == {"method", "conclusion"}
