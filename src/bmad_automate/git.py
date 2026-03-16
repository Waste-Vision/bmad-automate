"""Git operations and subprocess helpers."""

from __future__ import annotations

import re
import subprocess
import time

from bmad_automate.context import RunContext
from bmad_automate.models import Config, StepResult, StepStatus
from bmad_automate.stories import invalidate_cache
from bmad_automate.ui import (
    console,
    format_duration,
    log_to_file,
    set_running_title,
)

# ---------------------------------------------------------------------------
# Low-level subprocess helper (eliminates repeated boilerplate)
# ---------------------------------------------------------------------------

def run_git_command(
    cmd: str,
    config: Config,
    label: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, log its output, and return the CompletedProcess.

    This is a thin wrapper that handles encoding, timeout, and logging so
    callers don't repeat the same pattern.
    """
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        log_to_file(f"{label} STDOUT:\n{result.stdout}", config)
    if result.stderr:
        log_to_file(f"{label} STDERR:\n{result.stderr}", config)
    return result


# ---------------------------------------------------------------------------
# run_step — execute a single AI-driven step
# ---------------------------------------------------------------------------

def run_step(
    step_name: str,
    command: str,
    story_key: str,
    config: Config,
    ctx: RunContext,
) -> StepResult:
    """Execute a single workflow step with retry and timeout handling."""
    start_time = time.time()

    if config.dry_run:
        epic_num = story_key.split("-")[0] if story_key else ""
        context = f" (Epic {epic_num}, Story {story_key})" if epic_num else ""
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta]{context}"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    log_to_file(f"Running {step_name} for {story_key}", config)
    log_to_file(f"Command: {command}", config)

    for attempt in range(config.retries + 1):
        if ctx.interrupted:
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

            set_running_title()

            # Filter known CLI noise from stderr
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

    return StepResult(
        name=step_name,
        status=StepStatus.FAILED,
        error="Unknown error",
        duration=time.time() - start_time,
    )


# ---------------------------------------------------------------------------
# mark_story_done
# ---------------------------------------------------------------------------

def mark_story_done(story_key: str, config: Config) -> None:
    """Update a story's status to 'done' in sprint-status.yaml."""
    if not config.sprint_status.exists():
        return

    with open(config.sprint_status, encoding="utf-8") as f:
        raw = f.read()

    pattern = re.compile(
        rf"^(\s*{re.escape(story_key)}\s*:\s*)(\S+)(.*)$", re.MULTILINE
    )
    match = pattern.search(raw)
    if not match or match.group(2) == "done":
        return

    updated = pattern.sub(r"\g<1>done\g<3>", raw)
    with open(config.sprint_status, "w", encoding="utf-8") as f:
        f.write(updated)

    # Invalidate the YAML cache so subsequent reads see the update.
    invalidate_cache(config.sprint_status)
    log_to_file(f"Marked {story_key} as done in sprint-status.yaml", config)


# ---------------------------------------------------------------------------
# run_git_pull
# ---------------------------------------------------------------------------

def run_git_pull(
    story_key: str,
    config: Config,
    merge_conflict_prompt: str,
    ctx: RunContext,
) -> StepResult:
    """Pull from remote and merge; invoke AI only if there are conflicts."""
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
        pull = run_git_command("git pull", config, "git pull")

        if pull.returncode == 0:
            push = run_git_command("git push", config, "git push")
            duration = time.time() - start_time
            if push.returncode == 0:
                log_to_file(
                    f"SUCCESS: {step_name} ({format_duration(duration)})", config
                )
                return StepResult(
                    name=step_name, status=StepStatus.SUCCESS, duration=duration
                )
            error = (
                (push.stderr or "").strip()
                or f"git push exit code: {push.returncode}"
            )
            log_to_file(f"FAILED: {step_name} (push) - {error}", config)
            return StepResult(
                name=step_name, status=StepStatus.FAILED,
                error=error, duration=duration,
            )

        # Check for merge conflicts
        has_conflicts = False
        combined = (pull.stdout or "") + (pull.stderr or "")
        if "CONFLICT" in combined or "merge conflict" in combined.lower():
            has_conflicts = True
        else:
            status_check = run_git_command(
                "git status --porcelain", config, "git status"
            )
            if any(
                line.startswith(("UU ", "AA "))
                for line in (status_check.stdout or "").splitlines()
            ):
                has_conflicts = True

        if has_conflicts:
            console.print(
                "  [yellow]Merge conflicts detected — "
                "invoking AI to resolve...[/yellow]"
            )
            log_to_file("Merge conflicts detected, invoking AI", config)
            ai = config.ai_command
            resolve_cmd = f'{ai} "{merge_conflict_prompt}"'
            resolve_result = run_step(
                "git-pull-resolve", resolve_cmd, story_key, config, ctx
            )
            return StepResult(
                name=step_name,
                status=resolve_result.status,
                duration=resolve_result.duration,
                error=resolve_result.error,
            )

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
            name=step_name, status=StepStatus.FAILED,
            error=error, duration=time.time() - start_time,
        )

    except Exception as e:
        error = str(e)
        log_to_file(f"ERROR: {step_name} - {error}", config)
        return StepResult(
            name=step_name, status=StepStatus.FAILED,
            error=error, duration=time.time() - start_time,
        )


# ---------------------------------------------------------------------------
# run_after_epic_commit
# ---------------------------------------------------------------------------

def run_after_epic_commit(epic_num: int, config: Config) -> StepResult:
    """Commit, pull, and push any changes produced by the after-epic pipeline."""
    step_name = f"after-epic-commit-{epic_num}"
    start_time = time.time()

    if not config.quiet:
        console.print(
            f"  [dim]Running[/dim] [magenta]{step_name}[/magenta]..."
        )
    log_to_file(f"Running {step_name}", config)

    try:
        status = run_git_command("git status --porcelain", config, "status check")
        if not (status.stdout or "").strip():
            duration = time.time() - start_time
            log_to_file(f"SUCCESS: {step_name} (nothing to commit)", config)
            return StepResult(
                name=step_name, status=StepStatus.SUCCESS, duration=duration
            )

        commit_cmd = (
            'git add -A && git commit -m '
            f'"after-epic: retro and prep for epic {epic_num}"'
        )
        commit = run_git_command(commit_cmd, config, "commit")
        if commit.returncode != 0:
            error = (
                (commit.stderr or "").strip()
                or f"git commit exit code: {commit.returncode}"
            )
            log_to_file(f"FAILED: {step_name} (commit) - {error}", config)
            return StepResult(
                name=step_name, status=StepStatus.FAILED,
                error=error, duration=time.time() - start_time,
            )

        pull = run_git_command("git pull", config, "pull")
        if pull.returncode != 0:
            error = (
                (pull.stderr or "").strip()
                or f"git pull exit code: {pull.returncode}"
            )
            log_to_file(f"FAILED: {step_name} (pull) - {error}", config)
            return StepResult(
                name=step_name, status=StepStatus.FAILED,
                error=error, duration=time.time() - start_time,
            )

        push = run_git_command("git push", config, "push")
        duration = time.time() - start_time
        if push.returncode == 0:
            log_to_file(
                f"SUCCESS: {step_name} ({format_duration(duration)})", config
            )
            return StepResult(
                name=step_name, status=StepStatus.SUCCESS, duration=duration
            )

        error = (
            (push.stderr or "").strip()
            or f"git push exit code: {push.returncode}"
        )
        log_to_file(f"FAILED: {step_name} (push) - {error}", config)
        return StepResult(
            name=step_name, status=StepStatus.FAILED,
            error=error, duration=duration,
        )

    except subprocess.TimeoutExpired:
        error = "Timed out after 120s"
        log_to_file(f"TIMEOUT: {step_name} - {error}", config)
        return StepResult(
            name=step_name, status=StepStatus.FAILED,
            error=error, duration=time.time() - start_time,
        )

    except Exception as e:
        error = str(e)
        log_to_file(f"ERROR: {step_name} - {error}", config)
        return StepResult(
            name=step_name, status=StepStatus.FAILED,
            error=error, duration=time.time() - start_time,
        )
