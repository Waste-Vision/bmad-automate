"""Data models, enums, and constants for BMAD Automate."""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Compiled pattern for story keys: digit-digit-kebab-case
# (e.g., 3-3-account-translation)
STORY_PATTERN = re.compile(r"^\d+-\d+-.+$")

# Defaults
DEFAULT_SPRINT_STATUS = "_bmad-output/implementation-artifacts/sprint-status.yaml"
DEFAULT_STORY_DIR = "_bmad-output/implementation-artifacts"
DEFAULT_LOG_FILE = "bmad-automation.log"
DEFAULT_RETRIES = 1
DEFAULT_TIMEOUT = 3600  # 60 minutes
DEFAULT_BMAD_DIR = "_bmad"

# AI provider commands for non-interactive autonomous execution
AI_PROVIDERS: dict[str, str] = {
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

# All recognised step names, in pipeline order.
ALL_STEPS = ("create", "dev", "review", "commit", "pull")


class StepStatus(Enum):
    """Status of a single step execution within a story."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class StoryStatus(Enum):
    """Overall status of a story after all steps have been processed."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    """Result of executing a single step within a story."""

    name: str
    status: StepStatus
    duration: float = 0.0
    error: str = ""


@dataclass
class StoryResult:
    """Result of processing all steps for a single story."""

    key: str
    status: StoryStatus
    steps: list[StepResult] = field(default_factory=list)
    duration: float = 0.0
    failed_step: str = ""


@dataclass
class Config:
    """Configuration container for the automation script."""

    # Paths
    sprint_status: Path = Path(DEFAULT_SPRINT_STATUS)
    story_dir: Path = Path(DEFAULT_STORY_DIR)
    log_file: Path = Path(DEFAULT_LOG_FILE)

    # Execution control
    dry_run: bool = False
    yes: bool = False
    verbose: bool = False
    quiet: bool = False
    notify: bool = True

    # Story selection
    limit: int = 0  # 0 = unlimited
    start_from: str = ""
    specific_stories: list[str] = field(default_factory=list)
    epic: list[int] = field(default_factory=list)
    after_epic: list[int] = field(default_factory=list)

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
