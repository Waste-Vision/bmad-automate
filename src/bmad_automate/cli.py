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
    - Step-level control (skip any combination of steps)
    - Graceful Ctrl+C handling with partial summary
    - Comprehensive logging to file for debugging

Requirements:
    - Python 3.11+
    - Claude CLI installed (default), or GitHub Copilot CLI (--ai-provider github)
    - A BMAD project with _bmad/ workflow files

Usage:
    bmad-automate [options] [story_keys...]

See --help for full options or the README for comprehensive documentation.
"""

import atexit
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

# Constants
DEFAULT_SPRINT_STATUS = "_bmad-output/implementation-artifacts/sprint-status.yaml"
DEFAULT_STORY_DIR = "_bmad-output/implementation-artifacts"
DEFAULT_LOG_FILE = "bmad-automation.log"
DEFAULT_RETRIES = 1
DEFAULT_TIMEOUT = 3600  # 60 minutes
DEFAULT_BMAD_DIR = "_bmad"  # Default BMAD directory in project root

# AI provider commands for non-interactive autonomous execution
AI_PROVIDERS = {
    "claude": "claude --dangerously-skip-permissions -p",
    "github": "gh copilot --yolo -p",
}
DEFAULT_AI_PROVIDER = "claude"

# Workflow paths relative to the BMAD directory
WORKFLOW_ENGINE = "core/tasks/workflow.xml"
WORKFLOW_CREATE = "bmm/workflows/4-implementation/create-story/workflow.yaml"
WORKFLOW_DEV = "bmm/workflows/4-implementation/dev-story/workflow.yaml"
WORKFLOW_REVIEW = "bmm/workflows/4-implementation/code-review/workflow.yaml"
WORKFLOW_RETRO = "bmm/workflows/4-implementation/retrospective/workflow.yaml"
WORKFLOW_COURSE_CORRECT = "bmm/workflows/4-implementation/correct-course/workflow.yaml"
WORKFLOW_QUICK_DEV = "bmm/workflows/bmad-quick-flow/quick-dev/workflow.md"
WORKFLOW_EPIC_PREP = "bmm/workflows/bmad-quick-flow/quick-dev/workflow.md"

# Rich console for output
console = Console()

# Terminal title helpers — work on Windows Terminal, PowerShell, and most
# xterm-compatible terminals.  The escape sequence is harmless on terminals
# that don't support it (they simply ignore it).

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


class StepStatus(Enum):
    """
    Status of a single step execution within a story.

    Values:
        SUCCESS: Step completed without errors (exit code 0).
        FAILED: Step failed (non-zero exit, timeout, or exception).
        SKIPPED: Step was not executed (user skip flag or auto-skip).
    """

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class StoryStatus(Enum):
    """
    Overall status of a story after all steps have been processed.

    Values:
        COMPLETED: All steps succeeded (or were skipped intentionally).
        FAILED: At least one step failed, stopping further execution.
        SKIPPED: Story was skipped entirely (e.g., dry-run mode).
    """

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """
    Result of executing a single step within a story.

    Attributes:
        name: Step identifier (e.g., 'create-story', 'dev-story').
        status: Execution status (SUCCESS, FAILED, or SKIPPED).
        duration: Time taken in seconds (0.0 if skipped).
        error: Error message if failed, empty string otherwise.
    """

    name: str
    status: StepStatus
    duration: float = 0.0
    error: str = ""


@dataclass
class StoryResult:
    """
    Result of processing all steps for a single story.

    Attributes:
        key: Story identifier (e.g., '3-3-account-translation').
        status: Overall story status (COMPLETED, FAILED, or SKIPPED).
        steps: List of individual step results in execution order.
        duration: Total time taken for all steps in seconds.
        failed_step: Name of the step that failed, if any.
    """

    key: str
    status: StoryStatus
    steps: list[StepResult] = field(default_factory=list)
    duration: float = 0.0
    failed_step: str = ""


@dataclass
class Config:
    """
    Configuration container for the automation script.

    Controls all aspects of script behavior including paths, execution
    options, story selection, and step control.
    """

    # Paths
    sprint_status: Path = Path(DEFAULT_SPRINT_STATUS)
    story_dir: Path = Path(DEFAULT_STORY_DIR)
    log_file: Path = Path(DEFAULT_LOG_FILE)

    # Execution control
    dry_run: bool = False
    yes: bool = False
    verbose: bool = False
    quiet: bool = False

    # Story selection
    limit: int = 0  # 0 = unlimited
    start_from: str = ""
    specific_stories: list[str] = field(default_factory=list)
    epic: list[int] = field(default_factory=list)  # empty = all epics
    after_epic: list[int] = field(
        default_factory=list
    )  # explicitly run after-epic steps for these epics

    # Step control
    skip_create: bool = False
    skip_dev: bool = False
    skip_review: bool = False
    skip_commit: bool = False
    skip_pull: bool = False
    skip_retro: bool = False
    skip_course_correct: bool = False
    skip_retro_impl: bool = False
    skip_next_epic_prep: bool = False

    # Retry/Timeout
    retries: int = DEFAULT_RETRIES
    timeout: int = DEFAULT_TIMEOUT

    # BMAD directory
    bmad_dir: Path = Path(DEFAULT_BMAD_DIR)

    # AI provider
    ai_provider: str = DEFAULT_AI_PROVIDER

    @property
    def ai_command(self) -> str:
        """Return the AI CLI command for the configured provider."""
        return AI_PROVIDERS[self.ai_provider]


# Typer app instance
app = typer.Typer(
    name="bmad-automate",
    help="Automated BMAD Workflow Orchestrator",
    add_completion=False,
    rich_markup_mode="rich",
)


# Global state for signal handling
_interrupted = False
_current_story = ""
_results: list[StoryResult] = []
_start_time: float = 0.0
_config: Config | None = None


def signal_handler(signum: int, frame) -> None:  # noqa: ANN001
    """
    Handle interrupt signals (Ctrl+C, SIGTERM) gracefully.

    Sets the global _interrupted flag to True, allowing the main loop
    to complete the current operation before exiting with a summary.

    Args:
        signum: Signal number received.
        frame: Current stack frame (unused).
    """
    global _interrupted
    _interrupted = True
    console.print(
        "\n[yellow]Interrupt received. Finishing current operation...[/yellow]"
    )


def setup_signal_handlers() -> None:
    """
    Register signal handlers for graceful shutdown.

    Registers handlers for SIGINT (Ctrl+C) and SIGTERM to allow
    the script to complete current work and display a summary
    before exiting.
    """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def format_duration(seconds: float) -> str:
    """
    Format a duration in seconds to a human-readable string.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted string like "45s" or "3m 21s".

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


def log_to_file(message: str, config: Config) -> None:
    """
    Append a timestamped message to the log file.

    Args:
        message: Message to log.
        config: Configuration containing the log file path.

    The log format is: [YYYY-MM-DD HH:MM:SS] message
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


def get_actionable_stories(config: Config) -> dict[str, list[str]]:
    """
    Parse sprint-status.yaml and return stories grouped by actionable status.

    Reads the sprint-status.yaml file and extracts story keys that have
    one of the actionable statuses: 'review', 'in-progress', 'ready-for-dev',
    or 'backlog'. Story keys must match the pattern: digit-digit-kebab-case
    (e.g., '3-3-account').

    Args:
        config: Configuration containing the sprint_status file path.

    Returns:
        Dictionary with status names as keys and lists of story keys as values.
        Keys: 'review', 'in-progress', 'ready-for-dev', 'backlog'

    Raises:
        SystemExit: If sprint-status.yaml doesn't exist or has invalid format.
    """
    if not config.sprint_status.exists():
        console.print(
            f"[red]Error: Sprint status file not found: {config.sprint_status}[/red]"
        )
        sys.exit(2)

    with open(config.sprint_status, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        console.print("[red]Error: Invalid sprint-status.yaml format[/red]")
        sys.exit(2)

    dev_status = data["development_status"]

    # Pattern for story keys: digit-digit-kebab-case (e.g., 3-3-account-translation)
    story_pattern = re.compile(r"^\d+-\d+-.+$")

    # Actionable statuses in priority order
    actionable_statuses = ["review", "in-progress", "ready-for-dev", "backlog"]
    stories_by_status: dict[str, list[str]] = {s: [] for s in actionable_statuses}

    for key, status in dev_status.items():
        if story_pattern.match(key) and status in actionable_statuses:
            stories_by_status[status].append(key)

    return stories_by_status


def get_all_story_keys(config: Config) -> set[str]:
    """
    Get all story keys from sprint-status.yaml regardless of status.

    Used for validating user-specified story keys exist in the project.
    Unlike get_actionable_stories(), this returns ALL stories including
    'done', 'blocked', etc.

    Args:
        config: Configuration containing the sprint_status file path.

    Returns:
        Set of all story keys matching the digit-digit-kebab pattern.
        Returns empty set if file doesn't exist or is invalid.
    """
    if not config.sprint_status.exists():
        return set()

    with open(config.sprint_status, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        return set()

    dev_status = data["development_status"]
    story_pattern = re.compile(r"^\d+-\d+-.+$")

    return {key for key in dev_status.keys() if story_pattern.match(key)}


def filter_stories(
    stories_by_status: dict[str, list[str]], config: Config
) -> list[str]:
    """
    Apply filters to produce the final ordered list of stories to process.

    Handles story selection in this order:
    1. Specific stories: If user provides story keys, validate and use those.
    2. Auto-detect: Otherwise, combine stories in priority order.
    3. Apply --epic filter to limit to a specific epic.
    4. Apply --start-from and --limit filters to the result.

    Priority order for auto-detect: in-progress > ready-for-dev > backlog

    Args:
        stories_by_status: Dictionary of stories grouped by status.
        config: Configuration with filter settings.

    Returns:
        Ordered list of story keys to process.
    """
    # If specific stories provided, validate they exist (any status)
    if config.specific_stories:
        all_keys = get_all_story_keys(config)
        valid_stories = [s for s in config.specific_stories if s in all_keys]
        if len(valid_stories) != len(config.specific_stories):
            missing = set(config.specific_stories) - set(valid_stories)
            console.print(
                f"[yellow]Warning: Stories not found in sprint-status.yaml: "
                f"{missing}[/yellow]"
            )
        # Apply epic filter to specific stories too
        if config.epic:
            epic_prefixes = tuple(f"{e}-" for e in config.epic)
            valid_stories = [
                s for s in valid_stories if s.startswith(epic_prefixes)
            ]
        return valid_stories

    # Build ordered list: review first (resume interrupted reviews),
    # then in-progress, ready-for-dev, backlog
    stories = (
        stories_by_status.get("review", [])
        + stories_by_status.get("in-progress", [])
        + stories_by_status.get("ready-for-dev", [])
        + stories_by_status.get("backlog", [])
    )

    # Apply epic filter (e.g., --epic 3 or --epic 3,4,5)
    if config.epic:
        epic_prefixes = tuple(f"{e}-" for e in config.epic)
        stories = [s for s in stories if s.startswith(epic_prefixes)]
        if not stories:
            console.print(
                f"[yellow]Warning: No stories found for epic(s) "
                f"{','.join(str(e) for e in config.epic)}[/yellow]"
            )

    # Apply start-from filter
    if config.start_from:
        try:
            start_idx = stories.index(config.start_from)
            stories = stories[start_idx:]
        except ValueError:
            console.print(
                f"[yellow]Warning: Start story '{config.start_from}' "
                "not found, processing all[/yellow]"
            )

    # Apply limit
    if config.limit > 0:
        stories = stories[: config.limit]

    return stories


def get_story_path(story_key: str, config: Config) -> Path:
    """
    Construct the file path for a story's markdown file.

    Args:
        story_key: Story identifier (e.g., '3-3-account-translation').
        config: Configuration containing the story_dir path.

    Returns:
        Path to the story file: {story_dir}/{story_key}.md
    """
    return config.story_dir / f"{story_key}.md"


def run_step(
    step_name: str,
    command: str,
    story_key: str,
    config: Config,
) -> StepResult:
    """
    Execute a single workflow step with retry and timeout handling.

    Runs a shell command (the configured AI CLI invocation) and handles:
    - Dry-run mode (just prints what would run)
    - Retries on failure (configurable via config.retries)
    - Timeout enforcement (configurable via config.timeout)
    - Interrupt handling (checks _interrupted flag)
    - Logging to file (stdout, stderr, success/failure)

    Args:
        step_name: Human-readable step name (e.g., 'dev-story').
        command: Shell command to execute.
        story_key: Story identifier for logging.
        config: Configuration with timeout, retries, and output settings.

    Returns:
        StepResult with status, duration, and error details if failed.
    """
    start_time = time.time()

    if config.dry_run:
        epic_num = story_key.split("-")[0] if story_key else ""
        context = f" (Epic {epic_num}, Story {story_key})" if epic_num else ""
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: [magenta]{step_name}[/magenta]{context}"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    log_to_file(f"Running {step_name} for {story_key}", config)
    log_to_file(f"Command: {command}", config)

    for attempt in range(config.retries + 1):
        if _interrupted:
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error="Interrupted",
                duration=time.time() - start_time,
            )

        try:
            if not config.quiet:
                attempt_str = (
                    f" (attempt {attempt + 1}/{config.retries + 1})"
                    if attempt > 0
                    else ""
                )
                console.print(
                    f"  [dim]Running[/dim] [magenta]{step_name}[/magenta]"
                    f"{attempt_str}..."
                )

            result = subprocess.run(
                command,
                shell=True,
                capture_output=not config.verbose,
                text=True,
                timeout=config.timeout,
                encoding="utf-8",
                errors="replace",
            )

            # Restore our terminal title — subprocesses (e.g. claude CLI)
            # may have overwritten it with their own.
            set_running_title()

            # Filter out known CLI noise from stderr
            stderr = result.stderr or ""
            if config.ai_provider == "github":
                stderr = "\n".join(
                    line
                    for line in stderr.splitlines()
                    if "unknown option '--no-warnings'" not in line
                    and "Try 'copilot --help'" not in line
                ).strip()
            else:
                stderr = stderr.strip()

            # Log output
            if result.stdout:
                log_to_file(f"STDOUT:\n{result.stdout}", config)
            if stderr:
                log_to_file(f"STDERR:\n{stderr}", config)

            if result.returncode == 0:
                duration = time.time() - start_time
                log_to_file(
                    f"SUCCESS: {step_name} ({format_duration(duration)})", config
                )
                return StepResult(
                    name=step_name, status=StepStatus.SUCCESS, duration=duration
                )
            else:
                error = stderr or f"Exit code: {result.returncode}"
                log_to_file(f"FAILED: {step_name} - {error}", config)

                if attempt < config.retries:
                    console.print(f"  [yellow]Retrying {step_name}...[/yellow]")
                    continue

                return StepResult(
                    name=step_name,
                    status=StepStatus.FAILED,
                    error=error,
                    duration=time.time() - start_time,
                )

        except subprocess.TimeoutExpired:
            error = f"Timeout after {config.timeout}s"
            log_to_file(f"TIMEOUT: {step_name} - {error}", config)
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error=error,
                duration=time.time() - start_time,
            )

        except Exception as e:
            error = str(e)
            log_to_file(f"ERROR: {step_name} - {error}", config)
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error=error,
                duration=time.time() - start_time,
            )

    # Should not reach here, but just in case
    return StepResult(
        name=step_name,
        status=StepStatus.FAILED,
        error="Unknown error",
        duration=time.time() - start_time,
    )


def run_git_pull(
    story_key: str, config: Config, merge_conflict_prompt: str
) -> StepResult:
    """Pull from remote and merge; invoke AI only if there are conflicts.

    1. ``git pull`` — if clean, return SUCCESS.
    2. If exit code indicates merge conflicts, invoke the AI with
       *merge_conflict_prompt* to resolve them.
    3. If ``git pull`` fails for any other reason, return FAILED.

    Args:
        story_key: Story identifier for logging.
        config: Configuration with timeout, skip flags, and output settings.
        merge_conflict_prompt: Prompt sent to the AI if conflicts occur.

    Returns:
        StepResult with status, duration, and error details if failed.
    """
    step_name = "git-pull"
    start_time = time.time()

    if config.skip_pull:
        if not config.quiet:
            console.print(
                f"  [yellow]Skipping[/yellow] [magenta]{step_name}[/magenta]"
            )
        return StepResult(name=step_name, status=StepStatus.SKIPPED)

    if config.dry_run:
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: [magenta]{step_name}[/magenta]"
            f" (Story {story_key})"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    if not config.quiet:
        console.print(f"  [dim]Running[/dim] [magenta]{step_name}[/magenta]...")

    log_to_file(f"Running {step_name} for {story_key}", config)

    try:
        pull = subprocess.run(
            "git pull",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )

        if pull.stdout:
            log_to_file(f"git pull STDOUT:\n{pull.stdout}", config)
        if pull.stderr:
            log_to_file(f"git pull STDERR:\n{pull.stderr}", config)

        if pull.returncode == 0:
            duration = time.time() - start_time
            log_to_file(f"SUCCESS: {step_name} ({format_duration(duration)})", config)
            return StepResult(
                name=step_name, status=StepStatus.SUCCESS, duration=duration
            )

        # Check for merge conflicts
        has_conflicts = False
        combined = (pull.stdout or "") + (pull.stderr or "")
        if "CONFLICT" in combined or "merge conflict" in combined.lower():
            has_conflicts = True
        else:
            # Also check git status for unmerged paths
            status_check = subprocess.run(
                "git status --porcelain",
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if any(
                line.startswith("UU ") or line.startswith("AA ")
                for line in (status_check.stdout or "").splitlines()
            ):
                has_conflicts = True

        if has_conflicts:
            console.print(
                f"  [yellow]Merge conflicts detected — "
                f"invoking AI to resolve...[/yellow]"
            )
            log_to_file("Merge conflicts detected, invoking AI", config)
            ai = config.ai_command
            resolve_cmd = f'{ai} "{merge_conflict_prompt}"'
            resolve_result = run_step(
                "git-pull-resolve", resolve_cmd, story_key, config
            )
            # Return under the canonical step name so the summary is consistent
            return StepResult(
                name=step_name,
                status=resolve_result.status,
                duration=resolve_result.duration,
                error=resolve_result.error,
            )

        # Non-conflict failure
        error = (pull.stderr or "").strip() or f"Exit code: {pull.returncode}"
        log_to_file(f"FAILED: {step_name} - {error}", config)
        return StepResult(
            name=step_name,
            status=StepStatus.FAILED,
            error=error,
            duration=time.time() - start_time,
        )

    except subprocess.TimeoutExpired:
        error = "git pull timed out after 120s"
        log_to_file(f"TIMEOUT: {step_name} - {error}", config)
        return StepResult(
            name=step_name,
            status=StepStatus.FAILED,
            error=error,
            duration=time.time() - start_time,
        )

    except Exception as e:
        error = str(e)
        log_to_file(f"ERROR: {step_name} - {error}", config)
        return StepResult(
            name=step_name,
            status=StepStatus.FAILED,
            error=error,
            duration=time.time() - start_time,
        )


def process_story(
    story_key: str, config: Config, story_status: str = ""
) -> StoryResult:
    """
    Process all workflow steps for a single story.

    Executes the full BMAD workflow cycle for one story:
    1. create-story: Generate story markdown file (auto-skipped if exists)
    2. dev-story: Implement the story following the markdown spec
    3. code-review: Review implementation and auto-fix issues
    4. git-commit: Commit and push changes
    5. git-pull: Pull and merge remote changes

    Steps are auto-skipped based on story status:
    - in-review: skips create-story and dev-story (only review + commit)
    - in-progress: skips create-story if story file exists

    Each step can also be skipped via config flags. Execution stops on first failure.

    Args:
        story_key: Story identifier (e.g., '3-3-account-translation').
        config: Configuration with step skip flags and other settings.
        story_status: Current status from sprint-status.yaml (e.g., 'in-review').

    Returns:
        StoryResult with overall status and individual step results.
    """
    global _current_story
    _current_story = story_key

    start_time = time.time()
    story_path = get_story_path(story_key, config)
    steps: list[StepResult] = []

    log_to_file(f"=== Starting story: {story_key} ===", config)

    # AI CLI command prefix
    ai = config.ai_command
    bmad = config.bmad_dir

    # Build plain-English prompts that reference the BMAD workflow files
    # The AI reads the workflow engine + specific workflow instructions
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
        "message. Then push to the current branch. Do not forget submodules"
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
        # Story already developed — only needs code-review + commit
        if not config.quiet:
            console.print(
                "  [dim]Status is 'review', skipping create-story "
                "and dev-story[/dim]"
            )
        skip_create = True
        skip_dev = True
    elif not skip_create and story_path.exists():
        # Auto-skip create-story if story file already exists
        if not config.quiet:
            console.print("  [dim]Story file exists, skipping create-story[/dim]")
        skip_create = True

    # Define steps – each invokes the configured AI CLI
    step_definitions = [
        (
            "create-story",
            skip_create,
            f'{ai} "{create_prompt}"',
        ),
        (
            "dev-story",
            skip_dev,
            f'{ai} "{dev_prompt}"',
        ),
        (
            "code-review",
            config.skip_review,
            f'{ai} "{review_prompt}"',
        ),
        (
            "git-commit",
            config.skip_commit,
            f'{ai} "{commit_prompt}"',
        ),
    ]

    failed_step = ""
    for step_name, skip, command in step_definitions:
        if _interrupted:
            break

        if skip:
            if not config.quiet:
                console.print(
                    f"  [yellow]Skipping[/yellow] [magenta]{step_name}[/magenta]"
                )
            steps.append(StepResult(name=step_name, status=StepStatus.SKIPPED))
            continue

        result = run_step(step_name, command, story_key, config)
        steps.append(result)

        if result.status == StepStatus.FAILED:
            failed_step = step_name
            break

    # git-pull: direct subprocess, only invoke AI if merge conflicts arise
    if not failed_step and not _interrupted:
        pull_result = run_git_pull(
            story_key, config, merge_conflict_prompt
        )
        steps.append(pull_result)
        if pull_result.status == StepStatus.FAILED:
            failed_step = "git-pull"

    duration = time.time() - start_time

    # Determine overall status
    if any(s.status == StepStatus.FAILED for s in steps):
        status = StoryStatus.FAILED
    elif config.dry_run or all(s.status == StepStatus.SKIPPED for s in steps):
        status = StoryStatus.SKIPPED
    else:
        status = StoryStatus.COMPLETED

    log_to_file(
        f"=== Story {story_key}: {status.value} ({format_duration(duration)}) ===",
        config,
    )

    return StoryResult(
        key=story_key,
        status=status,
        steps=steps,
        duration=duration,
        failed_step=failed_step,
    )


def parse_epic_list(value: str) -> list[int]:
    """Parse a comma-separated string of epic numbers into a sorted list of ints.

    Args:
        value: Comma-separated epic numbers (e.g., "3" or "3,4,5") or empty string.

    Returns:
        Sorted list of unique epic numbers, or empty list if input is empty.

    Raises:
        typer.Exit: If any value is not a valid positive integer.
    """
    if not value.strip():
        return []
    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        try:
            n = int(part)
            if n <= 0:
                raise ValueError
            result.append(n)
        except ValueError:
            console.print(
                f"[red]Error: Invalid epic number '{part}' — "
                f"must be a positive integer[/red]"
            )
            raise typer.Exit(2)
    return sorted(set(result))


def is_epic_complete(epic_num: int, config: Config) -> bool:
    """Check whether all stories for a given epic have status 'done'.

    Unlike get_epics_needing_retro, this does NOT check the retrospective
    status — it only looks at whether every story in the epic is finished.

    Args:
        epic_num: Epic number to check.
        config: Configuration containing the sprint_status file path.

    Returns:
        True if the epic has stories and all of them are 'done'.
    """
    if not config.sprint_status.exists():
        return False

    with open(config.sprint_status, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        return False

    dev_status = data["development_status"]
    epic_prefix = f"{epic_num}-"
    story_pattern = re.compile(r"^\d+-\d+-.+$")

    statuses = [
        status
        for key, status in dev_status.items()
        if key.startswith(epic_prefix) and story_pattern.match(key)
    ]
    return len(statuses) > 0 and all(s == "done" for s in statuses)


def get_epics_needing_retro(config: Config) -> list[int]:
    """
    Re-read sprint-status.yaml and find epics where all stories are done
    but the retrospective has not been completed yet.

    An epic needs a retrospective when:
    - All its stories (digit-digit-kebab pattern) have status 'done'
    - Its retrospective entry (epic-N-retrospective) is 'optional' (not 'done')

    Args:
        config: Configuration containing the sprint_status file path.

    Returns:
        Sorted list of epic numbers that need retrospectives.
    """
    if not config.sprint_status.exists():
        return []

    with open(config.sprint_status, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        return []

    dev_status = data["development_status"]
    story_pattern = re.compile(r"^(\d+)-\d+-.+$")

    # Group stories by epic number
    stories_by_epic: dict[int, list[str]] = {}
    for key, status in dev_status.items():
        m = story_pattern.match(key)
        if m:
            epic_num = int(m.group(1))
            stories_by_epic.setdefault(epic_num, []).append(status)

    epics_needing_retro: list[int] = []
    for epic_num, statuses in sorted(stories_by_epic.items()):
        # All stories must be 'done'
        if not all(s == "done" for s in statuses):
            continue
        # Retrospective must not already be done
        retro_key = f"epic-{epic_num}-retrospective"
        retro_status = dev_status.get(retro_key, "")
        if retro_status != "done":
            epics_needing_retro.append(epic_num)

    return epics_needing_retro


def run_retrospective(epic_num: int, config: Config) -> StepResult:
    """
    Run the BMAD retrospective workflow for a completed epic.

    Args:
        epic_num: The epic number to run the retrospective for.
        config: Configuration with timeout, retries, and output settings.

    Returns:
        StepResult with status and duration.
    """
    ai = config.ai_command
    bmad = config.bmad_dir

    retro_prompt = (
        f"run the scrum-master retrospective workflow for a completed epic. "
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

    return run_step(step_name, command, f"epic-{epic_num}", config)


def run_course_correction(epic_num: int, config: Config) -> StepResult:
    """
    Run the scrum-master course-correction workflow for a completed epic.

    After the retrospective, the scrum master reviews whether the project
    needs a course correction and executes it if needed.

    Args:
        epic_num: The epic number to evaluate for course correction.
        config: Configuration with timeout, retries, and output settings.

    Returns:
        StepResult with status and duration.
    """
    ai = config.ai_command
    bmad = config.bmad_dir

    cc_prompt = (
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

    return run_step(step_name, command, f"epic-{epic_num}", config)


def run_retro_implementation(epic_num: int, config: Config) -> StepResult:
    """
    Run a quick dev pass to implement learnings from the retrospective.

    After the retrospective and course correction, this step applies any
    concrete improvements identified during the retro (e.g., refactoring,
    tooling changes, test coverage gaps) where relevant to the codebase.

    Args:
        epic_num: The epic number whose retro learnings to implement.
        config: Configuration with timeout, retries, and output settings.

    Returns:
        StepResult with status and duration.
    """
    ai = config.ai_command
    bmad = config.bmad_dir

    impl_prompt = (
        f"Load and execute the workflow at {bmad}/{WORKFLOW_QUICK_DEV}. "
        f"Implement the learnings from the Epic {epic_num} retrospective. "
        "Review the retrospective output and apply all improvements "
        "to the codebase — refactoring, tooling, test coverage, documentation, "
        "or process improvements. Skip anything that is not directly actionable in the code or documentation."
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

    return run_step(step_name, command, f"epic-{epic_num}", config)


def has_next_epic(epic_num: int, config: Config) -> bool:
    """
    Check whether the next epic (epic_num + 1) has stories in sprint-status.yaml.

    Reads the sprint-status.yaml file and looks for any story keys that
    start with the next epic's prefix (e.g., '4-' if epic_num is 3).

    Args:
        epic_num: The current (just-completed) epic number.
        config: Configuration containing the sprint_status file path.

    Returns:
        True if there are stories for the next epic, False otherwise.
    """
    if not config.sprint_status.exists():
        return False

    with open(config.sprint_status, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "development_status" not in data:
        return False

    dev_status = data["development_status"]
    next_epic_prefix = f"{epic_num + 1}-"
    story_pattern = re.compile(r"^\d+-\d+-.+$")

    return any(
        key.startswith(next_epic_prefix) and story_pattern.match(key)
        for key in dev_status.keys()
    )


def run_next_epic_preparation(
    previous_epic_num: int, config: Config
) -> StepResult:
    """
    Run preparation tasks for the next epic after a completed retro cycle.

    After the retrospective, course correction, and retro implementation for
    an epic are done, this step prepares the next epic by creating all its
    story files and reviewing the implementation plan.

    The preparation runs the create-story workflow for each story in the
    next epic, ensuring all artifacts are ready before development begins.

    Args:
        previous_epic_num: The epic number that just completed.
        config: Configuration with timeout, retries, and output settings.

    Returns:
        StepResult with status and duration.
    """
    ai = config.ai_command
    bmad = config.bmad_dir
    next_epic = previous_epic_num + 1

    prep_prompt = (
        f"Then load and execute the workflow at {bmad}/{WORKFLOW_EPIC_PREP}. "
        f"run all prep tasks for epic {next_epic} based on retrospective of {previous_epic_num}. "
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

    return run_step(step_name, command, f"epic-{next_epic}", config)


def run_after_epic_pipeline(
    epic_num: int,
    config: Config,
    retro_results: list[StepResult],
    *,
    require_retro_success: bool = False,
    progress: Progress | None = None,
    progress_task: TaskID | None = None,
) -> None:
    """Run the full after-epic pipeline for a single epic.

    Steps: retrospective -> course-correction -> retro-implementation
    -> next-epic-preparation.  Each step respects its own skip flag.

    When *require_retro_success* is True (used in the automatic in-story
    pipeline), steps after retro only run if the retro succeeded.
    When False (used for --after-epic), every non-skipped step runs
    independently.

    Args:
        epic_num: The epic number to process.
        config: Configuration with skip flags, timeout, retries, etc.
        retro_results: Mutable list to which step results are appended.
        require_retro_success: Gate later steps on retro success.
        progress: Optional Rich Progress instance for status updates.
        progress_task: Optional task ID within the Progress instance.
    """
    retro_ok = True  # assume success unless we actually run and fail

    # 1. Retrospective
    if not _interrupted and not config.skip_retro:
        if progress and progress_task is not None:
            progress.update(
                progress_task,
                description=f"[cyan]Epic {epic_num}: retrospective",
            )
        retro_result = run_retrospective(epic_num, config)
        retro_results.append(retro_result)
        retro_ok = retro_result.status == StepStatus.SUCCESS

        if retro_result.status == StepStatus.SUCCESS:
            console.print(
                f"  [green]OK[/green] retro-epic-{epic_num}"
                f"  [dim]{format_duration(retro_result.duration)}[/dim]"
            )
        elif retro_result.status == StepStatus.FAILED:
            console.print(
                f"  [red]XX[/red] retro-epic-{epic_num}"
                f"  [dim]{retro_result.error}[/dim]"
            )

    # 2. Course correction
    if not _interrupted and not config.skip_course_correct and (
        not require_retro_success or retro_ok
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
        log_to_file(
            f"Running course correction for epic {epic_num}",
            config,
        )
        cc_result = run_course_correction(epic_num, config)
        retro_results.append(cc_result)

        if cc_result.status == StepStatus.SUCCESS:
            console.print(
                f"  [green]OK[/green] "
                f"course-correct-epic-{epic_num}"
                f"  [dim]{format_duration(cc_result.duration)}[/dim]"
            )
        elif cc_result.status == StepStatus.FAILED:
            console.print(
                f"  [red]XX[/red] "
                f"course-correct-epic-{epic_num}"
                f"  [dim]{cc_result.error}[/dim]"
            )

    # 3. Retro implementation
    if not _interrupted and not config.skip_retro_impl and (
        not require_retro_success or retro_ok
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
        log_to_file(
            f"Implementing retro learnings for epic {epic_num}",
            config,
        )
        impl_result = run_retro_implementation(epic_num, config)
        retro_results.append(impl_result)

        if impl_result.status == StepStatus.SUCCESS:
            console.print(
                f"  [green]OK[/green] "
                f"retro-impl-epic-{epic_num}"
                f"  [dim]{format_duration(impl_result.duration)}[/dim]"
            )
        elif impl_result.status == StepStatus.FAILED:
            console.print(
                f"  [red]XX[/red] "
                f"retro-impl-epic-{epic_num}"
                f"  [dim]{impl_result.error}[/dim]"
            )

    # 4. Next epic preparation (only if next epic exists)
    if (
        not _interrupted
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
            f"{next_epic} (based on epic "
            f"{epic_num})...[/cyan]"
        )
        log_to_file(
            f"Preparing next epic {next_epic} "
            f"after epic {epic_num}",
            config,
        )
        prep_result = run_next_epic_preparation(epic_num, config)
        retro_results.append(prep_result)

        if prep_result.status == StepStatus.SUCCESS:
            console.print(
                f"  [green]OK[/green] "
                f"prep-next-epic-{next_epic}"
                f"  [dim]{format_duration(prep_result.duration)}[/dim]"
            )
        elif prep_result.status == StepStatus.FAILED:
            console.print(
                f"  [red]XX[/red] "
                f"prep-next-epic-{next_epic}"
                f"  [dim]{prep_result.error}[/dim]"
            )


def print_story_summary(result: StoryResult, config: Config) -> None:
    """
    Print a formatted summary of a completed story to the console.

    Displays the story key, total duration, status, and individual step results
    with color-coded status indicators. Respects quiet mode.

    Args:
        result: StoryResult containing status and step details.
        config: Configuration (checked for quiet mode).
    """
    if config.quiet:
        return

    # Status symbol and color
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

    # Show step details
    for step in result.steps:
        if step.status == StepStatus.SUCCESS:
            step_symbol = "[green]OK[/green]"
            duration_str = f"[dim]{format_duration(step.duration)}[/dim]"
        elif step.status == StepStatus.FAILED:
            step_symbol = "[red]XX[/red]"
            duration_str = f"[dim]{format_duration(step.duration)}[/dim]"
        else:
            step_symbol = "[yellow]--[/yellow]"
            duration_str = "[dim]skipped[/dim]"

        console.print(f"     {step_symbol} {step.name:<15} {duration_str}")


def print_dry_run_preview(stories: list[str], config: Config) -> None:
    """
    Print a detailed preview of what would be executed in dry-run mode.

    Displays a configuration table and numbered list of stories that would
    be processed. Used when --dry-run flag is specified.

    Args:
        stories: List of story keys that would be processed.
        config: Configuration to display (paths, timeouts, enabled steps).
    """
    console.print(
        Panel(
            "[bold cyan]DRY RUN MODE[/bold cyan] - No changes will be made",
            style="cyan",
        )
    )
    console.print()

    # Show configuration
    table = Table(title="Configuration", show_header=False, box=None)
    table.add_column("Setting", style="dim")
    table.add_column("Value")

    table.add_row("Sprint Status", str(config.sprint_status))
    table.add_row("Story Directory", str(config.story_dir))
    table.add_row("Stories to Process", str(len(stories)))
    table.add_row("Retries", str(config.retries))
    table.add_row("Timeout", f"{config.timeout}s")

    steps_enabled = []
    if not config.skip_create:
        steps_enabled.append("create-story")
    if not config.skip_dev:
        steps_enabled.append("dev-story")
    if not config.skip_review:
        steps_enabled.append("code-review")
    if not config.skip_commit:
        steps_enabled.append("git-commit")
    if not config.skip_pull:
        steps_enabled.append("git-pull")
    table.add_row("Steps", " -> ".join(steps_enabled))

    console.print(table)
    console.print()

    # Show stories
    console.print("[bold]Stories to process:[/bold]")
    for i, story in enumerate(stories, 1):
        console.print(f"  {i}. [cyan]{story}[/cyan]")

    console.print()


def confirm_start(stories: list[str], config: Config) -> bool:
    """
    Display a preview and prompt user for confirmation before starting.

    Shows the number of stories, enabled steps, and story list. Waits for
    user input unless interrupted with Ctrl+C or EOF.

    Args:
        stories: List of story keys to be processed.
        config: Configuration (for displaying enabled steps and log path).

    Returns:
        True if user confirms (Enter or 'y'), False if declined ('n') or interrupted.
    """
    console.print()
    console.print(Panel("[bold]BMAD Automation Preview[/bold]", style="blue"))
    console.print()

    console.print(f"  Stories to process: [bold]{len(stories)}[/bold]")

    steps_enabled = []
    if not config.skip_create:
        steps_enabled.append("create-story")
    if not config.skip_dev:
        steps_enabled.append("dev-story")
    if not config.skip_review:
        steps_enabled.append("code-review")
    if not config.skip_commit:
        steps_enabled.append("git-commit")
    if not config.skip_pull:
        steps_enabled.append("git-pull")
    console.print(f"  Steps: {' -> '.join(steps_enabled)}")
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
    results: list[StoryResult], config: Config, total_duration: float
) -> None:
    """
    Print the final summary report after all stories have been processed.

    Displays:
    - Header panel (green for success, red for failures, yellow for skipped)
    - Duration and story counts (completed, failed, skipped)
    - Success rate percentage
    - Results table with per-story status

    Args:
        results: List of StoryResult objects for all processed stories.
        config: Configuration (for log file path display).
        total_duration: Total elapsed time in seconds.
    """
    console.print()

    # Count results
    completed = sum(1 for r in results if r.status == StoryStatus.COMPLETED)
    failed = sum(1 for r in results if r.status == StoryStatus.FAILED)
    skipped = sum(1 for r in results if r.status == StoryStatus.SKIPPED)
    total = len(results)

    # Header panel
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

    # Summary stats
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

    # Results table
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


@app.command()
def main(
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
            "--verbose", "-v", help="Enable verbose output (show full command output)"
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Minimal output (only errors and summary)"),
    ] = False,
    # Story selection
    epic: Annotated[
        str,
        typer.Option(
            help="Only process stories for these epic numbers, comma-separated (e.g., --epic 3 or --epic 3,4,5)"
        ),
    ] = "",
    limit: Annotated[
        int,
        typer.Option(help="Process at most N stories (0 = unlimited)"),
    ] = 0,
    start_from: Annotated[
        str,
        typer.Option(help="Resume from specific story key (skip earlier stories)"),
    ] = "",
    # Step control
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
        typer.Option("--skip-pull", help="Skip git pull/merge step after commit"),
    ] = False,
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
            help="Skip implementing retrospective learnings after course correction",
        ),
    ] = False,
    skip_next_epic_prep: Annotated[
        bool,
        typer.Option(
            "--skip-next-epic-prep",
            help="Skip preparation tasks for the next epic after retro implementation",
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
        typer.Option(help=f"Retries per step (default: {DEFAULT_RETRIES})"),
    ] = DEFAULT_RETRIES,
    timeout: Annotated[
        int,
        typer.Option(help=f"Timeout per step in seconds (default: {DEFAULT_TIMEOUT})"),
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

        # Custom BMAD directory
        bmad-automate --bmad-dir path/to/_bmad

        # Use GitHub Copilot instead of Claude
        bmad-automate --ai-provider github
    """
    global _results, _start_time, _config

    # Build config from CLI arguments
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
    )
    _config = config

    # Validate AI provider
    if config.ai_provider not in AI_PROVIDERS:
        console.print(
            f"[red]Error: Unknown AI provider '{config.ai_provider}'[/red]\n"
            f"[dim]Available providers: {', '.join(AI_PROVIDERS)}[/dim]"
        )
        raise typer.Exit(2)

    # Validate BMAD directory exists
    if not config.bmad_dir.exists():
        console.print(
            f"[red]Error: BMAD directory not found: {config.bmad_dir}[/red]\n"
            "[dim]Make sure you're running from a BMAD project root, "
            "or use --bmad-dir to specify the path.[/dim]"
        )
        raise typer.Exit(2)

    # Set up signal handlers
    setup_signal_handlers()

    # Get and filter stories
    stories_by_status = get_actionable_stories(config)

    total_actionable = sum(len(v) for v in stories_by_status.values())

    # Check for pending retrospectives before deciding whether to exit
    pending_retro_epics: list[int] = []
    if not config.skip_retro:
        pending_retro_epics = get_epics_needing_retro(config)

    # When --epic is used and those epics are fully complete, automatically
    # include them in the after-epic pipeline so the user doesn't have to
    # separately pass --after-epic.
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

    # Build a status lookup so process_story knows each story's status
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

    # Dry run mode
    if config.dry_run:
        # Show after-epic pipeline (explicit --after-epic or auto-detected from --epic)
        # Uses run_after_epic_pipeline so dry-run output matches real execution logic.
        if after_epic_epics:
            console.print(
                "[bold]After-epic pipeline for "
                f"epic(s) {','.join(str(e) for e in after_epic_epics)}:[/bold]"
            )
            _dry_results: list[StepResult] = []
            for epic_num in after_epic_epics:
                run_after_epic_pipeline(epic_num, config, _dry_results)
            console.print()

        # Show pending retrospective pipeline (they run before stories)
        if pending_retro_epics:
            auto_retro_epics = [
                e for e in pending_retro_epics if e not in after_epic_epics
            ]
            if auto_retro_epics:
                console.print(
                    "[bold]Pending retrospective pipeline "
                    "(will run before stories):[/bold]"
                )
                _dry_results2: list[StepResult] = []
                for epic_num in auto_retro_epics:
                    run_after_epic_pipeline(
                        epic_num, config, _dry_results2,
                        require_retro_success=True,
                    )
                console.print()

        # Then show what story steps would run
        if filtered_stories:
            print_dry_run_preview(filtered_stories, config)
            console.print("[bold]Story steps that would run:[/bold]")
            for story in filtered_stories:
                if _interrupted:
                    break
                result = process_story(story, config, story_status_map.get(story, ""))
                _results.append(result)
        else:
            console.print("[dim]No actionable stories to process.[/dim]")
        raise typer.Exit(0)

    # Confirmation
    if not config.yes:
        if not confirm_start(filtered_stories, config):
            raise typer.Exit(0)

    # Set running indicator in terminal title (after dry-run/confirmation gates).
    # Register cleanup via atexit so the title is restored even if an unhandled
    # exception or SIGTERM causes an early exit (SIGKILL cannot be caught).
    set_running_title()
    atexit.register(restore_terminal_title, success=False)

    # Initialize log file
    log_to_file("=" * 50, config)
    log_to_file("BMAD Automation Started", config)
    log_to_file(f"Stories to process: {len(filtered_stories)}", config)
    log_to_file("=" * 50, config)

    _start_time = time.time()

    retro_results: list[StepResult] = []
    retro_done_epics: set[int] = set()

    # Run explicit --after-epic pipeline FIRST
    if after_epic_epics and not _interrupted:
        for epic_num in after_epic_epics:
            if _interrupted:
                break
            retro_done_epics.add(epic_num)
            console.print(
                f"\n  [cyan]Running after-epic pipeline for "
                f"epic {epic_num}...[/cyan]"
            )
            log_to_file(
                f"Running after-epic pipeline for epic {epic_num}",
                config,
            )
            run_after_epic_pipeline(epic_num, config, retro_results)

    # Run any already-pending retrospective pipelines BEFORE processing stories
    if pending_retro_epics and not _interrupted:
        for epic_num in pending_retro_epics:
            if _interrupted:
                break
            if epic_num in retro_done_epics:
                continue  # Already handled by --after-epic
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
                epic_num, config, retro_results, require_retro_success=True
            )

    # Process stories with progress
    if filtered_stories:
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
                if _interrupted:
                    console.print("\n[yellow]Interrupted by user[/yellow]")
                    break

                progress.update(
                    task,
                    description=f"[cyan]Story {i + 1}/{len(filtered_stories)}: {story}",
                )

                result = process_story(
                    story, config, story_status_map.get(story, "")
                )
                _results.append(result)

                print_story_summary(result, config)

                progress.advance(task)

                # Stop on failure unless we want to continue
                if result.status == StoryStatus.FAILED:
                    console.print(f"\n[red]Story {story} failed, stopping automation[/red]")
                    break

                # After each successful story, check if its epic now needs a retro
                if (
                    not config.skip_retro
                    and not _interrupted
                    and result.status == StoryStatus.COMPLETED
                ):
                    epics = get_epics_needing_retro(config)
                    for epic_num in epics:
                        if epic_num not in retro_done_epics:
                            retro_done_epics.add(epic_num)
                            console.print(
                                f"\n  [cyan]Epic {epic_num} complete — "
                                f"running after-epic pipeline...[/cyan]"
                            )
                            log_to_file(
                                f"Running after-epic pipeline for epic {epic_num}",
                                config,
                            )
                            run_after_epic_pipeline(
                                epic_num,
                                config,
                                retro_results,
                                require_retro_success=True,
                                progress=progress,
                                progress_task=task,
                            )

    # Final summary
    total_duration = time.time() - _start_time
    print_final_summary(_results, config, total_duration)

    log_to_file("=" * 50, config)
    log_to_file("BMAD Automation Finished", config)
    log_to_file(f"Duration: {format_duration(total_duration)}", config)
    log_to_file("=" * 50, config)

    # Restore terminal title with status indicator.
    # Unregister the atexit fallback first so it doesn't fire a second time.
    has_failures = any(r.status == StoryStatus.FAILED for r in _results)
    atexit.unregister(restore_terminal_title)
    restore_terminal_title(success=not has_failures and not _interrupted)

    # Exit code
    if has_failures:
        raise typer.Exit(1)
    elif _interrupted:
        raise typer.Exit(130)


if __name__ == "__main__":
    app()
