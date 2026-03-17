"""Terminal UI helpers — output, logging, summaries, and notifications."""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bmad_automate.models import (
    ALL_STEPS,
    Config,
    StepResult,
    StepStatus,
    StoryResult,
    StoryStatus,
)

# Thread-safe Rich console wrapper.
_console = Console()
_console_lock = threading.Lock()


class _ThreadSafeConsole:
    """Wraps Rich Console with a lock so prints from worker threads
    don't interleave at the character level."""

    def __getattr__(self, name: str):
        attr = getattr(_console, name)
        if not callable(attr):
            return attr

        def _locked(*args, **kwargs):
            with _console_lock:
                return attr(*args, **kwargs)

        return _locked


console = _ThreadSafeConsole()  # type: ignore[assignment]

# Lock for file logging.
_log_file_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Terminal title helpers
# ---------------------------------------------------------------------------

def set_terminal_title(title: str) -> None:
    """Set the terminal window title via an OSC escape sequence."""
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()


def set_running_title() -> None:
    """Replace the terminal title with a running indicator."""
    set_terminal_title("\u23f3 bmad-automate \u2014 running\u2026")


def restore_terminal_title(success: bool = True) -> None:
    """Set the terminal title to a finished/failed indicator."""
    if success:
        set_terminal_title("\u2705 bmad-automate \u2014 finished")
    else:
        set_terminal_title("\u274c bmad-automate \u2014 failed")


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string.

    Examples:
        >>> format_duration(45)
        '45s'
        >>> format_duration(201)
        '3m 21s'
    """
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining:02d}s"


# ---------------------------------------------------------------------------
# File logging
# ---------------------------------------------------------------------------

def log_to_file(message: str, config: Config) -> None:
    """Append a timestamped message to the log file.  Thread-safe."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_file_lock:
        with open(config.log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")


# ---------------------------------------------------------------------------
# Dependency graph visualisation (CLI)
# ---------------------------------------------------------------------------

def print_dependency_graph(
    dag: object,
    story_counts: dict[int, int] | None = None,
) -> None:
    """Print a visual dependency graph to the terminal.

    Shows dependency chains so users can identify the critical path
    and see which epics gate which.  Story counts (if provided)
    highlight where bottlenecks are.
    """
    from bmad_automate.dependencies import DAG

    if not isinstance(dag, DAG) or not dag.has_dependencies():
        return

    chains = dag.get_chains()
    counts = story_counts or {}

    console.print()
    console.print(Panel("[bold]Epic Dependency Chains[/bold]", style="blue"))
    console.print()

    # Find the critical path
    critical = dag.get_critical_path(counts) if counts else None

    for i, chain in enumerate(chains):
        parts: list[str] = []
        total_stories = 0
        for epic in chain:
            n = counts.get(epic, 0)
            total_stories += n
            label = f"Epic {epic}"
            if n > 0:
                label += f" ({n})"
            parts.append(f"[cyan]{label}[/cyan]")

        chain_str = " -> ".join(parts)
        is_critical = critical is not None and chain == critical
        marker = " [red bold]<< critical path[/red bold]" if is_critical else ""
        stories_note = f"  [dim]({total_stories} stories total)[/dim]" if total_stories else ""

        console.print(f"  Chain {i + 1}: {chain_str}{stories_note}{marker}")

    # Show convergence points (epics with multiple dependencies)
    console.print()
    for epic in sorted(dag._epics):
        deps = dag.get_dependencies(epic)
        if len(deps) > 1:
            dep_str = " and ".join(f"[cyan]Epic {d}[/cyan]" for d in deps)
            console.print(
                f"  [yellow]Epic {epic}[/yellow] waits for "
                f"{dep_str} — gated on the slowest chain"
            )

    console.print()


# ---------------------------------------------------------------------------
# Enabled-steps helper (used by dry-run preview & confirmation)
# ---------------------------------------------------------------------------

def get_enabled_steps(config: Config) -> list[str]:
    """Return the list of step display-names that are not skipped."""
    skip_map = {
        "create": config.skip_create,
        "dev": config.skip_dev,
        "review": config.skip_review,
        "commit": config.skip_commit,
        "pull": config.skip_pull,
    }
    # Map short names to display names used in output.
    display = {
        "create": "create-story",
        "dev": "dev-story",
        "review": "code-review",
        "commit": "git-commit",
        "pull": "git-pull",
    }
    return [display[s] for s in ALL_STEPS if not skip_map[s]]


# ---------------------------------------------------------------------------
# Story / run summaries
# ---------------------------------------------------------------------------

def print_story_summary(result: StoryResult, config: Config) -> None:
    """Print a formatted summary of a completed story to the console."""
    if config.quiet:
        return

    if result.status == StoryStatus.COMPLETED:
        status_text = "[green]COMPLETED[/green]"
        symbol = "[green]OK[/green]"
    elif result.status == StoryStatus.FAILED:
        status_text = f"[red]FAILED[/red] ({result.failed_step})"
        symbol = "[red]XX[/red]"
    else:
        status_text = "[yellow]SKIPPED[/yellow]"
        symbol = "[yellow]--[/yellow]"

    duration_str = format_duration(result.duration)
    console.print(
        f"\n  {symbol} [cyan]{result.key}[/cyan] | {duration_str} | {status_text}"
    )

    for step in result.steps:
        if step.status == StepStatus.SUCCESS:
            step_symbol = "[green]OK[/green]"
            dur = f"[dim]{format_duration(step.duration)}[/dim]"
        elif step.status == StepStatus.FAILED:
            step_symbol = "[red]XX[/red]"
            dur = f"[dim]{format_duration(step.duration)}[/dim]"
        else:
            step_symbol = "[yellow]--[/yellow]"
            dur = "[dim]skipped[/dim]"

        console.print(f"     {step_symbol} {step.name:<15} {dur}")


def print_dry_run_preview(stories: list[str], config: Config) -> None:
    """Print a detailed preview of what would be executed in dry-run mode."""
    console.print(
        Panel(
            "[bold cyan]DRY RUN MODE[/bold cyan] - No changes will be made",
            style="cyan",
        )
    )
    console.print()

    table = Table(title="Configuration", show_header=False, box=None)
    table.add_column("Setting", style="dim")
    table.add_column("Value")

    table.add_row("Sprint Status", str(config.sprint_status))
    table.add_row("Story Directory", str(config.story_dir))
    table.add_row("Stories to Process", str(len(stories)))
    table.add_row("Retries", str(config.retries))
    table.add_row("Timeout", f"{config.timeout}s")
    table.add_row("Steps", " -> ".join(get_enabled_steps(config)))

    console.print(table)
    console.print()

    console.print("[bold]Stories to process:[/bold]")
    for i, story in enumerate(stories, 1):
        console.print(f"  {i}. [cyan]{story}[/cyan]")
    console.print()


def confirm_start(stories: list[str], config: Config) -> bool:
    """Display a preview and prompt user for confirmation before starting."""
    console.print()
    console.print(Panel("[bold]BMAD Automation Preview[/bold]", style="blue"))
    console.print()

    console.print(f"  Stories to process: [bold]{len(stories)}[/bold]")
    console.print(f"  Steps: {' -> '.join(get_enabled_steps(config))}")
    console.print(f"  Log file: {config.log_file}")
    console.print()

    console.print("  [bold]Stories:[/bold]")
    for story in stories:
        console.print(f"    [dim]-[/dim] [cyan]{story}[/cyan]")
    console.print()

    try:
        response = console.input("[yellow]Proceed? [Y/n]:[/yellow] ")
        if response.lower() in ("n", "no"):
            console.print("[dim]Aborted.[/dim]")
            return False
        return True
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Aborted.[/dim]")
        return False


def print_final_summary(
    results: list[StoryResult],
    config: Config,
    total_duration: float,
    retro_results: list[StepResult] | None = None,
) -> None:
    """Print the final summary report after all stories have been processed."""
    console.print()

    completed = sum(1 for r in results if r.status == StoryStatus.COMPLETED)
    failed = sum(1 for r in results if r.status == StoryStatus.FAILED)
    skipped = sum(1 for r in results if r.status == StoryStatus.SKIPPED)
    total = len(results)

    if failed == 0 and completed > 0:
        header_style = "green"
        header_text = "BMAD AUTOMATION COMPLETE"
    elif failed > 0:
        header_style = "red"
        header_text = "BMAD AUTOMATION FINISHED WITH FAILURES"
    else:
        header_style = "yellow"
        header_text = "BMAD AUTOMATION SUMMARY"

    console.print(Panel(f"[bold]{header_text}[/bold]", style=header_style))
    console.print()

    stats_table = Table(show_header=False, box=None, padding=(0, 2))
    stats_table.add_column("Label", style="dim")
    stats_table.add_column("Value")

    stats_table.add_row("Duration", f"[bold]{format_duration(total_duration)}[/bold]")
    stories_str = (
        f"[green]{completed} completed[/green], "
        f"[red]{failed} failed[/red], "
        f"[yellow]{skipped} skipped[/yellow]"
    )
    stats_table.add_row("Stories", stories_str)

    if total > 0 and (completed + failed) > 0:
        success_rate = completed / (completed + failed) * 100
        rate_color = (
            "green" if success_rate >= 80 else "yellow" if success_rate >= 50 else "red"
        )
        stats_table.add_row(
            "Success Rate", f"[{rate_color}]{success_rate:.0f}%[/{rate_color}]"
        )

    console.print(stats_table)
    console.print()

    if results:
        results_table = Table(title="Story Results")
        results_table.add_column("Story", style="cyan")
        results_table.add_column("Time", justify="right")
        results_table.add_column("Status")

        for result in results:
            if result.status == StoryStatus.COMPLETED:
                status_text = Text("Done", style="green")
            elif result.status == StoryStatus.FAILED:
                status_text = Text(f"Failed ({result.failed_step})", style="red")
            else:
                status_text = Text("Skipped", style="yellow")

            results_table.add_row(
                result.key, format_duration(result.duration), status_text
            )

        console.print(results_table)

    console.print()
    console.print(f"[dim]Log file: {config.log_file}[/dim]")


# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------

def send_notification(title: str, message: str) -> None:
    """Send a desktop notification.  Best-effort — failures are silently ignored."""
    try:
        system = platform.system()
        if system == "Windows":
            # Use PowerShell BurntToast or fallback to basic balloon tip
            tmgr = "Windows.UI.Notifications.ToastNotificationManager"
            ttype = "Windows.UI.Notifications.ToastTemplateType"
            tnot = "Windows.UI.Notifications.ToastNotification"
            ps_script = (
                f"[{tmgr}, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] | Out-Null; "
                f"$template = [{tmgr}]::"
                f"GetTemplateContent([{ttype}]::ToastText02); "
                "$textNodes = $template.GetElementsByTagName"
                "('text'); "
                "$textNodes.Item(0).AppendChild("
                f"$template.CreateTextNode('{title}'))"
                " | Out-Null; "
                "$textNodes.Item(1).AppendChild("
                f"$template.CreateTextNode('{message}'))"
                " | Out-Null; "
                f"$notifier = [{tmgr}]::"
                "CreateToastNotifier('bmad-automate'); "
                f"$notifier.Show([{tnot}]::new($template))"
            )
            subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True,
                timeout=10,
            )
        elif system == "Darwin":
            subprocess.run(
                [
                    "osascript", "-e",
                    f'display notification "{message}" with title "{title}"',
                ],
                capture_output=True,
                timeout=10,
            )
        else:
            # Linux / other — try notify-send
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True,
                timeout=10,
            )
    except Exception:  # noqa: BLE001
        pass  # notifications are best-effort
