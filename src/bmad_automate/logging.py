"""LogBroker — thread-safe, multi-sink logging."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class LogEntry:
    """A single log record."""

    epic: int
    story: str | None
    step: str | None
    level: str  # "stdout", "stderr", "info", "error", "success"
    line: str
    timestamp: float = field(default_factory=time.time)
    cursor: int = 0  # assigned by RingBuffer
    event_kind: str | None = None  # pipeline event kind (step_start, step_done, etc.)


class RingBuffer:
    """Fixed-size circular buffer for SSE consumers.

    Thread-safe via a lock.  Each entry gets a monotonically increasing
    cursor so consumers can resume from a known position.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self._buffer: list[LogEntry | None] = [None] * capacity
        self._head = 0  # next write position (modular)
        self._cursor = 0  # global monotonic cursor
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def cursor(self) -> int:
        """Current global cursor (number of entries ever written)."""
        with self._lock:
            return self._cursor

    def append(self, entry: LogEntry) -> int:
        """Add an entry and return its cursor position."""
        with self._lock:
            entry.cursor = self._cursor
            self._buffer[self._head] = entry
            self._head = (self._head + 1) % self._capacity
            self._cursor += 1
            return entry.cursor

    def read_from(
        self, from_cursor: int
    ) -> tuple[list[LogEntry], int, bool]:
        """Read entries starting from *from_cursor*.

        Returns ``(entries, new_cursor, gap)`` where *gap* is True if
        some entries were lost due to buffer wrapping.
        """
        with self._lock:
            current = self._cursor
            if from_cursor >= current:
                return [], current, False

            oldest_available = max(0, current - self._capacity)
            gap = from_cursor < oldest_available
            start = max(from_cursor, oldest_available)

            entries: list[LogEntry] = []
            for c in range(start, current):
                idx = c % self._capacity
                entry = self._buffer[idx]
                if entry is not None:
                    entries.append(entry)

            return entries, current, gap


class FileSink:
    """Appends log entries to a file using the same format as log_to_file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def write(self, entry: LogEntry) -> None:
        timestamp = datetime.fromtimestamp(entry.timestamp).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {entry.line}\n")


class LogBroker:
    """Multi-sink log dispatcher.

    Sends every log entry to both a file sink and a ring buffer.
    Thread-safe — multiple workers can call ``write()`` concurrently.
    """

    def __init__(
        self,
        log_file: Path | None = None,
        buffer_size: int | None = None,
    ) -> None:
        size = buffer_size or int(
            os.environ.get("BMAD_LOG_BUFFER_SIZE", "10000")
        )
        self.ring_buffer = RingBuffer(capacity=size)
        self.file_sink = FileSink(log_file) if log_file else None

    def write(self, entry: LogEntry) -> None:
        """Write a log entry to all sinks."""
        self.ring_buffer.append(entry)
        if self.file_sink:
            self.file_sink.write(entry)

    def log(
        self,
        message: str,
        *,
        epic: int = 0,
        story: str | None = None,
        step: str | None = None,
        level: str = "info",
    ) -> None:
        """Convenience: create a LogEntry and write it."""
        self.write(LogEntry(
            epic=epic, story=story, step=step,
            level=level, line=message,
        ))
