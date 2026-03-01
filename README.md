# BMAD Automate

Automated [BMAD](https://github.com/bmad-code-org/BMAD-METHOD) Workflow Orchestrator — powered by GitHub Copilot CLI.

Processes stories through a complete development cycle using `gh copilot -p --yolo`:

```text
create-story -> dev-story -> code-review -> git-commit
```

For each step, the tool builds a plain-English prompt that tells Copilot to read
the BMAD workflow engine and execute the corresponding workflow files from
the project's `_bmad/` directory. No IDE-specific slash commands required.

## Prerequisites

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) with the Copilot extension:
  ```bash
  gh extension install github/gh-copilot
  ```

## Installation

### From GitHub (recommended)

```bash
uv tool install git+https://github.com/robertguss/bmad-automate
```

### From PyPI (when published)

```bash
uv tool install bmad-automate
```

### For development

```bash
git clone https://github.com/robertguss/bmad-automate.git
cd bmad-automate
uv sync
```

## Usage

Run from any BMAD project directory (must contain a `_bmad/` folder):

```bash
# Show help
bmad-automate --help

# Dry run to preview what would be processed
bmad-automate --dry-run

# Process next story
bmad-automate --limit 1

# Process all stories in epic 3
bmad-automate --epic 3

# Process specific story
bmad-automate 3-3-account-translation

# Non-interactive with verbose output
bmad-automate --yes --verbose --limit 5

# Custom BMAD directory location
bmad-automate --bmad-dir path/to/_bmad
```

## Options

### General Options

| Flag            | Description                              |
| --------------- | ---------------------------------------- |
| `-n, --dry-run` | Preview what would run without executing |
| `-y, --yes`     | Skip interactive confirmation prompt     |
| `-v, --verbose` | Show full AI output during execution     |
| `-q, --quiet`   | Minimal output (only errors and summary) |

### BMAD Directory

| Flag         | Default | Description                                        |
| ------------ | ------- | -------------------------------------------------- |
| `--bmad-dir` | `_bmad` | Path to the `_bmad/` directory with workflow files  |

### Story Selection

| Flag               | Description                               |
| ------------------ | ----------------------------------------- |
| `--epic N`         | Only process stories for epic N           |
| `--limit N`        | Process at most N stories (0 = unlimited) |
| `--start-from KEY` | Resume from specific story key            |
| `[stories...]`     | Specific story keys to process            |

### Step Control

| Flag            | Description                                          |
| --------------- | ---------------------------------------------------- |
| `--skip-create` | Skip create-story step                               |
| `--skip-dev`    | Skip dev-story step                                  |
| `--skip-review` | Skip code-review step                                |
| `--skip-commit` | Skip git commit/push step                            |
| `--skip-retro`  | Skip automatic retrospective after completing an epic |
| `--skip-course-correct` | Skip scrum-master course correction after epic retrospective |
| `--skip-retro-impl` | Skip implementing retrospective learnings after course correction |

### Retry & Timeout

| Flag          | Default | Description                 |
| ------------- | ------- | --------------------------- |
| `--retries N` | 1       | Retries per step on failure |
| `--timeout N` | 3600 | Timeout per step in seconds |

### Paths

| Flag              | Default                                                    | Description           |
| ----------------- | ---------------------------------------------------------- | --------------------- |
| `--sprint-status` | `_bmad-output/implementation-artifacts/sprint-status.yaml` | Sprint status file    |
| `--story-dir`     | `_bmad-output/implementation-artifacts`                    | Story files directory |
| `--log-file`      | `bmad-automation.log`                                      | Log file path         |

## Requirements

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) with the [Copilot extension](https://github.com/github/gh-copilot)
- A BMAD project with a `_bmad/` directory containing workflow files

## Project Structure

The tool expects a BMAD project structure with:

```
your-project/
├── _bmad/                        # BMAD workflow files
│   ├── core/tasks/workflow.xml   # Workflow engine
│   └── bmm/workflows/4-implementation/
│       ├── create-story/
│       ├── dev-story/
│       ├── code-review/
│       ├── retrospective/
│       └── correct-course/
├── _bmad-output/
│   └── implementation-artifacts/
│       ├── sprint-status.yaml    # Story statuses
│       ├── 3-1-feature-name.md   # Story files
│       └── ...
└── ...
```

## Story Selection Priority

Stories are processed in this order:

1. **review** — Resume interrupted code reviews (skips create + dev steps)
2. **in-progress** — Resume interrupted work first
3. **ready-for-dev** — Stories ready to implement
4. **backlog** — New stories to start

## Smart Behaviors

- **Auto-skip create**: If story file already exists, create-story is skipped
- **Status-aware step skipping**: Stories marked `review` automatically skip create-story and dev-story, going straight to code-review
- **Automatic retrospectives**: After all stories succeed, checks if any epic has all stories done and its retrospective is still `optional` — runs the retrospective workflow automatically
- **Scrum-master course correction**: After a successful retrospective, the scrum master evaluates whether a course correction is needed and executes it if so
- **Retro implementation**: After course correction, a quick dev pass (reusing the dev-story workflow) applies concrete improvements from the retrospective (refactoring, tooling, test coverage, docs) where relevant
- **Workflow-aware prompts**: Each step references the actual BMAD workflow files, so the AI reads and follows the full instructions
- **GitHub Copilot CLI integration**: Uses `gh copilot -p --yolo` for autonomous execution
- **BMAD directory detection**: Validates `_bmad/` exists before execution
- **Graceful interruption**: Ctrl+C shows partial summary before exiting

## Upgrading

```bash
uv tool upgrade bmad-automate
```

## License

MIT
