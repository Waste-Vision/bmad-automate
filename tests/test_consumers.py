"""Tests for consumers.py — CliConsumer event dispatch."""

from __future__ import annotations

from unittest.mock import patch

from bmad_automate.consumers import CliConsumer
from bmad_automate.events import (
    LOG_LINE,
    LOG_MESSAGE,
    STEP_DONE,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_START,
    STORY_DONE,
    STORY_START,
    PipelineEvent,
)


class TestCliConsumer:
    def _make_event(self, kind: str, **kwargs) -> PipelineEvent:
        return PipelineEvent(
            epic=kwargs.get("epic", 1),
            story=kwargs.get("story", "1-1-foo"),
            step=kwargs.get("step", "dev-story"),
            kind=kind,
            payload=kwargs.get("payload", {}),
        )

    def test_dispatches_step_start(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            STEP_START, payload={"attempt": 0, "retries": 1}
        )
        # Should not raise
        consumer(event)

    def test_dispatches_step_done(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            STEP_DONE, payload={"duration": 5.0}
        )
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_called_once()
            logged = mock_log.call_args[0][0]
            assert "SUCCESS" in logged
            assert "dev-story" in logged

    def test_dispatches_step_failed(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            STEP_FAILED, payload={"error": "timeout"}
        )
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_called_once()
            logged = mock_log.call_args[0][0]
            assert "FAILED" in logged
            assert "timeout" in logged

    def test_dispatches_step_skipped(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            STEP_SKIPPED, payload={"message": "Story file exists"}
        )
        # Should not raise
        consumer(event)

    def test_dispatches_step_skipped_no_message(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(STEP_SKIPPED, payload={})
        consumer(event)

    def test_dispatches_log_line(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            LOG_LINE,
            payload={"label": "run_step", "stream": "STDOUT", "content": "output"},
        )
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_called_once()
            logged = mock_log.call_args[0][0]
            assert "output" in logged

    def test_dispatches_log_message(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            LOG_MESSAGE, payload={"message": "starting pipeline"}
        )
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_called_once()

    def test_log_message_empty_is_noop(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(LOG_MESSAGE, payload={"message": ""})
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_not_called()

    def test_log_line_empty_content_is_noop(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            LOG_LINE, payload={"label": "", "stream": "", "content": ""}
        )
        with patch("bmad_automate.consumers.log_to_file") as mock_log:
            consumer(event)
            mock_log.assert_not_called()

    def test_unknown_event_kind_is_noop(self, config):
        consumer = CliConsumer(config)
        event = self._make_event("totally_unknown_kind")
        consumer(event)  # should not raise

    def test_quiet_mode_suppresses_step_start(self, config):
        consumer = CliConsumer(config, quiet=True)
        event = self._make_event(
            STEP_START, payload={"attempt": 0, "retries": 0}
        )
        # In quiet mode, _on_step_start returns early
        consumer(event)

    def test_quiet_mode_suppresses_step_skipped(self, config):
        consumer = CliConsumer(config, quiet=True)
        event = self._make_event(STEP_SKIPPED, payload={})
        consumer(event)

    def test_retry_attempt_shown_in_step_start(self, config):
        consumer = CliConsumer(config)
        event = self._make_event(
            STEP_START, payload={"attempt": 2, "retries": 3}
        )
        # Should not raise — shows "(attempt 3/4)" format
        consumer(event)
