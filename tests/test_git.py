"""Tests for git.py — subprocess helpers, run_step, mark_story_done, run_git_pull."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bmad_automate.context import RunContext
from bmad_automate.events import (
    LOG_LINE,
    STEP_DONE,
    STEP_FAILED,
    STEP_START,
    PipelineEvent,
)
from bmad_automate.git import (
    _extract_epic_num,
    mark_story_done,
    run_after_epic_commit,
    run_git_command,
    run_git_pull,
    run_step,
)
from bmad_automate.models import Config, StepResult, StepStatus
from bmad_automate.stories import invalidate_cache


@pytest.fixture(autouse=True)
def _clear_yaml_cache():
    """Prevent YAML cache leaks between tests."""
    invalidate_cache()
    yield
    invalidate_cache()


# ---------------------------------------------------------------------------
# _extract_epic_num
# ---------------------------------------------------------------------------


class TestExtractEpicNum:
    def test_normal_key(self):
        assert _extract_epic_num("3-1-feature") == 3

    def test_single_digit(self):
        assert _extract_epic_num("1-2-setup") == 1

    def test_multi_digit(self):
        assert _extract_epic_num("12-1-feature") == 12

    def test_invalid_no_dash(self):
        assert _extract_epic_num("nodash") == 0

    def test_invalid_non_numeric(self):
        assert _extract_epic_num("abc-1-feature") == 0

    def test_empty_string(self):
        assert _extract_epic_num("") == 0

    def test_epic_key(self):
        # "epic-3" -> "epic" is not an int -> returns 0
        assert _extract_epic_num("epic-3") == 0


# ---------------------------------------------------------------------------
# run_git_command
# ---------------------------------------------------------------------------


class TestRunGitCommand:
    @patch("bmad_automate.git.subprocess.run")
    def test_returns_completed_process(self, mock_run, config):
        mock_run.return_value = subprocess.CompletedProcess(
            args="git status", returncode=0, stdout="clean", stderr=""
        )
        result = run_git_command("git status", config, "status check")
        assert result.returncode == 0
        assert result.stdout == "clean"

    @patch("bmad_automate.git.subprocess.run")
    def test_logs_stdout_and_stderr(self, mock_run, config):
        mock_run.return_value = subprocess.CompletedProcess(
            args="git pull", returncode=0, stdout="output", stderr="warning"
        )
        with patch("bmad_automate.git.log_to_file") as mock_log:
            run_git_command("git pull", config, "pull")
            assert mock_log.call_count == 2

    @patch("bmad_automate.git.subprocess.run")
    def test_no_log_when_empty_output(self, mock_run, config):
        mock_run.return_value = subprocess.CompletedProcess(
            args="git status", returncode=0, stdout="", stderr=""
        )
        with patch("bmad_automate.git.log_to_file") as mock_log:
            run_git_command("git status", config, "status")
            mock_log.assert_not_called()

    @patch("bmad_automate.git.subprocess.run")
    def test_passes_cwd(self, mock_run, config):
        mock_run.return_value = subprocess.CompletedProcess(
            args="git status", returncode=0, stdout="", stderr=""
        )
        run_git_command("git status", config, "status", cwd="/some/path")
        _, kwargs = mock_run.call_args
        assert kwargs["cwd"] == "/some/path"

    @patch("bmad_automate.git.subprocess.run")
    def test_timeout_passed(self, mock_run, config):
        mock_run.return_value = subprocess.CompletedProcess(
            args="git status", returncode=0, stdout="", stderr=""
        )
        run_git_command("git status", config, "status", timeout=60)
        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == 60


# ---------------------------------------------------------------------------
# run_step
# ---------------------------------------------------------------------------


class TestRunStep:
    def test_dry_run_returns_skipped(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_step("create-story", "echo hello", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.SKIPPED
        assert result.name == "create-story"
        assert result.duration == 0.0

    @patch("bmad_automate.git.subprocess.run")
    def test_success_returns_success(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            args="echo", returncode=0, stdout="done", stderr=""
        )
        result = run_step("dev-story", "echo done", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.SUCCESS
        assert result.name == "dev-story"
        assert result.duration > 0
        assert mock_run.call_count == 1

    @patch("bmad_automate.git.subprocess.run")
    def test_failure_returns_failed(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            args="false", returncode=1, stdout="", stderr="error msg"
        )
        result = run_step("dev-story", "false", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.FAILED
        assert "error msg" in result.error
        assert mock_run.call_count == 1  # no retries

    @patch("bmad_automate.git.subprocess.run")
    def test_timeout_returns_failed(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="long", timeout=10)
        result = run_step("dev-story", "long", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.FAILED
        assert "Timeout" in result.error

    @patch("bmad_automate.git.subprocess.run")
    def test_exception_returns_failed(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.side_effect = OSError("no such command")
        result = run_step("dev-story", "bad", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.FAILED
        assert "no such command" in result.error

    @patch("bmad_automate.git.subprocess.run")
    def test_retry_on_failure(self, mock_run, make_config):
        cfg = make_config(retries=2)
        ctx = RunContext(config=cfg)

        # Fail twice then succeed
        mock_run.side_effect = [
            subprocess.CompletedProcess("cmd", returncode=1, stdout="", stderr="err"),
            subprocess.CompletedProcess("cmd", returncode=1, stdout="", stderr="err"),
            subprocess.CompletedProcess("cmd", returncode=0, stdout="ok", stderr=""),
        ]
        result = run_step("dev-story", "cmd", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.SUCCESS
        assert mock_run.call_count == 3

    @patch("bmad_automate.git.subprocess.run")
    def test_retry_exhaustion(self, mock_run, make_config):
        cfg = make_config(retries=1)
        ctx = RunContext(config=cfg)

        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", returncode=1, stdout="", stderr="always fail"
        )
        result = run_step("dev-story", "cmd", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.FAILED
        assert mock_run.call_count == 2  # initial + 1 retry

    def test_interrupted_returns_failed(self, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        ctx.interrupted = True
        result = run_step("dev-story", "echo", "3-1-feat", cfg, ctx)
        assert result.status == StepStatus.FAILED
        assert result.error == "Interrupted"

    @patch("bmad_automate.git.subprocess.run")
    def test_emits_start_and_done_events_with_payloads(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", returncode=0, stdout="ok", stderr=""
        )
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        run_step("dev-story", "cmd", "3-1-feat", cfg, ctx)
        ctx.event_bus.drain()

        start_events = [e for e in collected if e.kind == STEP_START]
        assert len(start_events) == 1
        assert start_events[0].payload["attempt"] == 0
        assert start_events[0].payload["retries"] == 0
        assert start_events[0].step == "dev-story"
        assert start_events[0].epic == 3

        done_events = [e for e in collected if e.kind == STEP_DONE]
        assert len(done_events) == 1
        assert "duration" in done_events[0].payload
        assert done_events[0].payload["duration"] > 0

    @patch("bmad_automate.git.subprocess.run")
    def test_emits_failed_event_with_payload(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", returncode=1, stdout="", stderr="broke"
        )
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        run_step("dev-story", "cmd", "3-1-feat", cfg, ctx)
        ctx.event_bus.drain()

        failed_events = [e for e in collected if e.kind == STEP_FAILED]
        assert len(failed_events) == 1
        assert "error" in failed_events[0].payload
        assert "broke" in failed_events[0].payload["error"]
        assert "duration" in failed_events[0].payload

    @patch("bmad_automate.git.subprocess.run")
    def test_emits_log_line_events(self, mock_run, make_config):
        cfg = make_config(retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", returncode=0, stdout="some output", stderr="some warning"
        )
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        run_step("dev-story", "cmd", "3-1-feat", cfg, ctx)
        ctx.event_bus.drain()

        log_events = [e for e in collected if e.kind == LOG_LINE]
        assert len(log_events) == 2  # stdout + stderr
        streams = {e.payload["stream"] for e in log_events}
        assert "STDOUT" in streams
        assert "STDERR" in streams

    @patch("bmad_automate.git.subprocess.run")
    def test_github_stderr_filtering_strips_noise(self, mock_run, make_config):
        cfg = make_config(ai_provider="github", retries=0)
        ctx = RunContext(config=cfg)
        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", returncode=1, stdout="",
            stderr="unknown option '--no-warnings'\nTry 'copilot --help'\nreal error",
        )
        collected: list[PipelineEvent] = []
        ctx.event_bus.subscribe(lambda e: collected.append(e))

        result = run_step("dev", "cmd", "3-1-feat", cfg, ctx)
        ctx.event_bus.drain()

        # The filtered stderr should only contain "real error"
        assert result.status == StepStatus.FAILED
        assert "real error" in result.error
        assert "--no-warnings" not in result.error
        assert "copilot --help" not in result.error


# ---------------------------------------------------------------------------
# mark_story_done
# ---------------------------------------------------------------------------


class TestMarkStoryDone:
    def test_marks_story_done(self, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(textwrap.dedent("""\
            development_status:
              3-1-feature: in-progress
              3-2-other: backlog
        """), encoding="utf-8")

        cfg = Config(
            sprint_status=ss,
            log_file=tmp_path / "test.log",
            quiet=True,
        )
        mark_story_done("3-1-feature", cfg)

        content = ss.read_text(encoding="utf-8")
        assert "3-1-feature: done" in content
        assert "3-2-other: backlog" in content

    def test_already_done_is_noop(self, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        original = textwrap.dedent("""\
            development_status:
              3-1-feature: done
        """)
        ss.write_text(original, encoding="utf-8")

        cfg = Config(sprint_status=ss, log_file=tmp_path / "test.log", quiet=True)
        mark_story_done("3-1-feature", cfg)

        assert ss.read_text(encoding="utf-8") == original

    def test_missing_file_is_noop(self, tmp_path):
        cfg = Config(
            sprint_status=tmp_path / "nonexistent.yaml",
            log_file=tmp_path / "test.log",
            quiet=True,
        )
        mark_story_done("3-1-feature", cfg)  # should not raise

    def test_missing_story_key_is_noop(self, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        original = textwrap.dedent("""\
            development_status:
              3-1-feature: in-progress
        """)
        ss.write_text(original, encoding="utf-8")

        cfg = Config(sprint_status=ss, log_file=tmp_path / "test.log", quiet=True)
        mark_story_done("9-9-nonexistent", cfg)

        assert ss.read_text(encoding="utf-8") == original

    def test_invalidates_cache(self, tmp_path):
        ss = tmp_path / "sprint-status.yaml"
        ss.write_text(textwrap.dedent("""\
            development_status:
              3-1-feature: in-progress
        """), encoding="utf-8")

        cfg = Config(sprint_status=ss, log_file=tmp_path / "test.log", quiet=True)

        from bmad_automate.stories import _load_sprint_status
        _load_sprint_status(ss)

        mark_story_done("3-1-feature", cfg)

        # Cache should be invalidated so re-read reflects the update
        data = _load_sprint_status(ss)
        assert data["development_status"]["3-1-feature"] == "done"


# ---------------------------------------------------------------------------
# run_git_pull
# ---------------------------------------------------------------------------


class TestRunGitPull:
    def test_skip_pull(self, make_config):
        cfg = make_config(skip_pull=True)
        ctx = RunContext(config=cfg)
        result = run_git_pull("3-1-feat", cfg, "resolve prompt", ctx)
        assert result.status == StepStatus.SKIPPED

    def test_dry_run(self, make_config):
        cfg = make_config(dry_run=True)
        ctx = RunContext(config=cfg)
        result = run_git_pull("3-1-feat", cfg, "resolve prompt", ctx)
        assert result.status == StepStatus.SKIPPED

    @patch("bmad_automate.git.run_git_command")
    def test_pull_and_push_success(self, mock_cmd, config, ctx):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("git pull", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("git push", 0, stdout="ok", stderr=""),
        ]
        result = run_git_pull("3-1-feat", config, "resolve prompt", ctx)
        assert result.status == StepStatus.SUCCESS
        assert result.name == "git-pull"
        assert mock_cmd.call_count == 2

    @patch("bmad_automate.git.run_git_command")
    def test_push_failure(self, mock_cmd, config, ctx):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("git pull", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("git push", 1, stdout="", stderr="push failed"),
        ]
        result = run_git_pull("3-1-feat", config, "resolve prompt", ctx)
        assert result.status == StepStatus.FAILED
        assert "push failed" in result.error

    @patch("bmad_automate.git.run_step")
    @patch("bmad_automate.git.run_git_command")
    def test_merge_conflict_invokes_ai(self, mock_cmd, mock_step, config, ctx):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess(
                "git pull", 1, stdout="CONFLICT in file.txt", stderr=""
            ),
        ]
        mock_step.return_value = StepResult(
            name="git-pull-resolve", status=StepStatus.SUCCESS, duration=5.0
        )
        result = run_git_pull("3-1-feat", config, "resolve conflicts", ctx)
        assert result.status == StepStatus.SUCCESS
        mock_step.assert_called_once()
        call_args = mock_step.call_args
        assert call_args[0][0] == "git-pull-resolve"
        # The command should include the merge conflict prompt
        assert "resolve conflicts" in call_args[0][1]

    @patch("bmad_automate.git.run_git_command")
    def test_pull_failure_no_conflict(self, mock_cmd, config, ctx):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("git pull", 1, stdout="", stderr="network error"),
            # git status check
            subprocess.CompletedProcess("git status", 0, stdout="", stderr=""),
        ]
        result = run_git_pull("3-1-feat", config, "resolve prompt", ctx)
        assert result.status == StepStatus.FAILED
        assert "network error" in result.error

    @patch("bmad_automate.git.run_step")
    @patch("bmad_automate.git.run_git_command")
    def test_conflict_detected_via_status(self, mock_cmd, mock_step, config, ctx):
        """Conflicts detected through git status --porcelain (UU lines)."""
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("git pull", 1, stdout="", stderr="merge failed"),
            subprocess.CompletedProcess("git status", 0, stdout="UU file.txt\n", stderr=""),
        ]
        mock_step.return_value = StepResult(
            name="git-pull-resolve", status=StepStatus.SUCCESS, duration=1.0
        )
        result = run_git_pull("3-1-feat", config, "resolve", ctx)
        assert result.status == StepStatus.SUCCESS
        mock_step.assert_called_once()

    @patch("bmad_automate.git.run_git_command")
    def test_timeout_returns_failed(self, mock_cmd, config, ctx):
        mock_cmd.side_effect = subprocess.TimeoutExpired(cmd="git pull", timeout=120)
        result = run_git_pull("3-1-feat", config, "resolve", ctx)
        assert result.status == StepStatus.FAILED
        assert "timed out" in result.error

    @patch("bmad_automate.git.run_git_command")
    def test_exception_returns_failed(self, mock_cmd, config, ctx):
        mock_cmd.side_effect = OSError("git not found")
        result = run_git_pull("3-1-feat", config, "resolve", ctx)
        assert result.status == StepStatus.FAILED
        assert "git not found" in result.error


# ---------------------------------------------------------------------------
# run_after_epic_commit
# ---------------------------------------------------------------------------


class TestRunAfterEpicCommit:
    @patch("bmad_automate.git.run_git_command")
    def test_nothing_to_commit(self, mock_cmd, config):
        mock_cmd.return_value = subprocess.CompletedProcess(
            "git status", 0, stdout="", stderr=""
        )
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.SUCCESS
        assert mock_cmd.call_count == 1  # only status check

    @patch("bmad_automate.git.run_git_command")
    def test_commit_pull_push_success(self, mock_cmd, config):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("status", 0, stdout=" M file.txt\n", stderr=""),
            subprocess.CompletedProcess("commit", 0, stdout="committed", stderr=""),
            subprocess.CompletedProcess("pull", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("push", 0, stdout="ok", stderr=""),
        ]
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.SUCCESS
        assert mock_cmd.call_count == 4

    @patch("bmad_automate.git.run_git_command")
    def test_commit_failure(self, mock_cmd, config):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("status", 0, stdout=" M file.txt\n", stderr=""),
            subprocess.CompletedProcess("commit", 1, stdout="", stderr="commit failed"),
        ]
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.FAILED
        assert "commit failed" in result.error

    @patch("bmad_automate.git.run_git_command")
    def test_pull_failure(self, mock_cmd, config):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("status", 0, stdout=" M file.txt\n", stderr=""),
            subprocess.CompletedProcess("commit", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("pull", 1, stdout="", stderr="pull failed"),
        ]
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.FAILED
        assert "pull failed" in result.error

    @patch("bmad_automate.git.run_git_command")
    def test_push_failure(self, mock_cmd, config):
        mock_cmd.side_effect = [
            subprocess.CompletedProcess("status", 0, stdout=" M file.txt\n", stderr=""),
            subprocess.CompletedProcess("commit", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("pull", 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess("push", 1, stdout="", stderr="push failed"),
        ]
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.FAILED
        assert "push failed" in result.error

    @patch("bmad_automate.git.run_git_command")
    def test_timeout(self, mock_cmd, config):
        mock_cmd.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=120)
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.FAILED
        assert "Timed out" in result.error

    @patch("bmad_automate.git.run_git_command")
    def test_exception(self, mock_cmd, config):
        mock_cmd.side_effect = RuntimeError("unexpected")
        result = run_after_epic_commit(3, config)
        assert result.status == StepStatus.FAILED
        assert "unexpected" in result.error
