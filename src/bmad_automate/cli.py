"""
BMAD Workflow Automation CLI.

Automates the BMAD (Business Method for Agile Development) workflow cycle
for stories defined in sprint-status.yaml. For each actionable story, the
script orchestrates a full development cycle using an AI CLI provider
(Claude by default, or GitHub Copilot):

    create-story -> dev-story -> code-review -> git-commit -> git-pull

Features:
    - Beautiful terminal UX with progress bars and colored output (via Rich)
    - Proper YAML parsing for sprint-status.yaml (no fragile grep/sed)
    - Robust subprocess handling with configurable timeouts and retries
    - Dry-run mode for previewing what would be executed
    - Flexible story selection (by status, limit, specific keys, or resume point)
    - Step-level control (skip any combination of steps, or --only to include)
    - Graceful Ctrl+C handling with partial summary
    - Comprehensive logging to file for debugging
    - Desktop notifications on completion (on by default, --no-notify to disable)

Requirements:
    - Python 3.11+
    - Claude CLI installed (default), or GitHub Copilot CLI (--ai-provider github)
    - A BMAD project with _bmad/ workflow files

Usage:
    bmad-automate [options] [story_keys...]

See --help for full options or the README for comprehensive documentation.
"""

import atexit
import signal
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from bmad_automate.context import RunContext, set_active_context
from bmad_automate.control import get_active_control, set_active_control
from bmad_automate.models import (
    AI_PROVIDERS,
    ALL_STEPS,
    DEFAULT_AI_PROVIDER,
    DEFAULT_BMAD_DIR,
    DEFAULT_LOG_FILE,
    DEFAULT_RETRIES,
    DEFAULT_SPRINT_STATUS,
    DEFAULT_STORY_DIR,
    DEFAULT_TIMEOUT,
    Config,
    StepResult,
    StoryStatus,
)
from bmad_automate.orchestrator import Orchestrator
from bmad_automate.pipeline import process_story, run_after_epic_pipeline
from bmad_automate.stories import (
    filter_stories,
    get_actionable_stories,
    get_epics_needing_retro,
    is_epic_complete,
    parse_epic_list,
)
from bmad_automate.ui import (
    confirm_start,
    console,
    format_duration,
    log_to_file,
    print_dependency_graph,
    print_dry_run_preview,
    print_final_summary,
    print_story_summary,
    restore_terminal_title,
    send_notification,
    set_running_title,
)

# Typer app instance
app = typer.Typer(
    name="bmad-automate",
    help="Automated BMAD Workflow Orchestrator",
    add_completion=False,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    """Handle interrupt signals (Ctrl+C, SIGTERM) gracefully."""
    ctrl = get_active_control()
    if ctrl is not None:
        ctrl.abort()
    else:
        # Fallback for legacy code paths
        from bmad_automate.context import get_active_context

        ctx = get_active_context()
        if ctx is not None:
            ctx.interrupted = True
    console.print(
        "\n[yellow]Interrupt received. Finishing current operation...[/yellow]"
    )


def setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


# ---------------------------------------------------------------------------
# --only parsing helper
# ---------------------------------------------------------------------------

def _parse_only(value: str) -> dict[str, bool]:
    """Parse ``--only create,review`` into a skip-flags dict.

    Returns a dict with keys ``skip_create``, ``skip_dev``, etc.  Steps
    *not* listed in *value* are set to ``True`` (skip).
    """
    requested = {s.strip() for s in value.split(",") if s.strip()}
    unknown = requested - set(ALL_STEPS)
    if unknown:
        console.print(
            f"[red]Error: Unknown step(s) in --only: "
            f"{', '.join(sorted(unknown))}[/red]\n"
            f"[dim]Valid steps: {', '.join(ALL_STEPS)}[/dim]"
        )
        raise typer.Exit(2)

    return {
        "skip_create": "create" not in requested,
        "skip_dev": "dev" not in requested,
        "skip_review": "review" not in requested,
        "skip_commit": "commit" not in requested,
        "skip_pull": "pull" not in requested,
    }


# ---------------------------------------------------------------------------
# Dependency graph helper
# ---------------------------------------------------------------------------

def _show_dependency_graph(
    stories: list[str], config: Config,
) -> None:
    """Build and display the dependency graph for the epics in *stories*."""
    import re

    import yaml

    from bmad_automate.dependencies import build_dag

    # Extract unique epic numbers from stories
    epic_nums = sorted({
        int(m.group(1))
        for s in stories
        if (m := re.match(r"^(\d+)-", s))
    })
    if len(epic_nums) < 2:
        return  # no point showing a graph for a single epic

    # Count stories per epic
    story_counts: dict[int, int] = {}
    for s in stories:
        m = re.match(r"^(\d+)-", s)
        if m:
            epic = int(m.group(1))
            story_counts[epic] = story_counts.get(epic, 0) + 1

    # Load YAML for dependency parsing
    yaml_text = ""
    yaml_data: dict = {}
    if config.sprint_status.exists():
        with open(config.sprint_status, encoding="utf-8") as f:
            yaml_text = f.read()
        yaml_data = yaml.safe_load(yaml_text) or {}

    dag = build_dag(yaml_data, yaml_text, epic_nums)
    print_dependency_graph(dag, story_counts=story_counts)


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

@app.callback(invoke_without_command=True)
def main(
    ctx_typer: typer.Context,
    # Positional arguments
    stories: Annotated[
        Optional[list[str]],
        typer.Argument(help="Specific story keys to process"),
    ] = None,
    # General options
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Preview what would run without executing"
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip interactive confirmation prompt"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", "-v",
            help="Enable verbose output (show full command output)",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet", "-q", help="Minimal output (only errors and summary)"
        ),
    ] = False,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify/--no-notify",
            help="Send a desktop notification when the run finishes",
        ),
    ] = True,
    # Story selection
    epic: Annotated[
        str,
        typer.Option(
            help=(
                "Only process stories for these epic numbers, comma-separated "
                "(e.g., --epic 3 or --epic 3,4,5)"
            ),
        ),
    ] = "",
    limit: Annotated[
        int,
        typer.Option(help="Process at most N stories (0 = unlimited)"),
    ] = 0,
    start_from: Annotated[
        str,
        typer.Option(
            help="Resume from specific story key (skip earlier stories)"
        ),
    ] = "",
    # Step control — skip individual steps
    skip_create: Annotated[
        bool,
        typer.Option("--skip-create", help="Skip create-story step"),
    ] = False,
    skip_dev: Annotated[
        bool,
        typer.Option("--skip-dev", help="Skip dev-story step"),
    ] = False,
    skip_review: Annotated[
        bool,
        typer.Option("--skip-review", help="Skip code-review step"),
    ] = False,
    skip_commit: Annotated[
        bool,
        typer.Option("--skip-commit", help="Skip git commit/push step"),
    ] = False,
    skip_pull: Annotated[
        bool,
        typer.Option(
            "--skip-pull", help="Skip git pull/merge step after commit"
        ),
    ] = False,
    # Step control — include only specific steps
    only: Annotated[
        str,
        typer.Option(
            "--only",
            help=(
                "Run ONLY these steps (comma-separated). "
                f"Valid: {', '.join(ALL_STEPS)}. "
                "Example: --only review,commit"
            ),
        ),
    ] = "",
    # After-epic step control
    skip_retro: Annotated[
        bool,
        typer.Option(
            "--skip-retro",
            help="Skip automatic retrospective after completing an epic",
        ),
    ] = False,
    skip_course_correct: Annotated[
        bool,
        typer.Option(
            "--skip-course-correct",
            help="Skip scrum-master course correction after epic retrospective",
        ),
    ] = False,
    skip_retro_impl: Annotated[
        bool,
        typer.Option(
            "--skip-retro-impl",
            help=(
                "Skip implementing retrospective learnings after "
                "course correction"
            ),
        ),
    ] = False,
    skip_next_epic_prep: Annotated[
        bool,
        typer.Option(
            "--skip-next-epic-prep",
            help=(
                "Skip preparation tasks for the next epic after "
                "retro implementation"
            ),
        ),
    ] = False,
    after_epic: Annotated[
        str,
        typer.Option(
            "--after-epic",
            help=(
                "Explicitly run all after-epic steps (retro, course-correct, "
                "retro-impl, next-epic-prep) for these epic numbers, "
                "comma-separated (e.g., --after-epic 3 or --after-epic 3,4)"
            ),
        ),
    ] = "",
    # Retry/Timeout
    retries: Annotated[
        int,
        typer.Option(
            help=f"Retries per step (default: {DEFAULT_RETRIES})"
        ),
    ] = DEFAULT_RETRIES,
    timeout: Annotated[
        int,
        typer.Option(
            help=f"Timeout per step in seconds (default: {DEFAULT_TIMEOUT})"
        ),
    ] = DEFAULT_TIMEOUT,
    # Paths
    sprint_status: Annotated[
        Path,
        typer.Option(help="Path to sprint-status.yaml"),
    ] = Path(DEFAULT_SPRINT_STATUS),
    story_dir: Annotated[
        Path,
        typer.Option(help="Path to story files directory"),
    ] = Path(DEFAULT_STORY_DIR),
    log_file: Annotated[
        Path,
        typer.Option(help="Path to log file"),
    ] = Path(DEFAULT_LOG_FILE),
    # BMAD directory
    bmad_dir: Annotated[
        Path,
        typer.Option(
            "--bmad-dir",
            help=(
                "Path to the _bmad directory containing workflow files "
                f"(default: {DEFAULT_BMAD_DIR})"
            ),
        ),
    ] = Path(DEFAULT_BMAD_DIR),
    # AI provider
    ai_provider: Annotated[
        str,
        typer.Option(
            "--ai-provider",
            help=(
                "AI provider to use: 'claude' (Claude CLI) or 'github' "
                f"(GitHub Copilot CLI). Default: {DEFAULT_AI_PROVIDER}"
            ),
        ),
    ] = DEFAULT_AI_PROVIDER,
    # Parallelisation
    parallel_epics: Annotated[
        int,
        typer.Option(
            "--parallel-epics",
            help=(
                "Process up to N epics concurrently in separate git "
                "worktrees (default: 1 = sequential)"
            ),
        ),
    ] = 1,
) -> None:
    """
    Automated BMAD Workflow Orchestrator.

    Process stories through the BMAD workflow cycle:
    create-story -> dev-story -> code-review -> git-commit -> git-pull

    Uses Claude CLI (default) or GitHub Copilot CLI to execute each step
    autonomously. Prompts reference the BMAD workflow files in the project's
    _bmad/ directory.

    Examples:

        # Dry run to see what would be processed
        bmad-automate --dry-run

        # Process next 3 stories
        bmad-automate --limit 3

        # Process all stories in epic 3
        bmad-automate --epic 3

        # Process stories in epics 3 and 4
        bmad-automate --epic 3,4

        # Run after-epic steps (retro, course-correct, etc.) for epic 3
        bmad-automate --after-epic 3

        # Process single story
        bmad-automate 3-3-account-translation

        # Non-interactive with verbose output
        bmad-automate --yes --verbose --limit 5

        # Run only code-review and commit for all stories
        bmad-automate --only review,commit

        # Disable desktop notification
        bmad-automate --no-notify --limit 5

        # Custom BMAD directory
        bmad-automate --bmad-dir path/to/_bmad

        # Use GitHub Copilot instead of Claude
        bmad-automate --ai-provider github
    """
    # If a subcommand (e.g. serve) was invoked, skip the default handler.
    if ctx_typer.invoked_subcommand is not None:
        return

    # Typer's invoke_without_command=True with a variadic positional argument
    # can swallow subcommand names (e.g. "serve") as story keys instead of
    # routing them.  Detect this and re-invoke the correct subcommand.
    if stories:
        click_group = ctx_typer.command
        if hasattr(click_group, "commands"):
            for story_arg in stories:
                if story_arg in click_group.commands:
                    ctx_typer.invoke(click_group.commands[story_arg])
                    return

    # ---- resolve --only vs --skip-* flags ----
    if only:
        if any([skip_create, skip_dev, skip_review, skip_commit, skip_pull]):
            console.print(
                "[red]Error: --only cannot be combined with --skip-* flags[/red]"
            )
            raise typer.Exit(2)
        only_flags = _parse_only(only)
        skip_create = only_flags["skip_create"]
        skip_dev = only_flags["skip_dev"]
        skip_review = only_flags["skip_review"]
        skip_commit = only_flags["skip_commit"]
        skip_pull = only_flags["skip_pull"]

    # ---- build config ----
    epic_list = parse_epic_list(epic)
    after_epic_list = parse_epic_list(after_epic)

    config = Config(
        sprint_status=sprint_status,
        story_dir=story_dir,
        log_file=log_file,
        dry_run=dry_run,
        yes=yes,
        verbose=verbose,
        quiet=quiet,
        notify=notify,
        limit=limit,
        start_from=start_from,
        specific_stories=stories or [],
        epic=epic_list,
        after_epic=after_epic_list,
        skip_create=skip_create,
        skip_dev=skip_dev,
        skip_review=skip_review,
        skip_commit=skip_commit,
        skip_pull=skip_pull,
        skip_retro=skip_retro,
        skip_course_correct=skip_course_correct,
        skip_retro_impl=skip_retro_impl,
        skip_next_epic_prep=skip_next_epic_prep,
        retries=retries,
        timeout=timeout,
        bmad_dir=bmad_dir,
        ai_provider=ai_provider,
        parallel_epics=parallel_epics,
    )

    # ---- validate ----
    if config.ai_provider not in AI_PROVIDERS:
        console.print(
            f"[red]Error: Unknown AI provider '{config.ai_provider}'[/red]\n"
            f"[dim]Available providers: {', '.join(AI_PROVIDERS)}[/dim]"
        )
        raise typer.Exit(2)

    if not config.bmad_dir.exists():
        console.print(
            f"[red]Error: BMAD directory not found: {config.bmad_dir}[/red]\n"
            "[dim]Make sure you're running from a BMAD project root, "
            "or use --bmad-dir to specify the path.[/dim]"
        )
        raise typer.Exit(2)

    # ---- run context & signal handlers ----
    ctx = RunContext(config=config)
    set_active_context(ctx)
    set_active_control(ctx.run_control)
    setup_signal_handlers()

    # ---- get and filter stories ----
    stories_by_status = get_actionable_stories(config)
    total_actionable = sum(len(v) for v in stories_by_status.values())

    pending_retro_epics: list[int] = []
    if not config.skip_retro:
        pending_retro_epics = get_epics_needing_retro(config)
        if config.epic:
            pending_retro_epics = [
                e for e in pending_retro_epics if e in config.epic
            ]

    after_epic_epics: list[int] = list(config.after_epic)
    if config.epic:
        for e in config.epic:
            if (
                e not in after_epic_epics
                and e not in pending_retro_epics
                and is_epic_complete(e, config)
            ):
                after_epic_epics.append(e)
        after_epic_epics = sorted(set(after_epic_epics))

    if (
        not total_actionable
        and not config.specific_stories
        and not pending_retro_epics
        and not after_epic_epics
    ):
        console.print(
            "[yellow]No actionable stories or pending retrospectives found "
            "in sprint-status.yaml[/yellow]"
        )
        raise typer.Exit(0)

    story_status_map: dict[str, str] = {}
    for status, keys in stories_by_status.items():
        for key in keys:
            story_status_map[key] = status

    filtered_stories = filter_stories(stories_by_status, config)

    if not filtered_stories and not pending_retro_epics and not after_epic_epics:
        console.print(
            "[yellow]No stories to process after applying filters "
            "and no pending retrospectives[/yellow]"
        )
        raise typer.Exit(0)

    # ---- dry run ----
    if config.dry_run:
        if after_epic_epics:
            console.print(
                "[bold]After-epic pipeline for "
                f"epic(s) {','.join(str(e) for e in after_epic_epics)}:[/bold]"
            )
            _dry: list[StepResult] = []
            for epic_num in after_epic_epics:
                run_after_epic_pipeline(epic_num, config, ctx, _dry)
            console.print()

        if pending_retro_epics:
            auto_retro = [
                e for e in pending_retro_epics if e not in after_epic_epics
            ]
            if auto_retro:
                console.print(
                    "[bold]Pending retrospective pipeline "
                    "(will run before stories):[/bold]"
                )
                _dry2: list[StepResult] = []
                for epic_num in auto_retro:
                    run_after_epic_pipeline(
                        epic_num, config, ctx, _dry2,
                        require_retro_success=True,
                    )
                console.print()

        if filtered_stories:
            # Show dependency graph if multiple epics
            _show_dependency_graph(filtered_stories, config)

            print_dry_run_preview(filtered_stories, config)
            console.print("[bold]Story steps that would run:[/bold]")
            for story in filtered_stories:
                if ctx.interrupted:
                    break
                result = process_story(
                    story, config, ctx, story_status_map.get(story, "")
                )
                ctx.results.append(result)
        else:
            console.print("[dim]No actionable stories to process.[/dim]")
        raise typer.Exit(0)

    # ---- confirmation ----
    if not config.yes and not confirm_start(filtered_stories, config):
        raise typer.Exit(0)

    # ---- terminal title ----
    set_running_title()
    atexit.register(restore_terminal_title, success=False)

    # ---- initialise log ----
    log_to_file("=" * 50, config)
    log_to_file("BMAD Automation Started", config)
    log_to_file(f"Stories to process: {len(filtered_stories)}", config)
    log_to_file("=" * 50, config)

    ctx.start_time = time.time()

    retro_results: list[StepResult] = []
    retro_done_epics: set[int] = set()

    # ---- explicit --after-epic pipeline ----
    if after_epic_epics and not ctx.interrupted:
        for epic_num in after_epic_epics:
            if ctx.interrupted:
                break
            retro_done_epics.add(epic_num)
            console.print(
                f"\n  [cyan]Running after-epic pipeline for "
                f"epic {epic_num}...[/cyan]"
            )
            log_to_file(
                f"Running after-epic pipeline for epic {epic_num}", config
            )
            run_after_epic_pipeline(epic_num, config, ctx, retro_results)

    # ---- pending retrospectives ----
    if pending_retro_epics and not ctx.interrupted:
        for epic_num in pending_retro_epics:
            if ctx.interrupted:
                break
            if epic_num in retro_done_epics:
                continue
            retro_done_epics.add(epic_num)
            console.print(
                f"\n  [cyan]Epic {epic_num} already complete — "
                f"running after-epic pipeline...[/cyan]"
            )
            log_to_file(
                f"Running pending after-epic pipeline for epic {epic_num}",
                config,
            )
            run_after_epic_pipeline(
                epic_num, config, ctx, retro_results,
                require_retro_success=True,
            )

    # ---- process stories ----
    if filtered_stories:
        if config.parallel_epics > 1:
            # Parallel mode — delegate to Orchestrator
            orch = Orchestrator(
                stories=filtered_stories,
                story_status_map=story_status_map,
                config=config,
                ctx=ctx,
            )
            orch_results = orch.run()
            ctx.results.extend(orch_results)
            for result in orch_results:
                print_story_summary(result, config)
        else:
            # Sequential mode — original behavior
            console.print()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            ) as progress:
                task = progress.add_task(
                    "[cyan]Processing stories...", total=len(filtered_stories)
                )

                for i, story in enumerate(filtered_stories):
                    if ctx.interrupted:
                        console.print("\n[yellow]Interrupted by user[/yellow]")
                        break

                    progress.update(
                        task,
                        description=(
                            f"[cyan]Story {i + 1}/{len(filtered_stories)}: "
                            f"{story}"
                        ),
                    )

                    result = process_story(
                        story, config, ctx, story_status_map.get(story, "")
                    )
                    ctx.results.append(result)

                    print_story_summary(result, config)
                    progress.advance(task)

                    if result.status == StoryStatus.FAILED:
                        console.print(
                            f"\n[red]Story {story} failed, stopping "
                            f"automation[/red]"
                        )
                        break

                    # Check if this story's epic now needs a retro
                    if (
                        not config.skip_retro
                        and not ctx.interrupted
                        and result.status == StoryStatus.COMPLETED
                    ):
                        story_epic = int(story.split("-")[0])
                        epics = [
                            e
                            for e in get_epics_needing_retro(config)
                            if e == story_epic
                        ]
                        for epic_num in epics:
                            if epic_num not in retro_done_epics:
                                retro_done_epics.add(epic_num)
                                console.print(
                                    f"\n  [cyan]Epic {epic_num} complete — "
                                    f"running after-epic pipeline...[/cyan]"
                                )
                                log_to_file(
                                    f"Running after-epic pipeline for "
                                    f"epic {epic_num}",
                                    config,
                                )
                                run_after_epic_pipeline(
                                    epic_num,
                                    config,
                                    ctx,
                                    retro_results,
                                    require_retro_success=True,
                                    progress=progress,
                                    progress_task=task,
                                )

    # ---- final summary ----
    total_duration = time.time() - ctx.start_time
    print_final_summary(ctx.results, config, total_duration, retro_results)

    log_to_file("=" * 50, config)
    log_to_file("BMAD Automation Finished", config)
    log_to_file(f"Duration: {format_duration(total_duration)}", config)
    log_to_file("=" * 50, config)

    has_failures = any(r.status == StoryStatus.FAILED for r in ctx.results)
    atexit.unregister(restore_terminal_title)
    restore_terminal_title(success=not has_failures and not ctx.interrupted)

    # ---- notification ----
    if config.notify:
        completed = sum(
            1 for r in ctx.results if r.status == StoryStatus.COMPLETED
        )
        failed = sum(
            1 for r in ctx.results if r.status == StoryStatus.FAILED
        )
        if has_failures:
            send_notification(
                "bmad-automate finished with failures",
                f"{completed} completed, {failed} failed "
                f"in {format_duration(total_duration)}",
            )
        elif ctx.interrupted:
            send_notification(
                "bmad-automate interrupted",
                f"{completed} completed before interruption "
                f"({format_duration(total_duration)})",
            )
        else:
            send_notification(
                "bmad-automate complete",
                f"All {completed} stories completed "
                f"in {format_duration(total_duration)}",
            )

    # ---- exit code ----
    if has_failures:
        raise typer.Exit(1)
    elif ctx.interrupted:
        raise typer.Exit(130)


@app.command("serve")
def serve(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to listen on"),
    ] = 8080,
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            help="Project directory (default: current directory)",
        ),
    ] = Path("."),
) -> None:
    """Launch the web dashboard server."""
    import uvicorn

    from bmad_automate.web.app import create_app
    from bmad_automate.web.lock import ServerLock

    lock = ServerLock(project_dir)
    existing = lock.is_server_running()
    if existing:
        console.print(
            f"[yellow]Server already running on port {existing.port} "
            f"(PID {existing.pid})[/yellow]"
        )
        raise typer.Exit(1)

    if not lock.acquire(port):
        console.print("[red]Error: Could not acquire server lock[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Starting BMAD Dashboard on http://localhost:{port}[/green]"
    )

    fastapi_app = create_app(project_dir=project_dir.resolve())

    # Open browser after server starts listening — use a timer thread so it
    # fires reliably regardless of FastAPI lifecycle quirks.
    import threading
    import webbrowser

    def _open_browser() -> None:
        webbrowser.open(f"http://localhost:{port}")

    @fastapi_app.on_event("startup")
    async def _schedule_browser_open() -> None:
        threading.Timer(0.5, _open_browser).start()

    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    app()
