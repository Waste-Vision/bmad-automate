"""Git worktree management for parallel epic processing."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


WORKTREE_DIR = ".bmad-worktrees"
RUN_STATE_FILE = "run-state.json"


class WorktreeManager:
    """Manages git worktrees for parallel epic execution.

    Each epic gets its own worktree branching from the current HEAD,
    allowing independent story processing with isolated file systems.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self._root = project_root or Path.cwd()
        self._worktree_base = self._root / WORKTREE_DIR

    @property
    def worktree_base(self) -> Path:
        return self._worktree_base

    def create(self, epic_num: int) -> Path:
        """Create a worktree for an epic, returning its path.

        Creates: .bmad-worktrees/epic-<N> on branch auto/epic-<N>
        """
        wt_path = self._worktree_base / f"epic-{epic_num}"
        branch_name = f"auto/epic-{epic_num}"

        if wt_path.exists():
            # Validate existing worktree: reuse if on the correct branch
            # (even if dirty — uncommitted changes are in-progress work worth keeping)
            try:
                branch_check = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(wt_path), capture_output=True, text=True,
                )
                if branch_check.stdout.strip() == branch_name:
                    return wt_path

                # Wrong branch — stale worktree, remove and recreate
                self.remove(epic_num)
            except Exception:
                self.remove(epic_num)

        self._worktree_base.mkdir(parents=True, exist_ok=True)

        # Prune stale worktree registrations (directories deleted but git still
        # tracks them) so that a subsequent `git worktree add` doesn't fail
        # with exit code 128.
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )

        # If the branch already exists (orphaned from a previous run where the
        # worktree directory was deleted but the branch wasn't cleaned up),
        # use --force and check-out the existing branch instead of -b (create).
        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        ).returncode == 0

        if branch_exists:
            cmd = ["git", "worktree", "add", str(wt_path), branch_name]
        else:
            cmd = ["git", "worktree", "add", str(wt_path), "-b", branch_name]

        subprocess.run(
            cmd,
            cwd=str(self._root),
            capture_output=True,
            text=True,
            check=True,
        )

        return wt_path

    def remove(self, epic_num: int) -> None:
        """Remove a worktree and its branch."""
        wt_path = self._worktree_base / f"epic-{epic_num}"
        branch_name = f"auto/epic-{epic_num}"

        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(wt_path), "--force"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
            )

        # Prune any stale registrations (handles the case where the directory
        # was deleted externally but git still has the worktree registered).
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )

        # Clean up the branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )

    def list_existing(self) -> list[tuple[int, Path]]:
        """Scan for existing worktrees matching the epic-<N> pattern."""
        if not self._worktree_base.exists():
            return []

        pattern = re.compile(r"^epic-(\d+)$")
        results: list[tuple[int, Path]] = []

        for entry in self._worktree_base.iterdir():
            if entry.is_dir():
                m = pattern.match(entry.name)
                if m:
                    results.append((int(m.group(1)), entry))

        return sorted(results, key=lambda x: x[0])

    def cleanup_all(self) -> None:
        """Remove all epic worktrees."""
        for epic_num, _ in self.list_existing():
            self.remove(epic_num)

        # Remove the base directory if empty
        if self._worktree_base.exists():
            try:
                self._worktree_base.rmdir()
            except OSError:
                pass  # not empty, leave it

    def get_worktree_path(self, epic_num: int) -> Path:
        """Return the path for an epic's worktree (may not exist yet)."""
        return self._worktree_base / f"epic-{epic_num}"

    # ------------------------------------------------------------------
    # Run state persistence (for resumability)
    # ------------------------------------------------------------------

    def save_run_state(self, state: dict) -> None:
        """Write run state to .bmad-worktrees/run-state.json."""
        self._worktree_base.mkdir(parents=True, exist_ok=True)
        state_file = self._worktree_base / RUN_STATE_FILE
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def load_run_state(self) -> dict | None:
        """Load run state, or return None if no state file exists."""
        state_file = self._worktree_base / RUN_STATE_FILE
        if not state_file.exists():
            return None
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)

    def clear_run_state(self) -> None:
        """Remove the run state file."""
        state_file = self._worktree_base / RUN_STATE_FILE
        if state_file.exists():
            state_file.unlink()
