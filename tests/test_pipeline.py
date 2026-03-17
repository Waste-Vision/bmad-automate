"""Tests for pipeline.py — story processing and after-epic pipeline.

Tests mock at the subprocess boundary (subprocess.run) rather than mocking
internal functions, so we verify actual orchestration logic.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bmad_automate.context import RunContext
from bmad_automate.events import (
    STEP_SKIPPED,
    STORY_DONE,
    STORY_START,
    PipelineEvent,
)
from bmad_automate.models import Config, StepStatus, StoryStatus
from bmad_automate.pipeline import (
    process_story,
    run_after_epic_pipeline,
    run_course_correction,
    run_next_epic_preparation,
    run_retro_implementation,
    run_retrospective,
)
from bmad_automate.stories import invalidate_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _subprocess_ok(*args, **kwargs):
    """Fake subprocess.run that always succeeds."""
    return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")


# ---------------------------------------------------------------------------
# process_story
# ---------------------------------------------------------------------------


class TestProcessStory:
    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_all_steps_succeed(self, mock_run, config, ctx):
        result = process_story("3-1-feature", config, ctx)

        assert result.status == StoryStatus.COMPLETED
        assert result.key == "3-1-feature"
        # 4 AI steps (create, dev, review, commit) + git-pull = 5
        step_names = [s.name for s in result.steps]
        assert step_names == [
            "create-story", "dev-story", "code-review", "git-commit", "git-pull"
        ]
        assert all(
            s.status in (StepStatus.SUCCESS, StepStatus.SKIPPED)
            for s in result.steps
        )

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_marks_story_done_on_success(self, mock_run, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(textwrap.dedent("""\
            development_status:
              3-1-feature: in-progress
        """), encoding="utf-8")

        cfg = Config(
            sprint_status=ss,
            story_dir=tmp_path,
            log_file=tmp_path / "test.log",
            bmad_dir=tmp_path / "_bmad",
            quiet=True, yes=True,
        )
        ctx = RunContext(config=cfg)

        process_story("3-1-feature", cfg, ctx)

        content = ss.read_text(encoding="utf-8")
        assert "3-1-feature: done" in content

    @patch("bmad_automate.git.subprocess.run")
    def test_step_failure_stops_pipeline(self, mock_run, make_config):
        # Use retries=0 so failures are immediate
        cfg = make_config(retries=0)
        test_ctx = RunContext(config=cfg)

        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="dev failed")
            return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

        mock_run.side_effect = fail_second

        result = process_story("3-1-feature", cfg, test_ctx)

        assert result.status == StoryStatus.FAILED
        assert result.failed_step == "dev-story"
        # create (success) + dev (failed) = 2
        assert len(result.steps) == 2
        assert result.steps[0].status == StepStatus.SUCCESS
        assert result.steps[1].status == StepStatus.FAILED

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_skip_create_when_file_exists(self, mock_run, config, ctx):
        story_file = config.story_dir / "3-1-feature.md"
        story_file.write_text("# Story", encoding="utf-8")

        result = process_story("3-1-feature", config, ctx)

        assert result.status == StoryStatus.COMPLETED
        create_step = next(s for s in result.steps if s.name == "create-story")
        assert create_step.status == StepStatus.SKIPPED
        # Other steps should still run
        dev_step = next(s for s in result.steps if s.name == "dev-story")
        assert dev_step.status == StepStatus.SUCCESS

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_review_status_skips_create_and_dev(self, mock_run, config, ctx):
        result = process_story("3-1-feature", config, ctx, story_status="review")

        assert result.status == StoryStatus.COMPLETED
        create_step = next(s for s in result.steps if s.name == "create-story")
        dev_step = next(s for s in result.steps if s.name == "dev-story")
        assert create_step.status == StepStatus.SKIPPED
        assert dev_step.status == StepStatus.SKIPPED
        # Review and commit should still run
        review_step = next(s for s in result.steps if s.name == "code-review")
        assert review_step.status == StepStatus.SUCCESS

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_skip_flags_respected(self, mock_run, make_config):
        cfg = make_config(skip_create=True, skip_review=True)
        ctx = RunContext(config=cfg)

        result = process_story("3-1-feature", cfg, ctx)

        create_step = next(s for s in result.steps if s.name == "create-story")
        review_step = next(s for s in result.steps if s.name == "code-review")
        assert create_step.status == StepStatus.SKIPPED
        assert review_step.status == StepStatus.SKIPPED
        # Non-skipped steps should still succeed
        dev_step = next(s for s in result.steps if s.name == "dev-story")
        assert dev_step.status == StepStatus.SUCCESS

    def test_dry_run_skips_all(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)

        result = process_story("3-1-feature", cfg, ctx)

        assert result.status == StoryStatus.SKIPPED
        assert all(s.status == StepStatus.SKIPPED for s in result.steps)
        assert len(result.steps) == 5

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_interrupted_stops_processing(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)

        call_count = 0

        def interrupt_after_first(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                ctx.interrupted = True
            return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

        mock_run.side_effect = interrupt_after_first

        result = process_story("3-1-feature", cfg, ctx)

        # Only first step should run before interrupt is detected
        assert call_count == 1
        # Result should reflect incomplete processing
        assert len(result.steps) <= 2  # at most create + skipped steps

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_emits_story_events(self, mock_run, config, ctx):
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        process_story("3-1-feature", config, ctx)
        ctx.event_bus.drain()

        start_events = [e for e in collected if e.kind == STORY_START]
        assert len(start_events) == 1
        assert start_events[0].story == "3-1-feature"
        assert start_events[0].epic == 3

        done_events = [e for e in collected if e.kind == STORY_DONE]
        assert len(done_events) == 1
        assert done_events[0].payload["status"] == "completed"
        assert "duration" in done_events[0].payload
        assert done_events[0].story == "3-1-feature"
        assert done_events[0].epic == 3

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_emits_skip_events(self, mock_run, make_config):
        cfg = make_config(skip_create=True)
        ctx = RunContext(config=cfg)
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        process_story("3-1-feature", cfg, ctx)
        ctx.event_bus.drain()

        skip_events = [e for e in collected if e.kind == STEP_SKIPPED]
        assert len(skip_events) >= 1
        skipped_steps = {e.step for e in skip_events}
        assert "create-story" in skipped_steps

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_does_not_mark_done_on_failure(self, mock_run, tmp_path):
        """Failed stories should NOT be marked as done."""
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(textwrap.dedent("""\
            development_status:
              3-1-feature: in-progress
        """), encoding="utf-8")

        cfg = Config(
            sprint_status=ss,
            story_dir=tmp_path,
            log_file=tmp_path / "test.log",
            bmad_dir=tmp_path / "_bmad",
            quiet=True, yes=True, retries=0,
        )
        ctx = RunContext(config=cfg)

        # Make the first call fail
        mock_run.side_effect = [
            subprocess.CompletedProcess("cmd", 1, stdout="", stderr="fail"),
        ]

        result = process_story("3-1-feature", cfg, ctx)
        assert result.status == StoryStatus.FAILED

        content = ss.read_text(encoding="utf-8")
        assert "3-1-feature: in-progress" in content


# ---------------------------------------------------------------------------
# After-epic step runners
# ---------------------------------------------------------------------------


class TestAfterEpicSteps:
    def test_retrospective_dry_run(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_retrospective(1, cfg, ctx)
        assert result.status == StepStatus.SKIPPED
        assert result.name == "retro-epic-1"

    def test_course_correction_dry_run(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_course_correction(1, cfg, ctx)
        assert result.status == StepStatus.SKIPPED
        assert result.name == "course-correct-epic-1"

    def test_retro_implementation_dry_run(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_retro_implementation(1, cfg, ctx)
        assert result.status == StepStatus.SKIPPED
        assert result.name == "retro-impl-epic-1"

    def test_next_epic_preparation_dry_run(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_next_epic_preparation(1, cfg, ctx)
        assert result.status == StepStatus.SKIPPED
        assert result.name == "prep-next-epic-2"

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_retrospective_runs_step(self, mock_run, config, ctx):
        result = run_retrospective(1, config, ctx)
        assert result.status == StepStatus.SUCCESS
        assert result.name == "retro-epic-1"

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_course_correction_runs_step(self, mock_run, config, ctx):
        result = run_course_correction(2, config, ctx)
        assert result.status == StepStatus.SUCCESS
        assert result.name == "course-correct-epic-2"

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_next_epic_uses_incremented_number(self, mock_run, config, ctx):
        result = run_next_epic_preparation(3, config, ctx)
        assert result.name == "prep-next-epic-4"


# ---------------------------------------------------------------------------
# After-epic pipeline
# ---------------------------------------------------------------------------


class TestAfterEpicPipeline:
    def test_dry_run_pipeline(self, tmp_path):
        # Use a sprint-status with no epic 2, so next-epic-prep is skipped
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text("development_status:\n  1-1-setup: done\n", encoding="utf-8")
        cfg = Config(
            sprint_status=ss,
            story_dir=tmp_path,
            log_file=tmp_path / "test.log",
            bmad_dir=tmp_path / "_bmad",
            quiet=True, yes=True, dry_run=True,
        )
        ctx = RunContext(config=cfg)
        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)
        # retro + course-correct + retro-impl = 3 (no next-epic since no epic 2)
        assert len(retro_results) == 3
        assert all(r.status == StepStatus.SKIPPED for r in retro_results)
        names = [r.name for r in retro_results]
        assert "retro-epic-1" in names
        assert "course-correct-epic-1" in names
        assert "retro-impl-epic-1" in names

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_pipeline_runs_all_steps(self, mock_run, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(textwrap.dedent("""\
            development_status:
              1-1-setup: done
              2-1-data: backlog
        """), encoding="utf-8")
        cfg = Config(
            sprint_status=ss,
            story_dir=tmp_path,
            log_file=tmp_path / "test.log",
            bmad_dir=tmp_path / "_bmad",
            quiet=True, yes=True,
        )
        ctx = RunContext(config=cfg)

        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)

        # retro + course-correct + retro-impl + next-epic-prep + commit = 5
        assert len(retro_results) == 5
        step_names = [r.name for r in retro_results]
        assert "retro-epic-1" in step_names
        assert "course-correct-epic-1" in step_names
        assert "retro-impl-epic-1" in step_names
        assert "prep-next-epic-2" in step_names
        assert "after-epic-commit-1" in step_names

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_skip_retro_still_runs_others(self, mock_run, make_config):
        cfg = make_config(skip_retro=True)
        ctx = RunContext(config=cfg)
        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)
        step_names = [r.name for r in retro_results]
        assert not any("retro-epic" in n for n in step_names)
        # Course-correct and retro-impl should still run
        assert any("course-correct" in n for n in step_names)
        assert any("retro-impl" in n for n in step_names)

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_skip_course_correct_still_runs_others(self, mock_run, make_config):
        cfg = make_config(skip_course_correct=True)
        ctx = RunContext(config=cfg)
        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)
        step_names = [r.name for r in retro_results]
        assert not any("course-correct" in n for n in step_names)
        assert any("retro-epic" in n for n in step_names)
        assert any("retro-impl" in n for n in step_names)

    @patch("bmad_automate.git.subprocess.run", side_effect=_subprocess_ok)
    def test_skip_retro_impl_still_runs_others(self, mock_run, make_config):
        cfg = make_config(skip_retro_impl=True)
        ctx = RunContext(config=cfg)
        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)
        step_names = [r.name for r in retro_results]
        assert not any("retro-impl" in n for n in step_names)
        assert any("retro-epic" in n for n in step_names)
        assert any("course-correct" in n for n in step_names)

    def test_interrupted_stops_pipeline(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        ctx.interrupted = True
        retro_results = []
        run_after_epic_pipeline(1, cfg, ctx, retro_results)
        assert len(retro_results) == 0
