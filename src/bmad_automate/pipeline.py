"""Story processing and after-epic pipeline orchestration."""

from __future__ import annotations

import time

from rich.progress import Progress, TaskID

from bmad_automate.context import RunContext
from bmad_automate.events import (
    LOG_MESSAGE,
    STEP_SKIPPED,
    STORY_DONE,
    STORY_START,
    PipelineEvent,
)
from bmad_automate.git import (
    _extract_epic_num,
    mark_story_done,
    run_after_epic_commit,
    run_git_pull,
    run_step,
)
from bmad_automate.models import (
    WORKFLOW_COURSE_CORRECT,
    WORKFLOW_CREATE,
    WORKFLOW_DEV,
    WORKFLOW_ENGINE,
    WORKFLOW_QUICK_DEV,
    WORKFLOW_RETRO,
    WORKFLOW_REVIEW,
    Config,
    StepResult,
    StepStatus,
    StoryResult,
    StoryStatus,
)
from bmad_automate.stories import get_story_path, has_next_epic
from bmad_automate.ui import console, format_duration, log_to_file

# ---------------------------------------------------------------------------
# Single-story processing
# ---------------------------------------------------------------------------

def process_story(
    story_key: str,
    config: Config,
    ctx: RunContext,
    story_status: str = "",
) -> StoryResult:
    """Process all workflow steps for a single story."""
    start_time = time.time()
    story_path = get_story_path(story_key, config)
    steps: list[StepResult] = []
    epic_num = _extract_epic_num(story_key)
    bus = ctx.event_bus

    bus.emit(PipelineEvent(
        epic=epic_num, story=story_key, step=None,
        kind=STORY_START,
    ))
    log_to_file(f"=== Starting story: {story_key} ===", config)

    ai = config.ai_command
    bmad = config.bmad_dir

    create_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_CREATE}. "
        f"Create story: {story_key}. "
        "Do not ask clarifying questions - use best judgment. "
        "Process the entire workflow automatically (YOLO mode)."
    )
    dev_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_DEV}. "
        f"Work on story file: {story_path}. "
        "Complete all tasks. Run tests after each implementation. "
        "Do not ask clarifying questions - use best judgment based on "
        "existing patterns. Continue until ALL tasks are complete (YOLO mode)."
    )
    review_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_REVIEW}. "
        f"Review story: {story_path}. "
        "When presenting options, always choose option 1 to "
        "auto-fix all issues immediately. Do not wait for user input."
    )
    commit_prompt = (
        f"Commit all changes for story {story_key} with a descriptive "
        "message. Do NOT push yet. Do not forget submodules"
    )
    merge_conflict_prompt = (
        "There are git merge conflicts after pulling from the remote. "
        "Resolve ALL merge conflicts in the working tree using best judgment, "
        "then stage the resolved files, commit the merge, and push. "
        "Do not ask clarifying questions."
    )

    # Auto-skip steps based on story status
    skip_create = config.skip_create
    skip_dev = config.skip_dev

    if story_status == "review":
        bus.emit(PipelineEvent(
            epic=epic_num, story=story_key, step="create-story",
            kind=STEP_SKIPPED,
            payload={"message": "Status is 'review', skipping create-story "
                     "and dev-story"},
        ))
        if not config.quiet and not bus.has_subscribers():
            console.print(
                "  [dim]Status is 'review', skipping create-story "
                "and dev-story[/dim]"
            )
        skip_create = True
        skip_dev = True
    elif not skip_create and story_path.exists():
        bus.emit(PipelineEvent(
            epic=epic_num, story=story_key, step="create-story",
            kind=STEP_SKIPPED,
            payload={"message": "Story file exists, skipping create-story"},
        ))
        if not config.quiet and not bus.has_subscribers():
            console.print("  [dim]Story file exists, skipping create-story[/dim]")
        skip_create = True

    step_definitions = [
        ("create-story", skip_create, f'{ai} "{create_prompt}"'),
        ("dev-story", skip_dev, f'{ai} "{dev_prompt}"'),
        ("code-review", config.skip_review, f'{ai} "{review_prompt}"'),
        ("git-commit", config.skip_commit, f'{ai} "{commit_prompt}"'),
    ]

    failed_step = ""
    for step_name, skip, command in step_definitions:
        if ctx.interrupted:
            break

        if skip:
            bus.emit(PipelineEvent(
                epic=epic_num, story=story_key, step=step_name,
                kind=STEP_SKIPPED,
            ))
            bus.drain()
            if not config.quiet and not bus.has_subscribers():
                console.print(
                    f"  [yellow]Skipping[/yellow] [magenta]{step_name}[/magenta]"
                )
            steps.append(StepResult(name=step_name, status=StepStatus.SKIPPED))
            continue

        result = run_step(step_name, command, story_key, config, ctx)
        steps.append(result)

        if result.status == StepStatus.FAILED:
            failed_step = step_name
            break

    # git-pull: direct subprocess, only invoke AI if merge conflicts arise.
    # In worktree mode the merge queue handles syncing, so skip pull/push.
    if not failed_step and not ctx.interrupted and not config.in_worktree:
        pull_result = run_git_pull(
            story_key, config, merge_conflict_prompt, ctx
        )
        steps.append(pull_result)
        if pull_result.status == StepStatus.FAILED:
            failed_step = "git-pull"

    duration = time.time() - start_time

    if any(s.status == StepStatus.FAILED for s in steps):
        status = StoryStatus.FAILED
    elif config.dry_run or all(s.status == StepStatus.SKIPPED for s in steps):
        status = StoryStatus.SKIPPED
    else:
        status = StoryStatus.COMPLETED
        mark_story_done(story_key, config)

    log_to_file(
        f"=== Story {story_key}: {status.value} "
        f"({format_duration(duration)}) ===",
        config,
    )

    bus.emit(PipelineEvent(
        epic=epic_num, story=story_key, step=None,
        kind=STORY_DONE,
        payload={"status": status.value, "duration": duration},
    ))
    bus.drain()

    return StoryResult(
        key=story_key,
        status=status,
        steps=steps,
        duration=duration,
        failed_step=failed_step,
    )


# ---------------------------------------------------------------------------
# After-epic step runners
# ---------------------------------------------------------------------------

def run_retrospective(
    epic_num: int, config: Config, ctx: RunContext,
) -> StepResult:
    """Run the BMAD retrospective workflow for a completed epic."""
    ai = config.ai_command
    bmad = config.bmad_dir

    retro_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_RETRO}. "
        f"Run the retrospective for Epic {epic_num}. "
        "Do not ask clarifying questions - use best judgment. "
        "Process the entire workflow automatically (YOLO mode)."
    )

    command = f'{ai} "{retro_prompt}"'
    step_name = f"retro-epic-{epic_num}"

    if config.dry_run:
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta] (Epic {epic_num})"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    return run_step(step_name, command, f"epic-{epic_num}", config, ctx)


def run_course_correction(
    epic_num: int, config: Config, ctx: RunContext,
) -> StepResult:
    """Run the scrum-master course-correction workflow for a completed epic."""
    ai = config.ai_command
    bmad = config.bmad_dir

    cc_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_COURSE_CORRECT}. "
        f"Evaluate whether a course correction is needed after Epic {epic_num}. "
        "Review the retrospective output and current sprint status. "
        "If a course correction is needed, execute it. "
        "If no correction is needed, document that decision. "
        "Do not ask clarifying questions - use best judgment. "
        "Process the entire workflow automatically (YOLO mode)."
    )

    command = f'{ai} "{cc_prompt}"'
    step_name = f"course-correct-epic-{epic_num}"

    if config.dry_run:
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta] (Epic {epic_num})"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    return run_step(step_name, command, f"epic-{epic_num}", config, ctx)


def run_retro_implementation(
    epic_num: int, config: Config, ctx: RunContext,
) -> StepResult:
    """Run a quick dev pass to implement learnings from the retrospective."""
    ai = config.ai_command
    bmad = config.bmad_dir

    impl_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Load and execute the workflow at {bmad}/{WORKFLOW_QUICK_DEV}. "
        f"Implement the learnings from the Epic {epic_num} retrospective. "
        "Review the retrospective output and apply all improvements "
        "to the codebase — refactoring, tooling, test coverage, documentation, "
        "or process improvements. Skip anything that is not directly actionable "
        "in the code or documentation. "
        "Do not ask clarifying questions - use best judgment. "
        "Process the entire workflow automatically (YOLO mode)."
    )

    command = f'{ai} "{impl_prompt}"'
    step_name = f"retro-impl-epic-{epic_num}"

    if config.dry_run:
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta] (Epic {epic_num})"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    return run_step(step_name, command, f"epic-{epic_num}", config, ctx)


def run_next_epic_preparation(
    previous_epic_num: int, config: Config, ctx: RunContext,
) -> StepResult:
    """Run preparation tasks for the next epic after a completed retro cycle."""
    ai = config.ai_command
    bmad = config.bmad_dir
    next_epic = previous_epic_num + 1

    prep_prompt = (
        f"Read and follow the BMAD workflow engine at {bmad}/{WORKFLOW_ENGINE}. "
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_QUICK_DEV}. "
        f"Run all prep tasks for epic {next_epic} based on retrospective "
        f"of {previous_epic_num}. "
        "Do not ask clarifying questions - use best judgment. "
        "Process the entire workflow automatically (YOLO mode)."
    )

    command = f'{ai} "{prep_prompt}"'
    step_name = f"prep-next-epic-{next_epic}"

    if config.dry_run:
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta] (Epic {next_epic})"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    return run_step(step_name, command, f"epic-{next_epic}", config, ctx)


# ---------------------------------------------------------------------------
# Full after-epic pipeline
# ---------------------------------------------------------------------------

def _print_step_result(label: str, result: StepResult) -> None:
    """Print a single after-epic step result line."""
    if result.status == StepStatus.SUCCESS:
        console.print(
            f"  [green]OK[/green] {label}"
            f"  [dim]{format_duration(result.duration)}[/dim]"
        )
    elif result.status == StepStatus.FAILED:
        console.print(
            f"  [red]XX[/red] {label}"
            f"  [dim]{result.error}[/dim]"
        )


def run_after_epic_pipeline(
    epic_num: int,
    config: Config,
    ctx: RunContext,
    retro_results: list[StepResult],
    *,
    require_retro_success: bool = False,
    progress: Progress | None = None,
    progress_task: TaskID | None = None,
) -> None:
    """Run the full after-epic pipeline for a single epic.

    Steps: retrospective -> course-correction -> retro-implementation
    -> next-epic-preparation.
    """
    retro_ok = True

    # 1. Retrospective
    if not ctx.interrupted and not config.skip_retro:
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"[cyan]Epic {epic_num}: retrospective",
            )
        retro_result = run_retrospective(epic_num, config, ctx)
        retro_results.append(retro_result)
        retro_ok = retro_result.status == StepStatus.SUCCESS
        _print_step_result(f"retro-epic-{epic_num}", retro_result)

    # 2. Course correction
    if (
        not ctx.interrupted
        and not config.skip_course_correct
        and (not require_retro_success or retro_ok)
    ):
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"[cyan]Epic {epic_num}: course correction",
            )
        console.print(
            f"\n  [cyan]Running scrum-master course "
            f"correction for epic {epic_num}...[/cyan]"
        )
        log_to_file(f"Running course correction for epic {epic_num}", config)
        cc_result = run_course_correction(epic_num, config, ctx)
        retro_results.append(cc_result)
        _print_step_result(f"course-correct-epic-{epic_num}", cc_result)

    # 3. Retro implementation
    if (
        not ctx.interrupted
        and not config.skip_retro_impl
        and (not require_retro_success or retro_ok)
    ):
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"[cyan]Epic {epic_num}: retro implementation",
            )
        console.print(
            f"\n  [cyan]Implementing retro learnings "
            f"for epic {epic_num}...[/cyan]"
        )
        log_to_file(f"Implementing retro learnings for epic {epic_num}", config)
        impl_result = run_retro_implementation(epic_num, config, ctx)
        retro_results.append(impl_result)
        _print_step_result(f"retro-impl-epic-{epic_num}", impl_result)

    # 4. Next epic preparation (only if next epic exists)
    if (
        not ctx.interrupted
        and not config.skip_next_epic_prep
        and (not require_retro_success or retro_ok)
        and has_next_epic(epic_num, config)
    ):
        next_epic = epic_num + 1
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"[cyan]Epic {next_epic}: preparation",
            )
        console.print(
            f"\n  [cyan]Preparing next epic "
            f"{next_epic} (based on epic {epic_num})...[/cyan]"
        )
        log_to_file(
            f"Preparing next epic {next_epic} after epic {epic_num}", config
        )
        prep_result = run_next_epic_preparation(epic_num, config, ctx)
        retro_results.append(prep_result)
        _print_step_result(f"prep-next-epic-{next_epic}", prep_result)

    # 5. Commit and push any changes from the after-epic pipeline
    if not ctx.interrupted and not config.dry_run:
        commit_result = run_after_epic_commit(epic_num, config)
        retro_results.append(commit_result)
        _print_step_result(f"after-epic-commit-{epic_num}", commit_result)
