"""Rate limiting — exponential backoff and concurrency throttling."""

from __future__ import annotations

import re
import threading
import time

# Patterns that indicate a rate limit response
RATE_LIMIT_PATTERNS = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"HTTP\s+429", re.IGNORECASE),
    re.compile(r"retry.?after", re.IGNORECASE),
    re.compile(r"throttl", re.IGNORECASE),
]


def is_rate_limited(stderr: str) -> bool:
    """Check if stderr output indicates a rate limit."""
    return any(p.search(stderr) for p in RATE_LIMIT_PATTERNS)


class RateLimiter:
    """Concurrency throttle with exponential backoff.

    Uses a counter + condition variable (not a semaphore) so that
    ``adjust_concurrency()`` can change the limit dynamically without
    affecting in-flight acquisitions.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        initial_backoff: float = 30.0,
        max_backoff: float = 300.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._active = 0
        self._cond = threading.Condition(threading.Lock())
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._backoff_factor = backoff_factor
        self._consecutive_limits: dict[int, int] = {}  # epic -> count

    @property
    def max_concurrent(self) -> int:
        with self._cond:
            return self._max_concurrent

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire a slot. Returns False if timed out."""
        with self._cond:
            deadline = time.monotonic() + timeout if timeout is not None else None
            while self._active >= self._max_concurrent:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    if not self._cond.wait(timeout=remaining):
                        return False
                else:
                    self._cond.wait()
            self._active += 1
            return True

    def release(self) -> None:
        """Release a slot."""
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()

    def record_rate_limit(self, epic_num: int) -> float:
        """Record a rate limit hit and return the backoff duration in seconds."""
        with self._cond:
            count = self._consecutive_limits.get(epic_num, 0) + 1
            self._consecutive_limits[epic_num] = count

        backoff = min(
            self._initial_backoff * (self._backoff_factor ** (count - 1)),
            self._max_backoff,
        )
        return backoff

    def record_success(self, epic_num: int) -> None:
        """Reset backoff counter after a successful step."""
        with self._cond:
            self._consecutive_limits.pop(epic_num, None)

    def get_backoff(self, epic_num: int) -> float:
        """Get the current backoff duration for an epic (0 if none)."""
        with self._cond:
            count = self._consecutive_limits.get(epic_num, 0)
        if count == 0:
            return 0.0
        return min(
            self._initial_backoff * (self._backoff_factor ** (count - 1)),
            self._max_backoff,
        )

    def should_degrade_to_sequential(self, epic_num: int) -> bool:
        """Check if too many rate limits have occurred (graceful degradation)."""
        with self._cond:
            count = self._consecutive_limits.get(epic_num, 0)
        return count >= 5

    def adjust_concurrency(self, new_max: int) -> None:
        """Dynamically adjust the concurrency limit.

        Safe to call while workers hold slots — in-flight workers are
        not affected. New limit takes effect on next acquire/release.
        """
        with self._cond:
            self._max_concurrent = max(1, new_max)
            # Wake all waiters so they re-check the new limit
            self._cond.notify_all()

    def wait_backoff(self, epic_num: int) -> float:
        """Sleep for the current backoff duration. Returns time slept."""
        backoff = self.get_backoff(epic_num)
        if backoff > 0:
            time.sleep(backoff)
        return backoff
