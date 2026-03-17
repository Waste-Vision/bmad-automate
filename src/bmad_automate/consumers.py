"""Event consumers — translate PipelineEvents into CLI output."""

from __future__ import annotations

from bmad_automate.events import (
    LOG_LINE,
    LOG_MESSAGE,
    STEP_DONE,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_START,
    STORY_DONE,
    STORY_START,
    PipelineEvent,
)
from bmad_automate.models import Config
from bmad_automate.ui import console, format_duration, log_to_file


class CliConsumer:
    """Receives PipelineEvents and renders them as Rich terminal output.

    Also routes log messages to the file logger.
    """

    def __init__(self, config: Config, *, quiet: bool = False) -> None:
        self.config = config
        self.quiet = quiet

    def __call__(self, event: PipelineEvent) -> None:
        """Dispatch an event to the appropriate handler."""
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler:
            handler(event)

    def _on_step_start(self, event: PipelineEvent) -> None:
        if self.quiet:
            return
        attempt = event.payload.get("attempt", 0)
        retries = event.payload.get("retries", 0)
        attempt_str = (
            f" (attempt {attempt + 1}/{retries + 1})" if attempt > 0 else ""
        )
        console.print(
            f"  [dim]Running[/dim] [magenta]{event.step}[/magenta]"
            f"{attempt_str}..."
        )

    def _on_step_done(self, event: PipelineEvent) -> None:
        duration = event.payload.get("duration", 0.0)
        log_to_file(
            f"SUCCESS: {event.step} ({format_duration(duration)})",
            self.config,
        )

    def _on_step_failed(self, event: PipelineEvent) -> None:
        error = event.payload.get("error", "")
        log_to_file(f"FAILED: {event.step} - {error}", self.config)

    def _on_step_skipped(self, event: PipelineEvent) -> None:
        if self.quiet:
            return
        message = event.payload.get("message")
        if message:
            console.print(f"  [dim]{message}[/dim]")
        else:
            console.print(
                f"  [yellow]Skipping[/yellow] [magenta]{event.step}[/magenta]"
            )

    def _on_log_line(self, event: PipelineEvent) -> None:
        label = event.payload.get("label", "")
        stream = event.payload.get("stream", "")
        content = event.payload.get("content", "")
        if content:
            log_to_file(f"{label} {stream}:\n{content}", self.config)

    def _on_log_message(self, event: PipelineEvent) -> None:
        message = event.payload.get("message", "")
        if message:
            log_to_file(message, self.config)
