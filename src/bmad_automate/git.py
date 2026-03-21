"""Git operations and subprocess helpers."""

from __future__ import annotations

import re
import subprocess
import time

from bmad_automate.context import RunContext
from bmad_automate.events import (
    LOG_LINE,
    LOG_MESSAGE,
    STEP_DONE,
    STEP_FAILED,
    STEP_RETRYING,
    STEP_START,
    PipelineEvent,
)
from bmad_automate.retry import RetryController
from bmad_automate.models import Config, StepResult, StepStatus
from bmad_automate.stories import invalidate_cache
from bmad_automate.ui import (
    console,
    format_duration,
    log_to_file,
    set_running_title,
)

# ---------------------------------------------------------------------------
# Global subprocess registry — lets the server terminate AI processes on shutdown
# ---------------------------------------------------------------------------

import threading as _threading

_active_procs: list["subprocess.Popen[str]"] = []
_active_procs_lock = _threading.Lock()


def _register_proc(proc: "subprocess.Popen[str]") -> None:
    with _active_procs_lock:
        _active_procs.append(proc)


def _unregister_proc(proc: "subprocess.Popen[str]") -> None:
    with _active_procs_lock:
        try:
            _active_procs.remove(proc)
        except ValueError:
            pass


def _kill_proc_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill a process and all its children (handles shell=True on Windows)."""
    import os
    import sys

    pid = proc.pid
    if sys.platform == "win32":
        # On Windows, terminate() only kills cmd.exe, not its children.
        # taskkill /F /T kills the entire process tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
    else:
        # On Unix, kill the entire process group.
        try:
            pgid = os.getpgid(pid)
            import signal as _signal
            os.killpg(pgid, _signal.SIGTERM)
        except Exception:
            proc.terminate()


def terminate_all_active() -> None:
    """Terminate all subprocesses currently tracked by run_step."""
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            _kill_proc_tree(proc)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Low-level subprocess helper (eliminates repeated boilerplate)
# ---------------------------------------------------------------------------

def run_git_command(
    cmd: str,
    config: Config,
    label: str,
    timeout: int = 120,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, log its output, and return the CompletedProcess.

    This is a thin wrapper that handles encoding, timeout, and logging so
    callers don't repeat the same pattern.
    """
    actual_cwd = cwd if cwd is not None else str(config.project_root)
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        cwd=actual_cwd,
    )
    if result.stdout:
        log_to_file(f"{label} STDOUT:\n{result.stdout}", config)
    if result.stderr:
        log_to_file(f"{label} STDERR:\n{result.stderr}", config)
    return result


# ---------------------------------------------------------------------------
# run_step — execute a single AI-driven step
# ---------------------------------------------------------------------------

def _extract_epic_num(story_key: str) -> int:
    """Extract epic number from a story key like '3-1-feature' or 'epic-3'."""
    try:
        parts = story_key.split("-")
        # Handle 'epic-N' format used by after-epic steps
        if parts[0] == "epic" and len(parts) > 1:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


def run_step(
    step_name: str,
    command: str,
    story_key: str,
    config: Config,
    ctx: RunContext,
) -> StepResult:
    """Execute a single workflow step with retry and timeout handling."""
    start_time = time.time()
    bus = ctx.event_bus
    epic_num = _extract_epic_num(story_key)

    if config.dry_run:
        context = f" (Epic {epic_num}, Story {story_key})" if epic_num else ""
        console.print(
            f"  [dim][DRY-RUN][/dim] Would run: "
            f"[magenta]{step_name}[/magenta]{context}"
        )
        return StepResult(name=step_name, status=StepStatus.SKIPPED, duration=0.0)

    log_to_file(f"Running {step_name} for {story_key}", config)
    log_to_file(f"Command: {command}", config)

    retry_ctrl: RetryController | None = None
    registry = ctx.retry_registry

    for attempt in range(config.retries + 1):
        if ctx.interrupted:
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error="Interrupted",
                duration=time.time() - start_time,
            )

        try:
            bus.emit(PipelineEvent(
                epic=epic_num, story=story_key, step=step_name,
                kind=STEP_START,
                payload={"attempt": attempt, "retries": config.retries},
            ))
            bus.drain()

            if not config.quiet and not bus.has_subscribers():
                attempt_str = (
                    f" (attempt {attempt + 1}/{config.retries + 1})"
                    if attempt > 0
                    else ""
                )
                console.print(
                    f"  [dim]Running[/dim] [magenta]{step_name}[/magenta]"
                    f"{attempt_str}..."
                )

            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=None if config.verbose else subprocess.PIPE,
                stderr=None if config.verbose else subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(config.project_root),
            )
            _register_proc(proc)
            try:
                deadline = time.time() + (config.timeout or 7200)
                while True:
                    if ctx.interrupted:
                        _kill_proc_tree(proc)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        stdout, stderr = "", ""
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        proc.kill()
                        proc.communicate()
                        raise subprocess.TimeoutExpired(command, config.timeout)
                    try:
                        stdout, stderr = proc.communicate(timeout=min(2.0, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                _unregister_proc(proc)
            result = subprocess.CompletedProcess(
                proc.args, proc.returncode,
                stdout or "", stderr or "",
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
                bus.emit(PipelineEvent(
                    epic=epic_num, story=story_key, step=step_name,
                    kind=LOG_LINE,
                    payload={"label": "", "stream": "STDOUT",
                             "content": result.stdout},
                ))
                log_to_file(f"STDOUT:\n{result.stdout}", config)
            if stderr:
                bus.emit(PipelineEvent(
                    epic=epic_num, story=story_key, step=step_name,
                    kind=LOG_LINE,
                    payload={"label": "", "stream": "STDERR",
                             "content": stderr},
                ))
                log_to_file(f"STDERR:\n{stderr}", config)

            if result.returncode == 0:
                duration = time.time() - start_time
                bus.emit(PipelineEvent(
                    epic=epic_num, story=story_key, step=step_name,
                    kind=STEP_DONE,
                    payload={"duration": duration},
                ))
                bus.drain()
                log_to_file(
                    f"SUCCESS: {step_name} ({format_duration(duration)})", config
                )
                # Clean up retry controller on success
                if retry_ctrl is not None:
                    registry.unregister(retry_ctrl.key)
                return StepResult(
                    name=step_name, status=StepStatus.SUCCESS, duration=duration
                )

            error = stderr or f"Exit code: {result.returncode}"
            log_to_file(f"FAILED: {step_name} - {error}", config)

            if attempt < config.retries and not ctx.interrupted:
                # Create or reuse retry controller for coordinated backoff
                if retry_ctrl is None:
                    retry_ctrl = RetryController(
                        epic=epic_num, story=story_key, step=step_name,
                        max_retries=config.retries,
                    )
                    registry.register(retry_ctrl)

                backoff = retry_ctrl.enter_backoff()
                bus.emit(PipelineEvent(
                    epic=epic_num, story=story_key, step=step_name,
                    kind=STEP_RETRYING,
                    payload={"attempt": attempt + 1, "backoff": backoff,
                             "error": error},
                ))
                bus.drain()

                if not config.quiet and not bus.has_subscribers():
                    console.print(
                        f"  [yellow]Retrying {step_name} "
                        f"in {backoff:.0f}s...[/yellow]"
                    )

                action = retry_ctrl.wait_backoff()
                if action == "skip" or ctx.interrupted:
                    registry.unregister(retry_ctrl.key)
                    bus.emit(PipelineEvent(
                        epic=epic_num, story=story_key, step=step_name,
                        kind=STEP_FAILED,
                        payload={"error": "Skipped by user",
                                 "duration": time.time() - start_time},
                    ))
                    bus.drain()
                    return StepResult(
                        name=step_name, status=StepStatus.FAILED,
                        error="Skipped by user",
                        duration=time.time() - start_time,
                    )
                # action == "retry" — loop continues
                continue

            # Exhausted all retries
            if retry_ctrl is not None:
                registry.unregister(retry_ctrl.key)
            bus.emit(PipelineEvent(
                epic=epic_num, story=story_key, step=step_name,
                kind=STEP_FAILED,
                payload={"error": error, "duration": time.time() - start_time},
            ))
            bus.drain()
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error=error,
                duration=time.time() - start_time,
            )

        except subprocess.TimeoutExpired:
            error = f"Timeout after {config.timeout}s"
            log_to_file(f"TIMEOUT: {step_name} - {error}", config)
            if retry_ctrl is not None:
                registry.unregister(retry_ctrl.key)
            bus.emit(PipelineEvent(
                epic=epic_num, story=story_key, step=step_name,
                kind=STEP_FAILED,
                payload={"error": error, "duration": time.time() - start_time},
            ))
            bus.drain()
            return StepResult(
                name=step_name,
                status=StepStatus.FAILED,
                error=error,
                duration=time.time() - start_time,
            )

        except Exception as e:
            error = str(e)
            log_to_file(f"ERROR: {step_name} - {error}", config)
            if retry_ctrl is not None:
                registry.unregister(retry_ctrl.key)
            bus.emit(PipelineEvent(
                epic=epic_num, story=story_key, step=step_name,
                kind=STEP_FAILED,
                payload={"error": error, "duration": time.time() - start_time},
            ))
            bus.drain()
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
    """Commit any changes produced by the after-epic pipeline.

    In worktree mode only commits (the merge queue handles syncing).
    In sequential mode also pulls and pushes to the remote.
    """
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

        # In worktree mode the merge queue handles syncing — skip pull/push.
        if config.in_worktree:
            duration = time.time() - start_time
            log_to_file(
                f"SUCCESS: {step_name} ({format_duration(duration)})", config
            )
            return StepResult(
                name=step_name, status=StepStatus.SUCCESS, duration=duration
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
