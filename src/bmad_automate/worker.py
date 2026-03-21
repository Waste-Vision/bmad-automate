"""EpicWorker — processes stories for a single epic in a worktree."""

from __future__ import annotations

import copy
from pathlib import Path

from bmad_automate.context import RunContext
from bmad_automate.events import EPIC_DONE, EPIC_START, PipelineEvent
from bmad_automate.models import Config, StoryResult, StoryStatus
from bmad_automate.pipeline import process_story, run_after_epic_pipeline
from bmad_automate.stories import get_epics_needing_retro


class EpicWorker:
    """Processes all stories for a single epic sequentially.

    When running in parallel mode, each worker operates in its own git
    worktree with paths re-scoped to the worktree directory.
    In sequential mode (parallel_epics=1), workers use the main working
    directory.
    """

    def __init__(
        self,
        epic_num: int,
        stories: list[str],
        story_status_map: dict[str, str],
        config: Config,
        ctx: RunContext,
        worktree_path: Path | None = None,
        run_after_epic: bool = False,
    ) -> None:
        self.epic_num = epic_num
        self.stories = stories
        self.story_status_map = story_status_map
        self.ctx = ctx
        self.worktree_path = worktree_path
        self.run_after_epic = run_after_epic
        self.results: list[StoryResult] = []

        # Create a worktree-scoped config if running in a worktree
        if worktree_path is not None:
            self.config = copy.copy(config)
            # Re-scope paths to the worktree — absolute paths must be made
            # relative to project_root first, otherwise Path('/a') / Path('/b')
            # returns Path('/b') (absolute path wins in pathlib).
            def _rescope(p: Path) -> Path:
                try:
                    return worktree_path / p.relative_to(config.project_root)
                except ValueError:
                    return worktree_path / p  # already relative

            self.config.sprint_status = _rescope(config.sprint_status)
            self.config.story_dir = _rescope(config.story_dir)
            self.config.bmad_dir = _rescope(config.bmad_dir)
            self.config.project_root = worktree_path
            self.config.in_worktree = True
        else:
            self.config = config

    def run(self) -> list[StoryResult]:
        """Process all stories for this epic. Returns list of StoryResults."""
        bus = self.ctx.event_bus

        bus.emit(PipelineEvent(
            epic=self.epic_num, story=None, step=None,
            kind=EPIC_START,
            payload={"stories": self.stories},
        ))

        for story_key in self.stories:
            if self.ctx.run_control.should_stop(self.epic_num):
                break

            # Wait if this epic is paused
            self.ctx.run_control.wait_if_paused(self.epic_num)

            if self.ctx.run_control.should_stop(self.epic_num):
                break

            result = process_story(
                story_key,
                self.config,
                self.ctx,
                self.story_status_map.get(story_key, ""),
            )
            self.results.append(result)

            # Check pause-after-story
            self.ctx.run_control.check_pause_after_story(self.epic_num)

            if result.status == StoryStatus.FAILED:
                break

        # Run after-epic pipeline if requested and all stories succeeded
        if (
            self.run_after_epic
            and not self.ctx.run_control.should_stop(self.epic_num)
            and not any(r.status == StoryStatus.FAILED for r in self.results)
            and self.epic_num in get_epics_needing_retro(self.config)
        ):
            retro_results: list = []
            run_after_epic_pipeline(self.epic_num, self.config, self.ctx, retro_results)

        bus.emit(PipelineEvent(
            epic=self.epic_num, story=None, step=None,
            kind=EPIC_DONE,
            payload={
                "stories_completed": sum(
                    1 for r in self.results
                    if r.status == StoryStatus.COMPLETED
                ),
                "stories_failed": sum(
                    1 for r in self.results
                    if r.status == StoryStatus.FAILED
                ),
            },
        ))

        return self.results
