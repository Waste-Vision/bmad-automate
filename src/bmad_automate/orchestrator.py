"""Orchestrator — manages parallel epic execution."""

from __future__ import annotations

import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from bmad_automate.context import RunContext
from bmad_automate.dependencies import build_dag
from bmad_automate.events import STATUS_CHANGE, STORY_DONE, PipelineEvent
from bmad_automate.merge_queue import MergeQueue
from bmad_automate.models import Config, StoryResult, StoryStatus
from bmad_automate.rate_limit import RateLimiter, is_rate_limited
from bmad_automate.stories import get_epics_needing_retro
from bmad_automate.ui import console
from bmad_automate.worker import EpicWorker
from bmad_automate.worktree import WorktreeManager


# Status priority for forward-only transitions (higher index = further along)
_STATUS_ORDER = {
    "backlog": 0,
    "ready-for-dev": 1,
    "in-progress": 2,
    "review": 3,
    "done": 4,
}


class StatusManager:
    """In-memory authoritative state map for story statuses.

    During parallel execution, each worktree writes to its own copy of
    sprint-status.yaml, but the StatusManager is the single source of
    truth. Status can only move forward (done > review > in-progress > ...).
    """

    def __init__(self) -> None:
        self._statuses: dict[str, str] = {}
        self._lock = threading.Lock()

    def update(self, story_key: str, new_status: str) -> bool:
        """Update a story's status if the new status is further along.

        Returns True if the status was actually updated.
        """
        with self._lock:
            current = self._statuses.get(story_key, "backlog")
            current_order = _STATUS_ORDER.get(current, -1)
            new_order = _STATUS_ORDER.get(new_status, -1)
            if new_order > current_order:
                self._statuses[story_key] = new_status
                return True
            return False

    def get(self, story_key: str) -> str:
        """Get the current status for a story."""
        with self._lock:
            return self._statuses.get(story_key, "backlog")

    def get_all(self) -> dict[str, str]:
        """Get a snapshot of all statuses."""
        with self._lock:
            return dict(self._statuses)

    def load_from_yaml(self, yaml_data: dict) -> None:
        """Initialize from parsed sprint-status.yaml data."""
        dev_status = yaml_data.get("development_status", {})
        with self._lock:
            for key, status in dev_status.items():
                self._statuses[key] = status


def _group_stories_by_epic(stories: list[str]) -> dict[int, list[str]]:
    """Group story keys by their epic number."""
    groups: dict[int, list[str]] = {}
    pattern = re.compile(r"^(\d+)-")
    for story in stories:
        m = pattern.match(story)
        if m:
            epic_num = int(m.group(1))
            groups.setdefault(epic_num, []).append(story)
    return dict(sorted(groups.items()))


class Orchestrator:
    """Manages parallel epic processing.

    When parallel_epics > 1, creates worktrees and spawns EpicWorkers
    in a ThreadPoolExecutor. When parallel_epics == 1, runs sequentially
    without worktrees (same behavior as the original CLI).
    """

    def __init__(
        self,
        stories: list[str],
        story_status_map: dict[str, str],
        config: Config,
        ctx: RunContext,
    ) -> None:
        self.stories = stories
        self.story_status_map = story_status_map
        self.config = config
        self.ctx = ctx
        self.epic_groups = _group_stories_by_epic(stories)

        # Add epics that already have all stories done but retro not yet run
        if not config.skip_retro:
            story_epic_set = set(self.epic_groups.keys())
            for epic_num in get_epics_needing_retro(config):
                if epic_num not in story_epic_set:
                    self.epic_groups[epic_num] = []  # after-epic only, no stories to process
            # Re-sort after potential additions
            self.epic_groups = dict(sorted(self.epic_groups.items()))

        project_root = config.project_root
        self.worktree_mgr = WorktreeManager(project_root=project_root)
        self.merge_queue = MergeQueue(project_root=project_root, config=config, ctx=ctx)
        self.status_manager = StatusManager()
        self.rate_limiter = RateLimiter(max_concurrent=config.parallel_epics)
        self.results: list[StoryResult] = []

        # Load initial statuses and subscribe to status changes
        self._init_status_manager()

    def _init_status_manager(self) -> None:
        """Load current statuses from YAML and subscribe to events."""
        if self.config.sprint_status.exists():
            with open(self.config.sprint_status, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data:
                self.status_manager.load_from_yaml(data)

        # Subscribe to status change events from workers
        def _on_event(event: PipelineEvent) -> None:
            if event.kind == STORY_DONE and event.story:
                status = event.payload.get("status", "")
                if status:
                    self.status_manager.update(event.story, status)

        self.ctx.event_bus.subscribe(_on_event)

    def _build_dag(self) -> None:
        """Build dependency DAG from sprint-status data."""
        yaml_text = ""
        yaml_data: dict = {}
        if self.config.sprint_status.exists():
            with open(self.config.sprint_status, encoding="utf-8") as f:
                yaml_text = f.read()
            yaml_data = yaml.safe_load(yaml_text) or {}

        epic_list = sorted(self.epic_groups.keys())
        self.dag = build_dag(yaml_data, yaml_text, epic_list, self.config.sprint_status)

        if self.dag.has_dependencies():
            console.print(f"\n  [dim]Dependency graph:\n{self.dag}[/dim]")

    def _load_worktree_statuses(self, epic_num: int, wt_path: Path) -> dict[str, str]:
        """Read story statuses from a worktree's sprint-status.yaml."""
        try:
            wt_sprint = wt_path / self.config.sprint_status.relative_to(
                self.config.project_root
            )
            if wt_sprint.exists():
                with open(wt_sprint, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return data.get("development_status", {})
        except Exception:
            pass
        return {}

    def _seed_completed_worktrees(
        self,
        pending_epics: set[int],
        completed_epics: set[int],
        worktree_paths: dict[int, Path],
    ) -> None:
        """At run start, detect worktrees whose stories are already all done.

        Such worktrees are enqueued for merge immediately so we don't re-run
        their stories. For partially-done worktrees the story_status_map is
        updated so already-done stories are skipped by the worker.
        """
        for epic_num in list(pending_epics):
            wt_path = self.worktree_mgr.get_worktree_path(epic_num)
            if not wt_path.exists():
                continue

            stories = self.epic_groups.get(epic_num, [])
            if not stories:
                continue  # after-epic-only, no stories to check

            wt_statuses = self._load_worktree_statuses(epic_num, wt_path)
            if not wt_statuses:
                continue

            # Propagate worktree statuses into the shared story_status_map
            # so workers skip already-done stories.
            for story in stories:
                wt_st = wt_statuses.get(story, "")
                if wt_st:
                    self.story_status_map[story] = wt_st

            # If every story is done, skip the worker entirely.
            if all(wt_statuses.get(s, "") == "done" for s in stories):
                console.print(
                    f"\n  [cyan]Epic {epic_num}: worktree already complete "
                    f"— queuing for merge[/cyan]"
                )
                worktree_paths[epic_num] = wt_path
                completed_epics.add(epic_num)
                self.merge_queue.enqueue(epic_num, wt_path)
                pending_epics.discard(epic_num)

    def run_parallel(self) -> list[StoryResult]:
        """Execute epics in parallel using worktrees."""
        # Build dependency DAG
        self._build_dag()

        max_workers = min(self.config.parallel_epics, len(self.epic_groups))

        # Register all epics with RunControl
        for epic_num in self.epic_groups:
            self.ctx.run_control.register_epic(epic_num)

        console.print(
            f"\n  [cyan]Parallel mode: {len(self.epic_groups)} epics, "
            f"{max_workers} concurrent workers[/cyan]"
        )

        completed_epics: set[int] = set()
        failed_epics: set[int] = set()

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit epics whose dependencies are satisfied
                pending_epics = set(self.epic_groups.keys())
                futures: dict = {}
                worktree_paths: dict[int, Path] = {}

                # Check for existing worktrees with completed work before
                # submitting any workers — avoids re-running already-done stories.
                self._seed_completed_worktrees(
                    pending_epics, completed_epics, worktree_paths
                )
                def _process_merges() -> None:
                    for mr in self.merge_queue.process_all():
                        if mr.success:
                            try:
                                self.worktree_mgr.remove(mr.epic_num)
                            except Exception:
                                pass
                        else:
                            failed_epics.add(mr.epic_num)
                            completed_epics.discard(mr.epic_num)

                # Merge any pre-existing complete worktrees immediately.
                if completed_epics and not self.ctx.interrupted:
                    _process_merges()

                def _submit_ready() -> None:
                    ready = self.dag.get_ready_epics(completed_epics)
                    for epic_num in ready:
                        if epic_num in pending_epics and epic_num not in futures.values():
                            if self.ctx.interrupted:
                                break
                            wt_path = self.worktree_mgr.create(epic_num)
                            worktree_paths[epic_num] = wt_path
                            worker = EpicWorker(
                                epic_num=epic_num,
                                stories=self.epic_groups[epic_num],
                                story_status_map=self.story_status_map,
                                config=self.config,
                                ctx=self.ctx,
                                worktree_path=wt_path,
                                run_after_epic=not self.config.skip_retro,
                            )
                            future = executor.submit(worker.run)
                            futures[future] = epic_num
                            pending_epics.discard(epic_num)

                _submit_ready()

                while futures:
                    done_futures = []
                    for future in as_completed(futures):
                        done_futures.append(future)
                        epic_num = futures[future]
                        try:
                            epic_results = future.result()
                            self.results.extend(epic_results)

                            has_failures = any(
                                r.status == StoryStatus.FAILED
                                for r in epic_results
                            )
                            if has_failures:
                                failed_epics.add(epic_num)
                            else:
                                completed_epics.add(epic_num)
                                wt_path = worktree_paths[epic_num]
                                self.merge_queue.enqueue(epic_num, wt_path)

                        except Exception as e:
                            console.print(
                                f"\n  [red]Epic {epic_num} failed: {e}[/red]"
                            )
                            failed_epics.add(epic_num)

                        # Merge completed epics immediately while other epics keep running
                        if not self.ctx.interrupted and self.merge_queue.pending_count > 0:
                            _process_merges()

                    for f in done_futures:
                        del futures[f]

                    # Submit newly-ready epics after completions (and merges)
                    _submit_ready()

                    if not futures and not pending_epics:
                        break

            # Drain any remaining events
            self.ctx.event_bus.drain()

            # Drain any stragglers from the merge queue
            if not self.ctx.interrupted and self.merge_queue.pending_count > 0:
                _process_merges()

            # Push merged results to remote in one go
            if completed_epics and not self.ctx.interrupted:
                self._push_to_remote()

        finally:
            pass  # worktrees removed immediately after each merge above

        return self.results

    def _push_to_remote(self) -> None:
        """Push the main branch to the remote after all merges complete."""
        console.print("\n  [cyan]Pushing merged results to remote...[/cyan]")
        try:
            result = subprocess.run(
                ["git", "push"],
                cwd=str(self.config.project_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                console.print("  [green]OK[/green] Pushed to remote")
            else:
                error = (result.stderr or "").strip() or f"exit code {result.returncode}"
                console.print(f"  [red]XX[/red] Push failed: {error}")
        except subprocess.TimeoutExpired:
            console.print("  [red]XX[/red] Push timed out after 120s")
        except Exception as e:
            console.print(f"  [red]XX[/red] Push failed: {e}")

    def run_sequential(self) -> list[StoryResult]:
        """Execute all stories sequentially.

        Reuses existing worktrees per-epic when found (e.g. after a restart),
        without forcing all epics through the full parallel/merge-queue path.
        """
        for epic_num in self.epic_groups:
            self.ctx.run_control.register_epic(epic_num)

        needs_push = False

        for epic_num, epic_stories in self.epic_groups.items():
            if self.ctx.interrupted:
                break

            # Use a worktree if one exists, or create one for after-epic-only epics
            wt_path = self.worktree_mgr.get_worktree_path(epic_num)
            after_epic_only = not epic_stories and not self.config.skip_retro
            if wt_path.exists():
                use_worktree = True
                console.print(
                    f"\n  [cyan]Reusing existing worktree for epic {epic_num}[/cyan]"
                )
            elif after_epic_only:
                use_worktree = True
                wt_path = self.worktree_mgr.create(epic_num)
                console.print(
                    f"\n  [cyan]Created worktree for epic {epic_num} (after-epic)[/cyan]"
                )
            else:
                use_worktree = False

            worker = EpicWorker(
                epic_num=epic_num,
                stories=epic_stories,
                story_status_map=self.story_status_map,
                config=self.config,
                ctx=self.ctx,
                worktree_path=wt_path if use_worktree else None,
                run_after_epic=not self.config.skip_retro,
            )
            epic_results = worker.run()
            self.results.extend(epic_results)

            has_failures = any(r.status == StoryStatus.FAILED for r in epic_results)

            # Merge the worktree if this epic succeeded
            if use_worktree and not has_failures and not self.ctx.interrupted:
                self.merge_queue.enqueue(epic_num, wt_path)
                merge_results = self.merge_queue.process_all()
                for mr in merge_results:
                    if not mr.success:
                        has_failures = True
                if not has_failures:
                    try:
                        self.worktree_mgr.remove(epic_num)
                    except Exception:
                        pass
                    needs_push = True

            # Stop on first failure in sequential mode
            if has_failures:
                break

        if needs_push and not self.ctx.interrupted:
            self._push_to_remote()

        return self.results

    def run(self) -> list[StoryResult]:
        """Run epics using the configured parallelism level."""
        if self.config.parallel_epics > 1 and len(self.epic_groups) > 1:
            return self.run_parallel()

        return self.run_sequential()
