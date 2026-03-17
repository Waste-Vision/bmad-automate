"""Tests for worktree.py — WorktreeManager."""

from __future__ import annotations

from pathlib import Path

from bmad_automate.worktree import WorktreeManager


class TestWorktreeManager:
    def test_get_worktree_path(self, tmp_path: Path):
        mgr = WorktreeManager(project_root=tmp_path)
        p = mgr.get_worktree_path(3)
        assert p == tmp_path / ".bmad-worktrees" / "epic-3"

    def test_list_existing_empty(self, tmp_path: Path):
        mgr = WorktreeManager(project_root=tmp_path)
        assert mgr.list_existing() == []

    def test_list_existing_finds_epic_dirs(self, tmp_path: Path):
        base = tmp_path / ".bmad-worktrees"
        base.mkdir()
        (base / "epic-1").mkdir()
        (base / "epic-5").mkdir()
        (base / "not-an-epic").mkdir()

        mgr = WorktreeManager(project_root=tmp_path)
        result = mgr.list_existing()
        assert len(result) == 2
        assert result[0][0] == 1
        assert result[1][0] == 5

    def test_save_and_load_run_state(self, tmp_path: Path):
        mgr = WorktreeManager(project_root=tmp_path)
        state = {"epics": {"3": {"last_story": "3-2-feature"}}}
        mgr.save_run_state(state)
        loaded = mgr.load_run_state()
        assert loaded == state

    def test_load_run_state_missing(self, tmp_path: Path):
        mgr = WorktreeManager(project_root=tmp_path)
        assert mgr.load_run_state() is None

    def test_clear_run_state(self, tmp_path: Path):
        mgr = WorktreeManager(project_root=tmp_path)
        mgr.save_run_state({"test": True})
        mgr.clear_run_state()
        assert mgr.load_run_state() is None
