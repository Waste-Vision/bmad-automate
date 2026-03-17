"""Tests for web/lock.py — ServerLock."""

from __future__ import annotations

import os
from pathlib import Path

from bmad_automate.web.lock import ServerLock


class TestServerLock:
    def test_acquire_and_release(self, tmp_path: Path):
        lock = ServerLock(tmp_path)
        assert lock.acquire(8080) is True
        assert lock.lock_path.exists()
        lock.release()
        assert not lock.lock_path.exists()

    def test_read_lock_info(self, tmp_path: Path):
        lock = ServerLock(tmp_path)
        lock.acquire(9090)
        info = lock.read()
        assert info is not None
        assert info.pid == os.getpid()
        assert info.port == 9090
        lock.release()

    def test_prevents_double_acquire(self, tmp_path: Path):
        lock1 = ServerLock(tmp_path)
        lock1.acquire(8080)

        lock2 = ServerLock(tmp_path)
        # Same process, so PID is alive — should fail
        assert lock2.acquire(9090) is False

        lock1.release()

    def test_stale_lock_cleaned_up(self, tmp_path: Path):
        lock = ServerLock(tmp_path)
        # Write a lock with a dead PID
        lock._lock_path.write_text(
            '{"pid": 999999999, "port": 8080}', encoding="utf-8"
        )

        info = lock.is_server_running()
        assert info is None
        # Stale file should be cleaned up
        assert not lock._lock_path.exists()

    def test_read_missing_lock(self, tmp_path: Path):
        lock = ServerLock(tmp_path)
        assert lock.read() is None

    def test_is_server_running_no_lock(self, tmp_path: Path):
        lock = ServerLock(tmp_path)
        assert lock.is_server_running() is None
