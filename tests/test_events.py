"""Tests for events.py — EventBus and PipelineEvent."""

from __future__ import annotations

from bmad_automate.events import (
    STEP_DONE,
    STEP_START,
    EventBus,
    PipelineEvent,
)


class TestPipelineEvent:
    def test_defaults(self):
        e = PipelineEvent(epic=1, story="1-1-foo", step="dev-story", kind=STEP_START)
        assert e.payload == {}
        assert e.timestamp > 0

    def test_with_payload(self):
        e = PipelineEvent(
            epic=1, story="1-1-foo", step="dev-story",
            kind=STEP_DONE, payload={"duration": 42.0},
        )
        assert e.payload["duration"] == 42.0


class TestEventBus:
    def test_emit_and_drain(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e))

        bus.emit(PipelineEvent(epic=1, story="1-1-foo", step="dev", kind=STEP_START))
        assert bus.pending >= 1

        count = bus.drain()
        assert count == 1
        assert len(received) == 1
        assert received[0].kind == STEP_START

    def test_multiple_subscribers(self):
        bus = EventBus()
        a, b = [], []
        bus.subscribe(lambda e: a.append(e))
        bus.subscribe(lambda e: b.append(e))

        bus.emit(PipelineEvent(epic=1, story=None, step=None, kind=STEP_DONE))
        bus.drain()

        assert len(a) == 1
        assert len(b) == 1

    def test_drain_empty(self):
        bus = EventBus()
        assert bus.drain() == 0

    def test_multiple_events(self):
        bus = EventBus()
        received = []
        bus.subscribe(lambda e: received.append(e.kind))

        bus.emit(PipelineEvent(epic=1, story=None, step=None, kind=STEP_START))
        bus.emit(PipelineEvent(epic=1, story=None, step=None, kind=STEP_DONE))

        count = bus.drain()
        assert count == 2
        assert received == [STEP_START, STEP_DONE]

    def test_no_subscribers_doesnt_error(self):
        bus = EventBus()
        bus.emit(PipelineEvent(epic=1, story=None, step=None, kind=STEP_START))
        assert bus.drain() == 1
