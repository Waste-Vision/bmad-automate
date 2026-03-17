"""Tests for orchestrator.py — story grouping, StatusManager, and Orchestrator."""

from __future__ import annotations

from unittest.mock import patch

from bmad_automate.context import RunContext
from bmad_automate.events import STORY_DONE, PipelineEvent
from bmad_automate.models import StepResult, StepStatus, StoryResult, StoryStatus
from bmad_automate.orchestrator import (
    Orchestrator,
    StatusManager,
    _group_stories_by_epic,
)


# ---------------------------------------------------------------------------
# _group_stories_by_epic
# ---------------------------------------------------------------------------


class TestGroupStoriesByEpic:
    def test_groups_correctly(self):
        stories = ["1-1-setup", "1-2-auth", "2-1-api", "3-1-dash"]
        result = _group_stories_by_epic(stories)
        assert result == {
            1: ["1-1-setup", "1-2-auth"],
            2: ["2-1-api"],
            3: ["3-1-dash"],
        }

    def test_empty_list(self):
        assert _group_stories_by_epic([]) == {}

    def test_sorted_by_epic(self):
        stories = ["3-1-a", "1-1-b", "2-1-c"]
        result = _group_stories_by_epic(stories)
        assert list(result.keys()) == [1, 2, 3]

    def test_preserves_story_order_within_epic(self):
        stories = ["1-3-c", "1-1-a", "1-2-b"]
        result = _group_stories_by_epic(stories)
        assert result[1] == ["1-3-c", "1-1-a", "1-2-b"]

    def test_invalid_keys_ignored(self):
        stories = ["1-1-setup", "not-a-story", "epic-1-retro"]
        result = _group_stories_by_epic(stories)
        assert result == {1: ["1-1-setup"]}


# ---------------------------------------------------------------------------
# StatusManager
# ---------------------------------------------------------------------------


class TestStatusManager:
    def test_update_forward_only(self):
        sm = StatusManager()
        assert sm.update("1-1-setup", "in-progress") is True
        assert sm.update("1-1-setup", "done") is True
        assert sm.get("1-1-setup") == "done"

    def test_backward_update_rejected(self):
        sm = StatusManager()
        sm.update("1-1-setup", "done")
        assert sm.update("1-1-setup", "backlog") is False
        assert sm.get("1-1-setup") == "done"

    def test_same_status_rejected(self):
        sm = StatusManager()
        sm.update("1-1-setup", "in-progress")
        assert sm.update("1-1-setup", "in-progress") is False

    def test_get_default(self):
        sm = StatusManager()
        assert sm.get("unknown") == "backlog"

    def test_get_all(self):
        sm = StatusManager()
        sm.update("1-1-a", "done")
        sm.update("2-1-b", "in-progress")
        all_statuses = sm.get_all()
        assert all_statuses == {"1-1-a": "done", "2-1-b": "in-progress"}

    def test_get_all_returns_snapshot(self):
        sm = StatusManager()
        sm.update("1-1-a", "done")
        snapshot = sm.get_all()
        sm.update("2-1-b", "done")
        # Snapshot should not include the later update
        assert "2-1-b" not in snapshot

    def test_load_from_yaml(self):
        sm = StatusManager()
        sm.load_from_yaml({
            "development_status": {
                "1-1-setup": "done",
                "2-1-api": "in-progress",
            }
        })
        assert sm.get("1-1-setup") == "done"
        assert sm.get("2-1-api") == "in-progress"

    def test_load_empty_yaml(self):
        sm = StatusManager()
        sm.load_from_yaml({})
        assert sm.get_all() == {}

    def test_unknown_status_transitions_to_known(self):
        sm = StatusManager()
        # Default is "backlog" (order 0), so updating to "in-progress" (order 2) works
        assert sm.update("1-1-a", "in-progress") is True
        assert sm.get("1-1-a") == "in-progress"


# ---------------------------------------------------------------------------
# Orchestrator (sequential mode)
# ---------------------------------------------------------------------------


class TestOrchestratorSequential:
    @patch("bmad_automate.worker.process_story")
    def test_sequential_processes_all(self, mock_process, make_config):
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        mock_process.return_value = StoryResult(
            key="1-1-setup", status=StoryStatus.COMPLETED,
            steps=[StepResult(name="dev", status=StepStatus.SUCCESS)],
            duration=1.0,
        )

        stories = ["1-1-setup", "1-2-auth"]
        orch = Orchestrator(stories, {}, cfg, ctx)
        results = orch.run_sequential()

        assert len(results) == 2
        assert mock_process.call_count == 2

    @patch("bmad_automate.worker.process_story")
    def test_sequential_stops_on_failure(self, mock_process, make_config):
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        def side_effect(story_key, config, ctx_arg, story_status=""):
            if story_key == "1-1-setup":
                return StoryResult(
                    key=story_key, status=StoryStatus.FAILED,
                    failed_step="dev",
                )
            return StoryResult(key=story_key, status=StoryStatus.COMPLETED)

        mock_process.side_effect = side_effect

        stories = ["1-1-setup", "1-2-auth"]
        orch = Orchestrator(stories, {}, cfg, ctx)
        results = orch.run_sequential()

        assert len(results) == 1
        assert results[0].status == StoryStatus.FAILED

    @patch("bmad_automate.worker.process_story")
    def test_sequential_stops_on_interrupt(self, mock_process, make_config):
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        def side_effect(story_key, config, ctx_arg, story_status=""):
            ctx.interrupted = True
            return StoryResult(key=story_key, status=StoryStatus.COMPLETED)

        mock_process.side_effect = side_effect

        stories = ["1-1-setup", "2-1-api"]
        orch = Orchestrator(stories, {}, cfg, ctx)
        results = orch.run_sequential()

        # First epic processes, but second should be skipped due to interrupt
        assert len(results) == 1

    @patch("bmad_automate.worker.process_story")
    def test_run_dispatches_to_sequential(self, mock_process, make_config):
        """With parallel_epics=1, run() should use sequential mode."""
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        mock_process.return_value = StoryResult(
            key="1-1-setup", status=StoryStatus.COMPLETED, duration=1.0,
        )

        stories = ["1-1-setup"]
        orch = Orchestrator(stories, {}, cfg, ctx)
        results = orch.run()

        assert len(results) == 1

    @patch("bmad_automate.worker.process_story")
    def test_registers_epics_with_run_control(self, mock_process, make_config):
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        mock_process.return_value = StoryResult(
            key="1-1-a", status=StoryStatus.COMPLETED, duration=1.0,
        )

        stories = ["1-1-a", "2-1-b"]
        orch = Orchestrator(stories, {}, cfg, ctx)
        orch.run_sequential()

        # Both epics (1 and 2) should be registered
        assert ctx.run_control.is_paused(1) is False  # registered, not paused
        assert ctx.run_control.is_paused(2) is False  # registered, not paused
        # Verify they respond to pause (proves they were registered)
        ctx.run_control.pause_epic(1)
        assert ctx.run_control.is_paused(1) is True
        ctx.run_control.pause_epic(2)
        assert ctx.run_control.is_paused(2) is True

    @patch("bmad_automate.worker.process_story")
    def test_status_manager_updates_from_events(self, mock_process, make_config):
        cfg = make_config(parallel_epics=1)
        ctx = RunContext(config=cfg)

        mock_process.return_value = StoryResult(
            key="1-1-a", status=StoryStatus.COMPLETED, duration=1.0,
        )

        stories = ["1-1-a"]
        orch = Orchestrator(stories, {}, cfg, ctx)

        # Simulate a STORY_DONE event
        ctx.event_bus.emit(PipelineEvent(
            epic=1, story="1-1-a", step=None,
            kind=STORY_DONE,
            payload={"status": "done"},
        ))
        ctx.event_bus.drain()

        assert orch.status_manager.get("1-1-a") == "done"
