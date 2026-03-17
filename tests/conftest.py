"""Shared test fixtures for BMAD Automate tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bmad_automate.context import RunContext
from bmad_automate.models import Config


SAMPLE_SPRINT_STATUS = textwrap.dedent("""\
    development_status:
      1-1-setup-project: done
      1-2-auth-module: done
      1-3-user-profile: done
      epic-1-retrospective: done
      2-1-data-models: done
      2-2-api-endpoints: in-progress
      2-3-search-feature: ready-for-dev
      2-4-notifications: backlog
      3-1-dashboard: ready-for-dev
      3-2-reports: backlog
      3-3-admin-panel: backlog
      epic-3-retrospective: optional
      4-1-performance: backlog
""")


@pytest.fixture()
def tmp_sprint_status(tmp_path: Path) -> Path:
    """Write a sample sprint-status.yaml and return its path."""
    p = tmp_path / "sprint-status.yaml"
    p.write_text(SAMPLE_SPRINT_STATUS, encoding="utf-8")
    return p


@pytest.fixture()
def make_config(tmp_path: Path, tmp_sprint_status: Path):
    """Factory fixture that returns a Config with tmp paths pre-filled."""

    def _make(**overrides) -> Config:
        defaults = dict(
            sprint_status=tmp_sprint_status,
            story_dir=tmp_path,
            log_file=tmp_path / "test.log",
            bmad_dir=tmp_path / "_bmad",
            yes=True,
            quiet=True,
        )
        defaults.update(overrides)
        return Config(**defaults)

    return _make


@pytest.fixture()
def config(make_config) -> Config:
    """A default Config pointing at tmp paths."""
    return make_config()


@pytest.fixture()
def ctx(config: Config) -> RunContext:
    """A fresh RunContext for testing."""
    return RunContext(config=config)
