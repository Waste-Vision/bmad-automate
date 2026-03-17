"""Tests for control.py — RunControl."""

from __future__ import annotations

import threading

from bmad_automate.control import RunControl


class TestRunControl:
    def test_register_and_should_stop(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        assert ctrl.should_stop(1) is False

    def test_abort_sets_global_flag(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.abort()
        assert ctrl.should_stop(1) is True
        assert ctrl.global_abort is True

    def test_pause_and_resume(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        assert ctrl.is_paused(1) is False

        ctrl.pause_epic(1)
        assert ctrl.is_paused(1) is True

        ctrl.resume_epic(1)
        assert ctrl.is_paused(1) is False

    def test_wait_if_paused_returns_immediately_when_not_paused(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        assert ctrl.wait_if_paused(1, timeout=0.01) is True

    def test_wait_if_paused_blocks_when_paused(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.pause_epic(1)

        # Should time out
        assert ctrl.wait_if_paused(1, timeout=0.01) is False

    def test_wait_if_paused_unblocks_on_resume(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.pause_epic(1)

        result = [False]

        def waiter():
            result[0] = ctrl.wait_if_paused(1, timeout=2.0)

        t = threading.Thread(target=waiter)
        t.start()
        ctrl.resume_epic(1)
        t.join(timeout=2.0)
        assert result[0] is True

    def test_abort_unblocks_paused_workers(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.pause_epic(1)

        result = [False]

        def waiter():
            result[0] = ctrl.wait_if_paused(1, timeout=2.0)

        t = threading.Thread(target=waiter)
        t.start()
        ctrl.abort()
        t.join(timeout=2.0)
        assert result[0] is True

    def test_independent_epic_pause(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.register_epic(2)

        ctrl.pause_epic(1)
        assert ctrl.is_paused(1) is True
        assert ctrl.is_paused(2) is False

    def test_check_pause_after_step(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.set_pause_after_step(1, True)
        ctrl.check_pause_after_step(1)
        assert ctrl.is_paused(1) is True

    def test_check_pause_after_story(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.set_pause_after_story(1, True)
        ctrl.check_pause_after_story(1)
        assert ctrl.is_paused(1) is True

    def test_resume_clears_pause_flags(self):
        ctrl = RunControl()
        ctrl.register_epic(1)
        ctrl.set_pause_after_step(1, True)
        ctrl.set_pause_after_story(1, True)
        ctrl.resume_epic(1)
        # Flags should be cleared
        ctrl.check_pause_after_step(1)
        assert ctrl.is_paused(1) is False

    def test_unregistered_epic_wait_returns_true(self):
        ctrl = RunControl()
        # Unregistered epic should not block
        assert ctrl.wait_if_paused(99) is True

    def test_unregistered_epic_is_not_paused(self):
        ctrl = RunControl()
        assert ctrl.is_paused(99) is False


class TestRunContextInterruptedCompat:
    def test_interrupted_reads_from_run_control(self):
        from bmad_automate.context import RunContext
        from bmad_automate.models import Config

        ctx = RunContext(config=Config())
        assert ctx.interrupted is False
        ctx.run_control.abort()
        assert ctx.interrupted is True

    def test_interrupted_setter_calls_abort(self):
        from bmad_automate.context import RunContext
        from bmad_automate.models import Config

        ctx = RunContext(config=Config())
        ctx.interrupted = True
        assert ctx.run_control.global_abort is True

    def test_interrupted_setter_false_resets(self):
        from bmad_automate.context import RunContext
        from bmad_automate.models import Config

        ctx = RunContext(config=Config())
        ctx.interrupted = True
        ctx.interrupted = False
        assert ctx.run_control.global_abort is False
