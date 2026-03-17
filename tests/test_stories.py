"""Tests for stories.py — YAML parsing, filtering, epic helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bmad_automate.models import Config
from bmad_automate.stories import (
    _load_sprint_status,
    filter_stories,
    get_actionable_stories,
    get_all_story_keys,
    get_epics_needing_retro,
    get_story_path,
    has_next_epic,
    invalidate_cache,
    is_epic_complete,
    parse_epic_list,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure YAML cache is clean between tests."""
    invalidate_cache()
    yield
    invalidate_cache()


class TestLoadSprintStatus:
    def test_loads_valid_file(self, tmp_sprint_status: Path):
        data = _load_sprint_status(tmp_sprint_status)
        assert data is not None
        assert "development_status" in data

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        assert _load_sprint_status(tmp_path / "missing.yaml") is None

    def test_returns_none_for_no_dev_status(self, tmp_path: Path):
        p = tmp_path / "bad.yaml"
        p.write_text("foo: bar\n", encoding="utf-8")
        assert _load_sprint_status(p) is None

    def test_caches_on_mtime(self, tmp_sprint_status: Path):
        d1 = _load_sprint_status(tmp_sprint_status)
        d2 = _load_sprint_status(tmp_sprint_status)
        assert d1 is d2  # same object from cache


class TestGetActionableStories:
    def test_groups_by_status(self, config: Config):
        result = get_actionable_stories(config)
        assert "2-2-api-endpoints" in result["in-progress"]
        assert "2-3-search-feature" in result["ready-for-dev"]
        assert "2-4-notifications" in result["backlog"]
        assert "3-1-dashboard" in result["ready-for-dev"]

    def test_excludes_done_stories(self, config: Config):
        result = get_actionable_stories(config)
        all_stories = []
        for stories in result.values():
            all_stories.extend(stories)
        assert "1-1-setup-project" not in all_stories

    def test_excludes_non_story_keys(self, config: Config):
        result = get_actionable_stories(config)
        all_stories = []
        for stories in result.values():
            all_stories.extend(stories)
        assert "epic-1-retrospective" not in all_stories
        assert "epic-3-retrospective" not in all_stories


class TestGetAllStoryKeys:
    def test_returns_all_story_pattern_keys(self, config: Config):
        keys = get_all_story_keys(config)
        assert "1-1-setup-project" in keys
        assert "2-2-api-endpoints" in keys
        assert "epic-1-retrospective" not in keys


class TestIsEpicComplete:
    def test_complete_epic(self, config: Config):
        assert is_epic_complete(1, config) is True

    def test_incomplete_epic(self, config: Config):
        assert is_epic_complete(2, config) is False

    def test_nonexistent_epic(self, config: Config):
        assert is_epic_complete(99, config) is False


class TestGetEpicsNeedingRetro:
    def test_finds_completed_epic_without_retro(self, tmp_path: Path):
        yaml_text = textwrap.dedent("""\
            development_status:
              5-1-feature: done
              5-2-feature: done
        """)
        p = tmp_path / "ss.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        cfg = Config(sprint_status=p)
        result = get_epics_needing_retro(cfg)
        assert 5 in result

    def test_skips_epic_with_retro_done(self, config: Config):
        # Epic 1 has all done + retro done
        result = get_epics_needing_retro(config)
        assert 1 not in result

    def test_skips_incomplete_epic(self, config: Config):
        result = get_epics_needing_retro(config)
        assert 2 not in result


class TestHasNextEpic:
    def test_has_next(self, config: Config):
        assert has_next_epic(1, config) is True  # epic 2 exists
        assert has_next_epic(2, config) is True  # epic 3 exists
        assert has_next_epic(3, config) is True  # epic 4 exists

    def test_no_next(self, config: Config):
        assert has_next_epic(4, config) is False
        assert has_next_epic(99, config) is False


class TestFilterStories:
    def test_orders_by_status_priority(self, config: Config):
        stories_by_status = get_actionable_stories(config)
        result = filter_stories(stories_by_status, config)
        # in-progress first, then ready-for-dev, then backlog
        assert result[0] == "2-2-api-endpoints"

    def test_epic_filter(self, make_config):
        cfg = make_config(epic=[3])
        stories_by_status = get_actionable_stories(cfg)
        result = filter_stories(stories_by_status, cfg)
        assert all(s.startswith("3-") for s in result)

    def test_limit(self, make_config):
        cfg = make_config(limit=2)
        stories_by_status = get_actionable_stories(cfg)
        result = filter_stories(stories_by_status, cfg)
        assert len(result) == 2

    def test_start_from(self, make_config):
        cfg = make_config(start_from="2-3-search-feature")
        stories_by_status = get_actionable_stories(cfg)
        result = filter_stories(stories_by_status, cfg)
        assert result[0] == "2-3-search-feature"
        assert "2-2-api-endpoints" not in result

    def test_specific_stories(self, make_config):
        cfg = make_config(specific_stories=["2-2-api-endpoints", "3-1-dashboard"])
        stories_by_status = get_actionable_stories(cfg)
        result = filter_stories(stories_by_status, cfg)
        assert result == ["2-2-api-endpoints", "3-1-dashboard"]

    def test_specific_stories_with_epic_filter(self, make_config):
        cfg = make_config(
            specific_stories=["2-2-api-endpoints", "3-1-dashboard"],
            epic=[3],
        )
        stories_by_status = get_actionable_stories(cfg)
        result = filter_stories(stories_by_status, cfg)
        assert result == ["3-1-dashboard"]


class TestGetStoryPath:
    def test_constructs_path(self, config: Config):
        p = get_story_path("3-1-feature", config)
        assert p == config.story_dir / "3-1-feature.md"


class TestParseEpicList:
    def test_single(self):
        assert parse_epic_list("3") == [3]

    def test_multiple(self):
        assert parse_epic_list("5,3,4") == [3, 4, 5]

    def test_empty(self):
        assert parse_epic_list("") == []
        assert parse_epic_list("  ") == []

    def test_deduplication(self):
        assert parse_epic_list("3,3,3") == [3]

    def test_invalid_raises(self):
        with pytest.raises((SystemExit, Exception)):
            parse_epic_list("abc")

    def test_zero_raises(self):
        with pytest.raises((SystemExit, Exception)):
            parse_epic_list("0")
