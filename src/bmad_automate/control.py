"""RunControl — per-epic pause/resume/abort replacing simple interrupted flag."""

from __future__ import annotations

import threading


class RunControl:
    """Thread-safe run control with per-epic granularity.

    Each epic gets its own pause flags and threading.Event.
    Sequential mode simply registers a single epic (or epic 0 as sentinel).
    """

    def __init__(self) -> None:
        self._abort_event = threading.Event()
        self._pause_after_step: dict[int, bool] = {}
        self._pause_after_story: dict[int, bool] = {}
        self._epic_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    @property
    def global_abort(self) -> bool:
        """Thread-safe check — backed by threading.Event for memory barrier."""
        return self._abort_event.is_set()

    @global_abort.setter
    def global_abort(self, value: bool) -> None:
        if value:
            self._abort_event.set()
        else:
            self._abort_event.clear()

    def register_epic(self, epic_num: int) -> None:
        """Initialize control state for an epic."""
        with self._lock:
            if epic_num not in self._epic_events:
                event = threading.Event()
                event.set()  # not paused by default
                self._epic_events[epic_num] = event
                self._pause_after_step[epic_num] = False
                self._pause_after_story[epic_num] = False

    def should_stop(self, epic_num: int) -> bool:
        """Check whether processing should stop for this epic."""
        return self._abort_event.is_set()

    def abort(self) -> None:
        """Signal a global abort — unblock all paused workers."""
        self._abort_event.set()
        with self._lock:
            for event in self._epic_events.values():
                event.set()  # unblock any waiting workers

    def pause_epic(self, epic_num: int) -> None:
        """Pause a specific epic."""
        with self._lock:
            event = self._epic_events.get(epic_num)
            if event:
                event.clear()

    def resume_epic(self, epic_num: int) -> None:
        """Resume a paused epic."""
        with self._lock:
            event = self._epic_events.get(epic_num)
            if event:
                event.set()
            self._pause_after_step[epic_num] = False
            self._pause_after_story[epic_num] = False

    def set_pause_after_step(self, epic_num: int, value: bool = True) -> None:
        """Set flag to pause after the current step completes."""
        with self._lock:
            self._pause_after_step[epic_num] = value

    def set_pause_after_story(self, epic_num: int, value: bool = True) -> None:
        """Set flag to pause after the current story completes."""
        with self._lock:
            self._pause_after_story[epic_num] = value

    def wait_if_paused(self, epic_num: int, timeout: float | None = None) -> bool:
        """Block until the epic is unpaused. Returns False if timed out."""
        event = self._epic_events.get(epic_num)
        if event is None:
            return True
        return event.wait(timeout=timeout)

    def check_pause_after_step(self, epic_num: int) -> None:
        """If pause-after-step is flagged, pause the epic now."""
        with self._lock:
            if self._pause_after_step.get(epic_num, False):
                self._pause_after_step[epic_num] = False
                event = self._epic_events.get(epic_num)
                if event:
                    event.clear()

    def check_pause_after_story(self, epic_num: int) -> None:
        """If pause-after-story is flagged, pause the epic now."""
        with self._lock:
            if self._pause_after_story.get(epic_num, False):
                self._pause_after_story[epic_num] = False
                event = self._epic_events.get(epic_num)
                if event:
                    event.clear()

    def is_paused(self, epic_num: int) -> bool:
        """Check if an epic is currently paused."""
        event = self._epic_events.get(epic_num)
        if event is None:
            return False
        return not event.is_set()

    def has_subscribers(self) -> bool:
        """Check if any epics are registered."""
        with self._lock:
            return len(self._epic_events) > 0


# Module-level reference for signal handler access.
_active_control: RunControl | None = None


def set_active_control(ctrl: RunControl) -> None:
    """Register the active RunControl (for signal handler)."""
    global _active_control
    _active_control = ctrl


def get_active_control() -> RunControl | None:
    """Return the active RunControl, or None."""
    return _active_control
