"""MergeQueue — serial merge of completed epic worktrees back to main."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from bmad_automate.ui import console


class MergeStatus(Enum):
    PENDING = "pending"
    MERGING = "merging"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class MergeRequest:
    epic_num: int
    worktree_path: Path
    branch_name: str
    status: MergeStatus = MergeStatus.PENDING
    error: str = ""


@dataclass
class MergeResult:
    epic_num: int
    success: bool
    error: str = ""


class MergeQueue:
    """Processes merge requests one at a time.

    Worktrees that complete their stories enqueue a merge request.
    The orchestrator drains the queue serially: fast-forward merge,
    fallback to regular merge, then after-epic pipeline.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self._root = project_root or Path.cwd()
        self._queue: list[MergeRequest] = []
        self._lock = threading.Lock()
        self._aborted = False

    @property
    def queue(self) -> list[MergeRequest]:
        with self._lock:
            return list(self._queue)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for r in self._queue if r.status == MergeStatus.PENDING
            )

    def enqueue(self, epic_num: int, worktree_path: Path) -> None:
        """Add a merge request to the queue."""
        branch_name = f"auto/epic-{epic_num}"
        with self._lock:
            self._queue.append(
                MergeRequest(
                    epic_num=epic_num,
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                )
            )

    def abort(self) -> None:
        """Abort all pending merges."""
        with self._lock:
            self._aborted = True
            for req in self._queue:
                if req.status == MergeStatus.PENDING:
                    req.status = MergeStatus.ABORTED

    def process_next(self) -> MergeResult | None:
        """Process the next pending merge request.

        Returns None if the queue is empty or aborted.
        """
        with self._lock:
            if self._aborted:
                return None
            pending = [
                r for r in self._queue if r.status == MergeStatus.PENDING
            ]
            if not pending:
                return None
            request = pending[0]
            request.status = MergeStatus.MERGING

        # Perform the merge outside the lock
        result = self._do_merge(request)

        with self._lock:
            request.status = (
                MergeStatus.DONE if result.success else MergeStatus.FAILED
            )
            request.error = result.error

        return result

    def process_all(self) -> list[MergeResult]:
        """Process all pending merge requests in order."""
        results: list[MergeResult] = []
        while True:
            result = self.process_next()
            if result is None:
                break
            results.append(result)
        return results

    def _do_merge(self, request: MergeRequest) -> MergeResult:
        """Attempt to merge a worktree branch into the current branch."""
        epic_num = request.epic_num
        branch = request.branch_name

        console.print(
            f"\n  [cyan]Merging epic {epic_num} "
            f"({branch})...[/cyan]"
        )

        try:
            # Try fast-forward merge first
            ff_result = subprocess.run(
                ["git", "merge", "--ff-only", branch],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=120,
            )

            if ff_result.returncode == 0:
                console.print(
                    f"  [green]OK[/green] Epic {epic_num} merged (fast-forward)"
                )
                return MergeResult(epic_num=epic_num, success=True)

            # Fall back to regular merge
            merge_result = subprocess.run(
                ["git", "merge", branch, "-m",
                 f"Merge parallel epic {epic_num}"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=120,
            )

            if merge_result.returncode == 0:
                console.print(
                    f"  [green]OK[/green] Epic {epic_num} merged"
                )
                return MergeResult(epic_num=epic_num, success=True)

            # Merge failed — check for conflicts
            combined = (merge_result.stdout or "") + (merge_result.stderr or "")
            if "CONFLICT" in combined:
                # Abort the failed merge to restore clean state
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(self._root),
                    capture_output=True,
                    text=True,
                )
                error = f"Merge conflicts in epic {epic_num}"
            else:
                error = (
                    (merge_result.stderr or "").strip()
                    or f"Merge exit code: {merge_result.returncode}"
                )

            console.print(
                f"  [red]XX[/red] Epic {epic_num} merge failed: {error}"
            )
            return MergeResult(epic_num=epic_num, success=False, error=error)

        except subprocess.TimeoutExpired:
            error = "Merge timed out after 120s"
            console.print(
                f"  [red]XX[/red] Epic {epic_num}: {error}"
            )
            return MergeResult(epic_num=epic_num, success=False, error=error)

        except Exception as e:
            error = str(e)
            console.print(
                f"  [red]XX[/red] Epic {epic_num}: {error}"
            )
            return MergeResult(epic_num=epic_num, success=False, error=error)

    def get_position(self, epic_num: int) -> int | None:
        """Return the queue position for an epic (0-based), or None."""
        with self._lock:
            pending = [
                r for r in self._queue if r.status == MergeStatus.PENDING
            ]
            for i, req in enumerate(pending):
                if req.epic_num == epic_num:
                    return i
        return None
