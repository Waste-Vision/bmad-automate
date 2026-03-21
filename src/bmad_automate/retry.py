"""RetryController — coordinated retry with manual UI support."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class RetryState:
    """State of a single retry controller."""

    epic: int
    story: str
    step: str
    max_retries: int
    attempt: int = 0
    backoff: float = 0.0
    deadline: float = 0.0  # monotonic time when auto-retry fires
    failed: bool = False
    skipped: bool = False

    @property
    def retry_in(self) -> float:
        """Seconds until the next auto-retry (0 if not waiting)."""
        if self.deadline <= 0:
            return 0.0
        remaining = self.deadline - time.monotonic()
        return max(0.0, remaining)

    @property
    def exhausted(self) -> bool:
        return self.attempt >= self.max_retries


# Registry key type
_Key = tuple[int, str, str]  # (epic, story, step)


class RetryController:
    """Manages retry state for a single step execution.

    The controller is created on step failure and registered in the
    shared ``RetryRegistry``.  It supports:

    - Automatic exponential back-off retries.
    - Manual "retry now" that cancels the back-off timer.
    - Manual "skip" that marks the step as failed immediately.

    Thread-safe — workers and the web UI can interact concurrently.
    """

    def __init__(
        self,
        epic: int,
        story: str,
        step: str,
        max_retries: int = 1,
        initial_backoff: float = 10.0,
        backoff_factor: float = 2.0,
        max_backoff: float = 120.0,
    ) -> None:
        self.state = RetryState(
            epic=epic, story=story, step=step, max_retries=max_retries,
        )
        self._initial_backoff = initial_backoff
        self._backoff_factor = backoff_factor
        self._max_backoff = max_backoff
        self._event = threading.Event()  # set to wake from backoff sleep
        self._lock = threading.Lock()

    @property
    def key(self) -> _Key:
        return (self.state.epic, self.state.story, self.state.step)

    def enter_backoff(self) -> float:
        """Enter back-off state after a failure. Returns the backoff duration."""
        with self._lock:
            self.state.attempt += 1
            backoff = min(
                self._initial_backoff * (self._backoff_factor ** (self.state.attempt - 1)),
                self._max_backoff,
            )
            self.state.backoff = backoff
            self.state.deadline = time.monotonic() + backoff
            self._event.clear()
        return backoff

    def wait_backoff(self) -> str:
        """Block until the backoff timer expires or is interrupted.

        Returns:
            "retry" — timer expired or retry_now() was called.
            "skip"  — skip() was called.
            "exhausted" — max retries reached.
        """
        with self._lock:
            if self.state.skipped:
                return "skip"
            if self.state.exhausted:
                self.state.failed = True
                return "exhausted"
            remaining = self.state.retry_in

        if remaining > 0:
            # Wait for the timer or an external signal
            self._event.wait(timeout=remaining)

        with self._lock:
            if self.state.skipped:
                return "skip"
            self.state.deadline = 0
            self.state.backoff = 0
            return "retry"

    def retry_now(self) -> None:
        """Cancel the back-off timer and retry immediately."""
        with self._lock:
            self.state.deadline = 0
            self.state.backoff = 0
        self._event.set()

    def skip(self) -> None:
        """Mark the step as skipped/failed — stop retrying."""
        with self._lock:
            self.state.skipped = True
            self.state.failed = True
        self._event.set()

    def to_dict(self) -> dict:
        """Serializable snapshot for the API."""
        with self._lock:
            return {
                "epic": self.state.epic,
                "story": self.state.story,
                "step": self.state.step,
                "attempt": self.state.attempt,
                "max_retries": self.state.max_retries,
                "retry_in": round(self.state.retry_in, 1),
                "failed": self.state.failed,
                "skipped": self.state.skipped,
                "exhausted": self.state.exhausted,
            }


class RetryRegistry:
    """Shared registry of active RetryControllers.

    Keyed by ``(epic_num, story_key, step_name)``.  Only one controller
    per step can exist at a time.
    """

    def __init__(self) -> None:
        self._controllers: dict[_Key, RetryController] = {}
        self._lock = threading.Lock()

    def register(self, ctrl: RetryController) -> None:
        """Register a controller. Replaces any existing one for the same key."""
        with self._lock:
            self._controllers[ctrl.key] = ctrl

    def unregister(self, key: _Key) -> RetryController | None:
        """Remove and return a controller, or None."""
        with self._lock:
            return self._controllers.pop(key, None)

    def get(self, key: _Key) -> RetryController | None:
        """Look up a controller by key."""
        with self._lock:
            return self._controllers.get(key)

    def get_all(self) -> list[RetryController]:
        """Return a snapshot of all active controllers."""
        with self._lock:
            return list(self._controllers.values())

    def retry_now(self, epic: int, story: str, step: str) -> bool:
        """Trigger immediate retry for a step. Returns True if found."""
        ctrl = self.get((epic, story, step))
        if ctrl is not None:
            ctrl.retry_now()
            return True
        return False

    def skip(self, epic: int, story: str, step: str) -> bool:
        """Skip a failing step. Returns True if found."""
        ctrl = self.get((epic, story, step))
        if ctrl is not None:
            ctrl.skip()
            return True
        return False

    def skip_all(self) -> None:
        """Skip all active retry controllers — used on abort to wake sleeping loops."""
        with self._lock:
            ctrls = list(self._controllers.values())
        for ctrl in ctrls:
            ctrl.skip()

    def to_dict(self) -> list[dict]:
        """Serializable snapshot of all active retries."""
        return [c.to_dict() for c in self.get_all()]
