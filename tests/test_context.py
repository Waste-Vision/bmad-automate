"""Tests for context.py — RunContext and global context management."""

from __future__ import annotations

from bmad_automate.context import (
    RunContext,
    get_active_context,
    set_active_context,
)
from bmad_automate.control import RunControl
from bmad_automate.events import EventBus
from bmad_automate.models import Config, StoryResult, StoryStatus


class TestRunContext:
    def test_defaults(self, config):
        ctx = RunContext(config=config)
        assert ctx.results == []
        assert ctx.start_time == 0.0
        assert isinstance(ctx.event_bus, EventBus)
        assert ctx.log_broker is None
        assert isinstance(ctx.run_control, RunControl)

    def test_interrupted_reads_global_abort(self, config):
        ctx = RunContext(config=config)
        assert ctx.interrupted is False
        ctx.run_control.abort()
        assert ctx.interrupted is True

    def test_interrupted_setter_true(self, config):
        ctx = RunContext(config=config)
        ctx.interrupted = True
        assert ctx.run_control.global_abort is True

    def test_interrupted_setter_false(self, config):
        ctx = RunContext(config=config)
        ctx.interrupted = True
        ctx.interrupted = False
        assert ctx.run_control.global_abort is False

    def test_results_are_mutable(self, config):
        ctx = RunContext(config=config)
        ctx.results.append(
            StoryResult(key="1-1-a", status=StoryStatus.COMPLETED)
        )
        assert len(ctx.results) == 1


class TestActiveContext:
    def test_set_and_get(self, config):
        ctx = RunContext(config=config)
        set_active_context(ctx)
        assert get_active_context() is ctx

    def test_default_is_none(self):
        # Reset to clean state
        import bmad_automate.context as mod
        original = mod._ctx
        mod._ctx = None
        try:
            assert get_active_context() is None
        finally:
            mod._ctx = original
