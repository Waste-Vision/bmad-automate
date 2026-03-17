"""Tests for models.py — enums, dataclasses, constants."""

from pathlib import Path

from bmad_automate.models import (
    AI_PROVIDERS,
    ALL_STEPS,
    STORY_PATTERN,
    Config,
    StepResult,
    StepStatus,
    StoryResult,
    StoryStatus,
)


class TestStoryPattern:
    def test_matches_valid_keys(self):
        assert STORY_PATTERN.match("3-3-account-translation")
        assert STORY_PATTERN.match("1-1-setup")
        assert STORY_PATTERN.match("12-5-long-kebab-case-name")

    def test_rejects_invalid_keys(self):
        assert not STORY_PATTERN.match("epic-3-retrospective")
        assert not STORY_PATTERN.match("not-a-story")
        assert not STORY_PATTERN.match("3-setup")
        assert not STORY_PATTERN.match("")


class TestStepResult:
    def test_defaults(self):
        r = StepResult(name="dev-story", status=StepStatus.SUCCESS)
        assert r.duration == 0.0
        assert r.error == ""

    def test_with_error(self):
        r = StepResult(
            name="git-pull", status=StepStatus.FAILED, error="conflict"
        )
        assert r.error == "conflict"


class TestStoryResult:
    def test_defaults(self):
        r = StoryResult(key="3-1-feature", status=StoryStatus.COMPLETED)
        assert r.steps == []
        assert r.duration == 0.0
        assert r.failed_step == ""


class TestConfig:
    def test_default_values(self):
        c = Config()
        assert c.dry_run is False
        assert c.retries == 1
        assert c.timeout == 3600
        assert c.limit == 0
        assert c.ai_provider == "claude"

    def test_ai_command(self):
        c = Config(ai_provider="claude")
        assert c.ai_command == AI_PROVIDERS["claude"]

        c2 = Config(ai_provider="github")
        assert c2.ai_command == AI_PROVIDERS["github"]

    def test_all_steps_tuple(self):
        assert ALL_STEPS == ("create", "dev", "review", "commit", "pull")
