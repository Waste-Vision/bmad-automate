"""Run context — replaces module-level global state."""

from __future__ import annotations

from dataclasses import dataclass, field

from bmad_automate.models import Config, StoryResult


@dataclass
class RunContext:
    """Mutable state for a single automation run.

    Passed explicitly instead of relying on module-level globals so
    the code is easier to test and reason about.
    """

    config: Config
    interrupted: bool = False
    results: list[StoryResult] = field(default_factory=list)
    start_time: float = 0.0


# Singleton used by the signal handler (which cannot receive extra args).
_ctx: RunContext | None = None


def set_active_context(ctx: RunContext) -> None:
    """Register *ctx* as the active run context (for signal handler access)."""
    global _ctx
    _ctx = ctx


def get_active_context() -> RunContext | None:
    """Return the active run context, or ``None`` if not set."""
    return _ctx
