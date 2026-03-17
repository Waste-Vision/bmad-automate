"""Tests for merge_queue.py — MergeQueue."""

from __future__ import annotations

from pathlib import Path

from bmad_automate.merge_queue import MergeQueue, MergeStatus


class TestMergeQueue:
    def test_enqueue(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        q.enqueue(3, tmp_path / "wt-3")
        assert q.pending_count == 1

    def test_enqueue_multiple(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        q.enqueue(3, tmp_path / "wt-3")
        q.enqueue(5, tmp_path / "wt-5")
        assert q.pending_count == 2

    def test_get_position(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        q.enqueue(3, tmp_path / "wt-3")
        q.enqueue(5, tmp_path / "wt-5")
        assert q.get_position(3) == 0
        assert q.get_position(5) == 1
        assert q.get_position(99) is None

    def test_abort_marks_pending_as_aborted(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        q.enqueue(3, tmp_path / "wt-3")
        q.enqueue(5, tmp_path / "wt-5")
        q.abort()

        queue = q.queue
        assert all(r.status == MergeStatus.ABORTED for r in queue)
        assert q.process_next() is None

    def test_process_next_empty(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        assert q.process_next() is None

    def test_queue_snapshot(self, tmp_path: Path):
        q = MergeQueue(project_root=tmp_path)
        q.enqueue(3, tmp_path / "wt-3")
        snapshot = q.queue
        assert len(snapshot) == 1
        assert snapshot[0].epic_num == 3



# StatusManager tests live in test_orchestrator.py (canonical location).
