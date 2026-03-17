"""Tests for logging.py — LogBroker, RingBuffer, FileSink."""

from __future__ import annotations

import threading
from pathlib import Path

from bmad_automate.logging import FileSink, LogBroker, LogEntry, RingBuffer


class TestRingBuffer:
    def test_append_and_read(self):
        buf = RingBuffer(capacity=10)
        e = LogEntry(epic=1, story=None, step=None, level="info", line="hello")
        cursor = buf.append(e)
        assert cursor == 0

        entries, new_cursor, gap = buf.read_from(0)
        assert len(entries) == 1
        assert entries[0].line == "hello"
        assert new_cursor == 1
        assert gap is False

    def test_read_from_current_returns_empty(self):
        buf = RingBuffer(capacity=10)
        buf.append(LogEntry(epic=1, story=None, step=None, level="info", line="x"))
        entries, cursor, gap = buf.read_from(1)
        assert entries == []
        assert cursor == 1
        assert gap is False

    def test_wrapping_causes_gap(self):
        buf = RingBuffer(capacity=3)
        for i in range(5):
            buf.append(
                LogEntry(epic=1, story=None, step=None, level="info", line=f"msg-{i}")
            )

        # Reading from 0 should detect a gap (only 3 entries available)
        entries, cursor, gap = buf.read_from(0)
        assert gap is True
        assert len(entries) == 3
        assert cursor == 5
        # Should have entries 2, 3, 4
        assert entries[0].line == "msg-2"

    def test_no_gap_within_capacity(self):
        buf = RingBuffer(capacity=10)
        for i in range(5):
            buf.append(
                LogEntry(epic=1, story=None, step=None, level="info", line=f"msg-{i}")
            )
        entries, cursor, gap = buf.read_from(0)
        assert gap is False
        assert len(entries) == 5

    def test_cursor_property(self):
        buf = RingBuffer(capacity=10)
        assert buf.cursor == 0
        buf.append(LogEntry(epic=1, story=None, step=None, level="info", line="x"))
        assert buf.cursor == 1


class TestFileSink:
    def test_writes_timestamped_format(self, tmp_path: Path):
        p = tmp_path / "test.log"
        sink = FileSink(p)
        entry = LogEntry(
            epic=1, story="1-1-foo", step="dev",
            level="info", line="test message",
        )
        sink.write(entry)
        content = p.read_text(encoding="utf-8")
        assert "test message" in content
        assert content.startswith("[")
        assert "] test message\n" in content

    def test_appends_multiple(self, tmp_path: Path):
        p = tmp_path / "test.log"
        sink = FileSink(p)
        for i in range(3):
            sink.write(
                LogEntry(epic=1, story=None, step=None, level="info", line=f"msg-{i}")
            )
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3


class TestLogBroker:
    def test_write_to_both_sinks(self, tmp_path: Path):
        broker = LogBroker(log_file=tmp_path / "test.log", buffer_size=100)
        broker.log("hello", epic=1, story="1-1-foo")

        # Ring buffer got it
        entries, _, _ = broker.ring_buffer.read_from(0)
        assert len(entries) == 1
        assert entries[0].line == "hello"

        # File got it
        content = (tmp_path / "test.log").read_text(encoding="utf-8")
        assert "hello" in content

    def test_no_file_sink(self):
        broker = LogBroker(log_file=None, buffer_size=100)
        broker.log("test")
        entries, _, _ = broker.ring_buffer.read_from(0)
        assert len(entries) == 1

    def test_thread_safe_writes(self, tmp_path: Path):
        broker = LogBroker(log_file=tmp_path / "test.log", buffer_size=1000)
        errors = []

        def writer(thread_id: int):
            try:
                for i in range(50):
                    broker.log(f"thread-{thread_id}-msg-{i}", epic=thread_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        entries, cursor, _ = broker.ring_buffer.read_from(0)
        assert len(entries) == 250  # 5 threads * 50 messages
