"""Process coordination — file lock for server/CLI mutual exclusion."""

from __future__ import annotations

import atexit
import json
import os
import signal
import sys
from pathlib import Path


LOCK_FILE = ".bmad-automate.lock"


class LockInfo:
    """Information stored in the lock file."""

    def __init__(self, pid: int, port: int) -> None:
        self.pid = pid
        self.port = port

    def to_dict(self) -> dict:
        return {"pid": self.pid, "port": self.port}

    @classmethod
    def from_dict(cls, data: dict) -> LockInfo:
        return cls(pid=data["pid"], port=data["port"])


def _pid_is_alive(pid: int) -> bool:
    """Check if a process with the given PID is still alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class ServerLock:
    """Exclusive file lock for the web server.

    The lock file contains the PID and port of the running server.
    The CLI checks this lock on startup to decide whether to delegate
    to the server or run directly.
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self._dir = project_dir or Path.cwd()
        self._lock_path = self._dir / LOCK_FILE
        self._acquired = False

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def acquire(self, port: int) -> bool:
        """Try to acquire the lock. Returns False if another server is running."""
        existing = self.read()
        if existing and _pid_is_alive(existing.pid):
            return False  # another server is alive

        # Write our lock
        info = LockInfo(pid=os.getpid(), port=port)
        self._lock_path.write_text(
            json.dumps(info.to_dict()), encoding="utf-8"
        )
        self._acquired = True

        # Register cleanup
        atexit.register(self.release)
        return True

    def release(self) -> None:
        """Release the lock (delete the file)."""
        if self._acquired and self._lock_path.exists():
            try:
                self._lock_path.unlink()
            except OSError:
                pass
            self._acquired = False

    def read(self) -> LockInfo | None:
        """Read the lock file, returning None if it doesn't exist or is invalid."""
        if not self._lock_path.exists():
            return None
        try:
            data = json.loads(self._lock_path.read_text(encoding="utf-8"))
            return LockInfo.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def is_server_running(self) -> LockInfo | None:
        """Check if a server is currently running.

        Returns LockInfo if alive, None otherwise. Cleans up stale locks.
        """
        info = self.read()
        if info is None:
            return None

        if _pid_is_alive(info.pid):
            return info

        # Stale lock — process is dead
        try:
            self._lock_path.unlink()
        except OSError:
            pass
        return None
