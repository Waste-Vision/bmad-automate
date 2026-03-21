"""MergeQueue — serial merge of completed epic worktrees back to main."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from bmad_automate.ui import console

if TYPE_CHECKING:
    from bmad_automate.context import RunContext
    from bmad_automate.models import Config


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

    def __init__(
        self,
        project_root: Path | None = None,
        config: "Config | None" = None,
        ctx: "RunContext | None" = None,
    ) -> None:
        self._root = project_root or Path.cwd()
        self._config = config
        self._ctx = ctx
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

    def _commit_local_changes(self) -> None:
        """Commit any uncommitted changes in the main repo before merging.

        Worktrees writing sprint-status.yaml or planning artifacts may leave
        the main working tree dirty, which causes `git merge` to abort with
        "your local changes would be overwritten".
        """
        # Check for any tracked modifications or untracked files that would
        # conflict (git merge only complains about files it needs to touch).
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        if not (status.stdout or "").strip():
            return  # nothing to commit

        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore: auto-commit local changes before epic merge",
             "--allow-empty"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )

    def _do_merge(self, request: MergeRequest) -> MergeResult:
        """Attempt to merge a worktree branch into the current branch."""
        epic_num = request.epic_num
        branch = request.branch_name

        console.print(
            f"\n  [cyan]Merging epic {epic_num} "
            f"({branch})...[/cyan]"
        )

        try:
            # Commit any local changes in the main repo first — otherwise git
            # will refuse to merge if tracked files would be overwritten.
            self._commit_local_changes()

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
                return self._resolve_conflicts_with_ai(request)

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

    def _unmerged_files(self) -> list[tuple[str, str]]:
        """Return list of (status, path) for all unmerged files.

        Status is the two-character git status code, e.g. 'UU', 'AA', 'AU'.
        """
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        unmerged = []
        for line in (result.stdout or "").splitlines():
            if len(line) >= 3:
                xy = line[:2]
                path = line[3:]
                if "U" in xy or xy == "AA" or xy == "DD":
                    unmerged.append((xy, path))
        return unmerged

    def _pre_resolve_trivial_conflicts(self) -> None:
        """Handle conflict types the AI cannot fix via text editing.

        - Both-added (AA): take theirs — the incoming epic has the authoritative copy.
        - Submodules (SM): take theirs — submodule pointer advances with the epic.
        """
        unmerged = self._unmerged_files()
        for xy, path in unmerged:
            is_submodule = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--stage", path],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            ).stdout.startswith("16")  # mode 160000 = submodule

            if xy == "AA" or is_submodule:
                subprocess.run(
                    ["git", "checkout", "--theirs", path],
                    cwd=str(self._root),
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "add", path],
                    cwd=str(self._root),
                    capture_output=True,
                    text=True,
                )

    def _resolve_conflicts_with_ai(self, request: MergeRequest) -> MergeResult:
        """Invoke the AI to resolve merge conflicts, then complete the merge commit."""
        epic_num = request.epic_num
        branch = request.branch_name

        if self._config is None or self._ctx is None:
            # No AI context available — abort and fail
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            )
            error = f"Merge conflicts in epic {epic_num} (no AI context to resolve)"
            console.print(f"  [red]XX[/red] Epic {epic_num} merge failed: {error}")
            return MergeResult(epic_num=epic_num, success=False, error=error)

        console.print(
            f"  [yellow]~~[/yellow] Merge conflicts in epic {epic_num} "
            f"— invoking AI to resolve..."
        )

        # Pre-resolve AA (both-added) and submodule conflicts which the AI
        # cannot fix via text editing — take theirs in both cases.
        self._pre_resolve_trivial_conflicts()

        # Check if there are still UU conflicts left for the AI to handle.
        remaining = self._unmerged_files()
        if not remaining:
            # All conflicts were trivial — just commit.
            commit_result = subprocess.run(
                ["git", "commit", "--no-edit"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            )
            if commit_result.returncode == 0:
                console.print(
                    f"  [green]OK[/green] Epic {epic_num} merged (all conflicts trivially resolved)"
                )
                return MergeResult(epic_num=epic_num, success=True)

        from bmad_automate.git import run_step
        from bmad_automate.models import StepStatus

        import copy
        resolve_config = copy.copy(self._config)
        resolve_config.project_root = self._root
        resolve_config.in_worktree = False

        ai = resolve_config.ai_command
        remaining_paths = ", ".join(p for _, p in remaining)
        prompt = (
            f"There are git merge conflicts in the repository at {self._root} "
            f"after merging the epic branch `{branch}` into the main branch. "
            f"The conflicted files are: {remaining_paths}. "
            "For each file: read it, resolve every conflict marker "
            "(<<<<<<, =======, >>>>>>>) preferring the incoming changes from "
            f"`{branch}` when uncertain, then run 'git add <file>'. "
            "Once ALL unmerged files are staged (confirm with "
            "'git diff --name-only --diff-filter=U' returning empty), "
            "run 'git commit --no-edit' to complete the merge. "
            "Do NOT push. Do not ask clarifying questions."
        )
        command = f'{ai} "{prompt}"'
        step_name = f"merge-conflict-resolve-epic-{epic_num}"

        run_step(step_name, command, f"epic-{epic_num}", resolve_config, self._ctx)

        # Verify the merge is actually clean regardless of Claude's exit code.
        still_unmerged = self._unmerged_files()
        if not still_unmerged:
            # Check if the merge commit was made; if not, commit now.
            merge_head = (self._root / ".git" / "MERGE_HEAD")
            if merge_head.exists():
                subprocess.run(
                    ["git", "commit", "--no-edit"],
                    cwd=str(self._root),
                    capture_output=True,
                    text=True,
                )
            console.print(
                f"  [green]OK[/green] Epic {epic_num} merge conflicts resolved by AI"
            )
            return MergeResult(epic_num=epic_num, success=True)

        # AI left files unresolved — force-take theirs and commit.
        console.print(
            f"  [yellow]~~[/yellow] Epic {epic_num}: AI left {len(still_unmerged)} "
            f"file(s) unresolved — taking incoming for remainder"
        )
        for _, path in still_unmerged:
            subprocess.run(
                ["git", "checkout", "--theirs", path],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "add", path],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            )
        commit_result = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )
        if commit_result.returncode == 0:
            console.print(
                f"  [green]OK[/green] Epic {epic_num} merge completed (fallback resolution)"
            )
            return MergeResult(epic_num=epic_num, success=True)

        error = (commit_result.stderr or "").strip() or "merge commit failed"
        console.print(f"  [red]XX[/red] Epic {epic_num} merge failed: {error}")
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
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
