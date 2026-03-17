"""Tests for ui.py — formatting, logging, step helpers."""

from __future__ import annotations

from bmad_automate.models import Config
from bmad_automate.ui import format_duration, get_enabled_steps, log_to_file


class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(45) == "45s"
        assert format_duration(0) == "0s"
        assert format_duration(59) == "59s"

    def test_minutes_and_seconds(self):
        assert format_duration(60) == "1m 00s"
        assert format_duration(61) == "1m 01s"
        assert format_duration(201) == "3m 21s"
        assert format_duration(3600) == "60m 00s"

    def test_fractional_seconds(self):
        assert format_duration(45.7) == "46s"
        assert format_duration(0.4) == "0s"


class TestGetEnabledSteps:
    def test_all_enabled(self):
        cfg = Config()
        steps = get_enabled_steps(cfg)
        assert steps == [
            "create-story", "dev-story", "code-review",
            "git-commit", "git-pull",
        ]

    def test_skip_some(self):
        cfg = Config(skip_create=True, skip_pull=True)
        steps = get_enabled_steps(cfg)
        assert "create-story" not in steps
        assert "git-pull" not in steps
        assert "dev-story" in steps

    def test_skip_all(self):
        cfg = Config(
            skip_create=True, skip_dev=True, skip_review=True,
            skip_commit=True, skip_pull=True,
        )
        assert get_enabled_steps(cfg) == []


class TestLogToFile:
    def test_writes_timestamped_line(self, config: Config):
        log_to_file("test message", config)
        content = config.log_file.read_text(encoding="utf-8")
        assert "test message" in content
        assert content.startswith("[")
        assert "] test message\n" in content

    def test_appends(self, config: Config):
        log_to_file("first", config)
        log_to_file("second", config)
        lines = config.log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]
