"""EventBus — tagged event channel decoupling pipeline execution from output."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# Event kind constants
STEP_START = "step_start"
STEP_DONE = "step_done"
STEP_FAILED = "step_failed"
STEP_SKIPPED = "step_skipped"
STORY_START = "story_start"
STORY_DONE = "story_done"
EPIC_START = "epic_start"
EPIC_DONE = "epic_done"
STATUS_CHANGE = "status_change"
STEP_RETRYING = "step_retrying"
LOG_LINE = "log_line"
LOG_MESSAGE = "log_message"


@dataclass
class PipelineEvent:
    """A single event produced by pipeline execution."""

    epic: int
    story: str | None
    step: str | None
    kind: str
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """Thread-safe event channel.

    Workers emit events via ``emit()``.  Any thread may call ``drain()``
    to dispatch pending events to all registered subscribers — subscriber
    callbacks must themselves be thread-safe (e.g. use a locked console).

    ``subscribe()`` must be called before workers start (during setup).
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[PipelineEvent] = queue.Queue()
        self._subscribers: list[Callable[[PipelineEvent], None]] = []
        self._lock = threading.Lock()

    def emit(self, event: PipelineEvent) -> None:
        """Enqueue an event (non-blocking, thread-safe)."""
        self._queue.put_nowait(event)

    def subscribe(self, callback: Callable[[PipelineEvent], None]) -> None:
        """Register a consumer callback. Must be called before drain() starts."""
        with self._lock:
            self._subscribers.append(callback)

    def has_subscribers(self) -> bool:
        """Check if any subscribers are registered."""
        with self._lock:
            return len(self._subscribers) > 0

    def drain(self) -> int:
        """Process all pending events, dispatching to subscribers.

        Returns the number of events processed.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        count = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            for callback in subscribers:
                callback(event)
            count += 1
        return count

    @property
    def pending(self) -> int:
        """Approximate number of pending events."""
        return self._queue.qsize()
