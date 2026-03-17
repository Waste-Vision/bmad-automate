"""Tests for worker.py — EpicWorker story processing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bmad_automate.context import RunContext
from bmad_automate.events import EPIC_DONE, EPIC_START, PipelineEvent
from bmad_automate.models import (
    Config,
    StepResult,
    StepStatus,
    StoryResult,
    StoryStatus,
)
from bmad_automate.worker import EpicWorker


def _make_story_result(key: str, status: StoryStatus = StoryStatus.COMPLETED) -> StoryResult:
    return StoryResult(
        key=key,
        status=status,
        steps=[StepResult(name="dev", status=StepStatus.SUCCESS)],
        duration=1.0,
    )


class TestEpicWorker:
    @patch("bmad_automate.worker.process_story")
    def test_processes_all_stories(self, mock_process, config, ctx):
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k)
        ctx.run_control.register_epic(1)

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup", "1-2-auth"],
            story_status_map={"1-1-setup": "ready-for-dev", "1-2-auth": "backlog"},
            config=config,
            ctx=ctx,
        )
        results = worker.run()

        assert len(results) == 2
        assert all(r.status == StoryStatus.COMPLETED for r in results)
        assert mock_process.call_count == 2

    @patch("bmad_automate.worker.process_story")
    def test_stops_on_failure(self, mock_process, config, ctx):
        def side_effect(key, cfg, cx, status=""):
            if key == "1-1-setup":
                return _make_story_result(key, StoryStatus.FAILED)
            return _make_story_result(key)

        mock_process.side_effect = side_effect
        ctx.run_control.register_epic(1)

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup", "1-2-auth"],
            story_status_map={},
            config=config,
            ctx=ctx,
        )
        results = worker.run()

        assert len(results) == 1
        assert results[0].status == StoryStatus.FAILED

    @patch("bmad_automate.worker.process_story")
    def test_stops_on_global_abort(self, mock_process, config, ctx):
        def abort_after_first(key, cfg, cx, status=""):
            ctx.run_control.abort()
            return _make_story_result(key)

        mock_process.side_effect = abort_after_first
        ctx.run_control.register_epic(2)

        worker = EpicWorker(
            epic_num=2,
            stories=["2-1-api", "2-2-data"],
            story_status_map={},
            config=config,
            ctx=ctx,
        )
        results = worker.run()

        assert len(results) == 1

    @patch("bmad_automate.worker.process_story")
    def test_emits_epic_start_and_done_events(self, mock_process, config, ctx):
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k)
        ctx.run_control.register_epic(1)

        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup"],
            story_status_map={},
            config=config,
            ctx=ctx,
        )
        worker.run()
        ctx.event_bus.drain()

        kinds = [e.kind for e in collected]
        assert EPIC_START in kinds
        assert EPIC_DONE in kinds

        done_event = next(e for e in collected if e.kind == EPIC_DONE)
        assert done_event.payload["stories_completed"] == 1
        assert done_event.payload["stories_failed"] == 0

    @patch("bmad_automate.worker.process_story")
    def test_epic_done_payload_counts_failures(self, mock_process, config, ctx):
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k, StoryStatus.FAILED)
        ctx.run_control.register_epic(1)

        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup"],
            story_status_map={},
            config=config,
            ctx=ctx,
        )
        worker.run()
        ctx.event_bus.drain()

        done_event = next(e for e in collected if e.kind == EPIC_DONE)
        assert done_event.payload["stories_failed"] == 1
        assert done_event.payload["stories_completed"] == 0

    @patch("bmad_automate.worker.process_story")
    def test_passes_story_status(self, mock_process, config, ctx):
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k)
        ctx.run_control.register_epic(1)

        status_map = {"1-1-setup": "review"}
        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup"],
            story_status_map=status_map,
            config=config,
            ctx=ctx,
        )
        worker.run()

        # Verify process_story was called with the story status from the map
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args
        # process_story(story_key, config, ctx, story_status)
        actual_story_key = call_kwargs[0][0]
        assert actual_story_key == "1-1-setup"
        # The story_status is the last positional arg
        actual_status = call_kwargs[0][-1]
        assert actual_status == "review"


class TestEpicWorkerWorktree:
    @patch("bmad_automate.worker.process_story")
    def test_worktree_rescopes_config_paths(self, mock_process, tmp_path):
        # Use relative paths in config so worktree re-scoping works
        cfg = Config(
            sprint_status=Path("sprint-status.yaml"),
            story_dir=Path("stories"),
            bmad_dir=Path("_bmad"),
            log_file=tmp_path / "test.log",
            quiet=True, yes=True,
        )
        ctx = RunContext(config=cfg)
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k)
        ctx.run_control.register_epic(1)

        wt_path = tmp_path / "worktrees" / "epic-1"

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup"],
            story_status_map={},
            config=cfg,
            ctx=ctx,
            worktree_path=wt_path,
        )

        # Config should be re-scoped
        assert worker.config is not cfg  # should be a copy
        assert worker.config.project_root == wt_path
        assert wt_path in worker.config.sprint_status.parents or worker.config.sprint_status.parent == wt_path
        assert wt_path in worker.config.story_dir.parents or worker.config.story_dir.parent == wt_path
        assert wt_path in worker.config.bmad_dir.parents or worker.config.bmad_dir.parent == wt_path

    @patch("bmad_automate.worker.process_story")
    def test_no_worktree_uses_original_config(self, mock_process, config, ctx):
        mock_process.side_effect = lambda k, c, cx, s="": _make_story_result(k)
        ctx.run_control.register_epic(1)

        worker = EpicWorker(
            epic_num=1,
            stories=["1-1-setup"],
            story_status_map={},
            config=config,
            ctx=ctx,
            worktree_path=None,
        )

        assert worker.config is config
