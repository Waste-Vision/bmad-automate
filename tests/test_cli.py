"""Tests for the CLI entry point — argument parsing, validation, and dry-run paths."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bmad_automate.cli import _parse_only, app, signal_handler
from bmad_automate.control import RunControl, set_active_control
from bmad_automate.stories import invalidate_cache

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Set up a minimal BMAD project directory."""
    ss = tmp_path / "sprint-status.yaml"
    ss.write_text(
        textwrap.dedent("""\
            development_status:
              1-1-setup: ready-for-dev
              1-2-auth: backlog
              2-1-data: ready-for-dev
              2-2-api: backlog
        """),
        encoding="utf-8",
    )
    bmad = tmp_path / "_bmad"
    bmad.mkdir()
    story_dir = tmp_path / "stories"
    story_dir.mkdir()
    return tmp_path


def _common_args(project_dir: Path) -> list[str]:
    """Return common CLI args pointing at the temp project."""
    return [
        "--sprint-status", str(project_dir / "sprint-status.yaml"),
        "--story-dir", str(project_dir / "stories"),
        "--log-file", str(project_dir / "test.log"),
        "--bmad-dir", str(project_dir / "_bmad"),
    ]


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_exits_zero(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", *_common_args(project_dir)]
        )
        assert result.exit_code == 0

    def test_dry_run_shows_stories(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", *_common_args(project_dir)]
        )
        assert "1-1-setup" in result.output
        assert "1-2-auth" in result.output

    def test_dry_run_shows_step_names(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", *_common_args(project_dir)]
        )
        assert "create-story" in result.output
        assert "DRY-RUN" in result.output

    def test_dry_run_with_limit(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", "--limit", "1", *_common_args(project_dir)]
        )
        assert result.exit_code == 0
        assert "1-1-setup" in result.output
        # With limit=1, only the first story should appear in the steps output.
        # Count occurrences of DRY-RUN lines to verify only 1 story processed.
        dry_run_lines = [
            ln for ln in result.output.splitlines() if "DRY-RUN" in ln
        ]
        # Each story produces multiple DRY-RUN lines (one per step).
        # With 2 stories we'd get ~10 lines; with 1 story we get ~5.
        assert len(dry_run_lines) <= 6

    def test_dry_run_with_start_from(self, project_dir: Path):
        result = runner.invoke(
            app, [
                "--dry-run", "--start-from", "1-2-auth",
                *_common_args(project_dir),
            ],
        )
        assert result.exit_code == 0
        # 1-1-setup should NOT appear in "Stories to process" section
        # but 1-2-auth should appear
        assert "1-2-auth" in result.output
        # Count story lines in "Stories to process" — 1-1-setup should be absent
        lines_with_setup = [
            ln for ln in result.output.splitlines()
            if "1-1-setup" in ln and "DRY-RUN" in ln
        ]
        assert len(lines_with_setup) == 0


# ---------------------------------------------------------------------------
# Epic filter
# ---------------------------------------------------------------------------


class TestEpicFilter:
    def test_epic_filter_dry_run(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", "--epic", "2", *_common_args(project_dir)]
        )
        assert result.exit_code == 0
        assert "2-1-data" in result.output
        assert "1-1-setup" not in result.output

    def test_multi_epic_filter(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", "--epic", "1,2", *_common_args(project_dir)]
        )
        assert result.exit_code == 0
        assert "1-1-setup" in result.output
        assert "2-1-data" in result.output

    def test_epic_no_stories(self, project_dir: Path):
        result = runner.invoke(
            app, ["--dry-run", "--epic", "99", *_common_args(project_dir)]
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --only flag
# ---------------------------------------------------------------------------


class TestOnlyFlag:
    def test_only_flag(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--only", "review,commit", *_common_args(project_dir)],
        )
        assert result.exit_code == 0

    def test_only_invalid_step(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--only", "bogus", *_common_args(project_dir)],
        )
        assert result.exit_code != 0

    def test_only_conflicts_with_skip(self, project_dir: Path):
        result = runner.invoke(
            app,
            [
                "--dry-run", "--only", "review",
                "--skip-create",
                *_common_args(project_dir),
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# _parse_only unit tests
# ---------------------------------------------------------------------------


class TestParseOnly:
    def test_single_step(self):
        flags = _parse_only("review")
        assert flags["skip_review"] is False
        assert flags["skip_create"] is True
        assert flags["skip_dev"] is True
        assert flags["skip_commit"] is True
        assert flags["skip_pull"] is True

    def test_multiple_steps(self):
        flags = _parse_only("create,dev")
        assert flags["skip_create"] is False
        assert flags["skip_dev"] is False
        assert flags["skip_review"] is True

    def test_all_steps(self):
        flags = _parse_only("create,dev,review,commit,pull")
        assert all(v is False for v in flags.values())

    def test_whitespace_handling(self):
        flags = _parse_only(" review , commit ")
        assert flags["skip_review"] is False
        assert flags["skip_commit"] is False

    def test_unknown_step_raises(self):
        import typer
        with pytest.raises(typer.Exit):
            _parse_only("bogus")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_ai_provider(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--ai-provider", "openai", *_common_args(project_dir)],
        )
        assert result.exit_code != 0

    def test_missing_bmad_dir(self, project_dir: Path):
        result = runner.invoke(
            app,
            [
                "--dry-run",
                "--sprint-status", str(project_dir / "sprint-status.yaml"),
                "--bmad-dir", str(project_dir / "nonexistent"),
            ],
        )
        assert result.exit_code != 0

    def test_no_actionable_stories(self, tmp_path: Path):
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(
            textwrap.dedent("""\
                development_status:
                  1-1-setup: done
            """),
            encoding="utf-8",
        )
        bmad = tmp_path / "_bmad"
        bmad.mkdir()

        result = runner.invoke(
            app,
            [
                "--dry-run",
                "--sprint-status", str(ss),
                "--bmad-dir", str(bmad),
            ],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Skip flags — verify the flags actually affect dry-run output
# ---------------------------------------------------------------------------


class TestSkipFlags:
    def test_skip_create_omits_create_step(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--skip-create", *_common_args(project_dir)],
        )
        assert result.exit_code == 0
        # create-story should be skipped, not appear in DRY-RUN step lines
        dry_run_lines = [
            ln for ln in result.output.splitlines() if "DRY-RUN" in ln
        ]
        create_lines = [ln for ln in dry_run_lines if "create-story" in ln]
        assert len(create_lines) == 0
        # But other steps should still appear
        dev_lines = [ln for ln in dry_run_lines if "dev-story" in ln]
        assert len(dev_lines) > 0

    def test_skip_dev_omits_dev_step(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--skip-dev", *_common_args(project_dir)],
        )
        assert result.exit_code == 0
        dry_run_lines = [
            ln for ln in result.output.splitlines() if "DRY-RUN" in ln
        ]
        dev_lines = [ln for ln in dry_run_lines if "dev-story" in ln]
        assert len(dev_lines) == 0

    def test_skip_all_steps(self, project_dir: Path):
        result = runner.invoke(
            app,
            [
                "--dry-run",
                "--skip-create", "--skip-dev", "--skip-review",
                "--skip-commit", "--skip-pull",
                *_common_args(project_dir),
            ],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# After-epic flags
# ---------------------------------------------------------------------------


class TestAfterEpicFlags:
    def test_skip_retro(self, project_dir: Path):
        result = runner.invoke(
            app,
            ["--dry-run", "--skip-retro", *_common_args(project_dir)],
        )
        assert result.exit_code == 0

    def test_after_epic_flag(self, project_dir: Path):
        ss = project_dir / "sprint-status.yaml"
        ss.write_text(
            textwrap.dedent("""\
                development_status:
                  1-1-setup: done
                  1-2-auth: done
                  2-1-data: backlog
            """),
            encoding="utf-8",
        )
        invalidate_cache()

        result = runner.invoke(
            app,
            ["--dry-run", "--after-epic", "1", *_common_args(project_dir)],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Specific stories
# ---------------------------------------------------------------------------


class TestSpecificStories:
    def test_specific_story_key(self, project_dir: Path):
        result = runner.invoke(
            app,
            [
                "--dry-run",
                *_common_args(project_dir),
                "1-1-setup",
            ],
        )
        assert result.exit_code == 0
        assert "1-1-setup" in result.output

    def test_nonexistent_story_warns(self, project_dir: Path):
        result = runner.invoke(
            app,
            [
                "--dry-run",
                *_common_args(project_dir),
                "99-1-nope",
            ],
        )
        assert result.exit_code == 0
        # Should show a warning about the missing story
        assert "not found" in result.output.lower() or "warning" in result.output.lower() or "no" in result.output.lower()


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------


class TestSignalHandler:
    def test_signal_handler_sets_abort_via_control(self):
        ctrl = RunControl()
        set_active_control(ctrl)
        try:
            assert ctrl.global_abort is False
            signal_handler(2, None)
            assert ctrl.global_abort is True
        finally:
            set_active_control(None)

    def test_signal_handler_fallback_to_context(self):
        """When no RunControl is active, falls back to context."""
        set_active_control(None)

        from bmad_automate.context import RunContext, set_active_context
        from bmad_automate.models import Config

        cfg = Config(quiet=True)
        ctx = RunContext(config=cfg)
        set_active_context(ctx)
        try:
            # Verify precondition: no active control
            from bmad_automate.control import get_active_control
            assert get_active_control() is None

            signal_handler(2, None)
            assert ctx.interrupted is True
        finally:
            set_active_context(None)
