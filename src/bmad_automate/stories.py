"""Sprint-status YAML parsing, caching, and story filtering."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import typer
import yaml

from bmad_automate.models import STORY_PATTERN, Config
from bmad_automate.ui import console

# ---------------------------------------------------------------------------
# YAML cache — avoids re-reading and re-parsing the same file repeatedly
# ---------------------------------------------------------------------------

_yaml_cache: dict[Path, tuple[float, dict]] = {}  # path -> (mtime, parsed_data)


def _load_sprint_status(path: Path) -> dict | None:
    """Return the parsed sprint-status data, using a cache keyed on mtime.

    Returns ``None`` when the file is missing or has no ``development_status``
    key.
    """
    if not path.exists():
        return None

    mtime = path.stat().st_mtime
    cached = _yaml_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        return None

    _yaml_cache[path] = (mtime, data)
    return data


def invalidate_cache(path: Path | None = None) -> None:
    """Drop cached YAML data.  Call after modifying sprint-status.yaml."""
    if path is None:
        _yaml_cache.clear()
    else:
        _yaml_cache.pop(path, None)


# ---------------------------------------------------------------------------
# Story retrieval
# ---------------------------------------------------------------------------

def get_actionable_stories(config: Config) -> dict[str, list[str]]:
    """Parse sprint-status.yaml and return stories grouped by actionable status.

    Priority order: review > in-progress > ready-for-dev > backlog.
    """
    data = _load_sprint_status(config.sprint_status)
    if data is None:
        if not config.sprint_status.exists():
            console.print(
                f"[red]Error: Sprint status file not found: "
                f"{config.sprint_status}[/red]"
            )
        else:
            console.print("[red]Error: Invalid sprint-status.yaml format[/red]")
        sys.exit(2)

    dev_status = data["development_status"]

    actionable_statuses = ["review", "in-progress", "ready-for-dev", "backlog"]
    stories_by_status: dict[str, list[str]] = {s: [] for s in actionable_statuses}

    for key, status in dev_status.items():
        if STORY_PATTERN.match(key) and status in actionable_statuses:
            stories_by_status[status].append(key)

    return stories_by_status


def get_all_story_keys(config: Config) -> set[str]:
    """Get all story keys from sprint-status.yaml regardless of status."""
    data = _load_sprint_status(config.sprint_status)
    if data is None:
        return set()
    return {
        key
        for key in data["development_status"]
        if STORY_PATTERN.match(key)
    }


# ---------------------------------------------------------------------------
# Epic helpers
# ---------------------------------------------------------------------------

def is_epic_complete(epic_num: int, config: Config) -> bool:
    """Check whether all stories for a given epic have status 'done'."""
    data = _load_sprint_status(config.sprint_status)
    if data is None:
        return False

    dev_status = data["development_status"]
    epic_prefix = f"{epic_num}-"

    statuses = [
        status
        for key, status in dev_status.items()
        if key.startswith(epic_prefix) and STORY_PATTERN.match(key)
    ]
    return len(statuses) > 0 and all(s == "done" for s in statuses)


def get_epics_needing_retro(config: Config) -> list[int]:
    """Find epics where all stories are done but the retrospective is not."""
    data = _load_sprint_status(config.sprint_status)
    if data is None:
        return []

    dev_status = data["development_status"]
    epic_pattern = re.compile(r"^(\d+)-\d+-.+$")

    stories_by_epic: dict[int, list[str]] = {}
    for key, status in dev_status.items():
        m = epic_pattern.match(key)
        if m:
            epic_num = int(m.group(1))
            stories_by_epic.setdefault(epic_num, []).append(status)

    epics_needing_retro: list[int] = []
    for epic_num, statuses in sorted(stories_by_epic.items()):
        if not all(s == "done" for s in statuses):
            continue
        retro_key = f"epic-{epic_num}-retrospective"
        retro_status = dev_status.get(retro_key, "")
        if retro_status != "done":
            epics_needing_retro.append(epic_num)

    return epics_needing_retro


def has_next_epic(epic_num: int, config: Config) -> bool:
    """Check whether the next epic (epic_num + 1) has stories."""
    data = _load_sprint_status(config.sprint_status)
    if data is None:
        return False

    dev_status = data["development_status"]
    next_epic_prefix = f"{epic_num + 1}-"

    return any(
        key.startswith(next_epic_prefix) and STORY_PATTERN.match(key)
        for key in dev_status
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_stories(
    stories_by_status: dict[str, list[str]], config: Config
) -> list[str]:
    """Apply filters to produce the final ordered list of stories to process."""
    if config.specific_stories:
        all_keys = get_all_story_keys(config)
        valid_stories = [s for s in config.specific_stories if s in all_keys]
        if len(valid_stories) != len(config.specific_stories):
            missing = set(config.specific_stories) - set(valid_stories)
            console.print(
                f"[yellow]Warning: Stories not found in sprint-status.yaml: "
                f"{missing}[/yellow]"
            )
        if config.epic:
            epic_prefixes = tuple(f"{e}-" for e in config.epic)
            valid_stories = [
                s for s in valid_stories if s.startswith(epic_prefixes)
            ]
        return valid_stories

    stories = (
        stories_by_status.get("review", [])
        + stories_by_status.get("in-progress", [])
        + stories_by_status.get("ready-for-dev", [])
        + stories_by_status.get("backlog", [])
    )

    if config.epic:
        epic_prefixes = tuple(f"{e}-" for e in config.epic)
        stories = [s for s in stories if s.startswith(epic_prefixes)]
        if not stories:
            console.print(
                f"[yellow]Warning: No stories found for epic(s) "
                f"{','.join(str(e) for e in config.epic)}[/yellow]"
            )

    if config.start_from:
        try:
            start_idx = stories.index(config.start_from)
            stories = stories[start_idx:]
        except ValueError:
            console.print(
                f"[yellow]Warning: Start story '{config.start_from}' "
                "not found, processing all[/yellow]"
            )

    if config.limit > 0:
        stories = stories[: config.limit]

    return stories


def get_story_path(story_key: str, config: Config) -> Path:
    """Construct the file path for a story's markdown file."""
    return config.story_dir / f"{story_key}.md"


def parse_epic_list(value: str) -> list[int]:
    """Parse a comma-separated string of epic numbers into a sorted list."""
    if not value.strip():
        return []
    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        try:
            n = int(part)
            if n <= 0:
                raise ValueError  # noqa: TRY301
            result.append(n)
        except ValueError:
            console.print(
                f"[red]Error: Invalid epic number '{part}' — "
                f"must be a positive integer[/red]"
            )
            raise typer.Exit(2)  # noqa: B904
    return sorted(set(result))
