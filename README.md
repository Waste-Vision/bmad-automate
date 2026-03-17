# BMAD Automate

Automated [BMAD](https://github.com/bmad-code-org/BMAD-METHOD) Workflow Orchestrator — powered by AI CLI providers (Claude CLI or GitHub Copilot CLI).

Processes stories through a complete development cycle:

```text
create-story -> dev-story -> code-review -> git-commit -> git-pull
```

For each step, the tool builds a plain-English prompt that tells the AI to read
the BMAD workflow engine and execute the corresponding workflow files from
the project's `_bmad/` directory.

## Prerequisites

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) (default), or
  [GitHub CLI](https://cli.github.com/) with the Copilot extension:
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
pip install -e ".[dev]"
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

# Process stories in epics 3 and 4
bmad-automate --epic 3,4

# Process specific story
bmad-automate 3-3-account-translation

# Non-interactive with verbose output
bmad-automate --yes --verbose --limit 5

# Run only code-review and commit steps
bmad-automate --only review,commit

# Run after-epic pipeline (retro, course-correct, etc.) for epic 3
bmad-automate --after-epic 3

# Use GitHub Copilot instead of Claude
bmad-automate --ai-provider github

# Process 3 epics in parallel (each in its own git worktree)
bmad-automate --epic 3,4,5 --parallel-epics 3
```

### Web Dashboard

The web dashboard is a browser-based GUI included with the CLI. Start it from your BMAD project directory:

```bash
# Start on default port 8080 — opens your browser automatically
bmad-automate serve

# Custom port
bmad-automate serve --port 9090

# Specify a different project directory
bmad-automate serve --project-dir /path/to/project
```

Once running, open **http://localhost:8080** in your browser (it opens automatically on launch). The UI has three tabs:

#### Dashboard Tab
Displays your `sprint-status.yaml` as a visual **sprint board** with columns for Backlog, Ready for Dev, In Progress, Review, and Done. Each story appears as a card showing its epic and current status. The board refreshes automatically every 5 seconds.

#### Run Tab
Start and monitor automation runs with real-time feedback:

- **Start a run** by posting to the API (or use the CLI while the server is running — it auto-delegates to the server)
- **Live log streaming** — AI CLI output appears within 1 second via Server-Sent Events (SSE). Each story gets its own log stream
- **Pause All** — halt all epic workers after their current step completes
- **Abort** — graceful stop equivalent to Ctrl+C
- **Concurrency slider** — dynamically adjust how many epics run in parallel (takes effect before the next step starts)
- **Merge queue visibility** — when parallel epics are active, shows which epic is merging and which are waiting

#### History Tab
Browse past runs with date, duration, stories processed, success rate, and failures. Click into any run to view its full log output.

#### CLI + Server Coordination
When the server is running, the CLI automatically detects it via a lock file (`.bmad-automate.lock`). If you run `bmad-automate --epic 3` while the server is active, the CLI delegates the run to the server and streams results back to your terminal — you get the same Rich output as usual.

#### API Endpoints
The server also exposes a JSON API at `/api/v1/` for integrations:

| Endpoint                  | Method | Description                              |
| ------------------------- | ------ | ---------------------------------------- |
| `/api/v1/status`          | GET    | Current orchestrator state               |
| `/api/v1/run`             | POST   | Start a new automation run               |
| `/api/v1/control`         | POST   | Pause/resume/skip/retry/abort commands   |
| `/api/v1/history`         | GET    | List past runs                           |
| `/api/v1/logs/{run_id}`   | GET    | SSE stream of log events                 |

## Options

### General Options

| Flag                      | Description                              |
| ------------------------- | ---------------------------------------- |
| `-n, --dry-run`           | Preview what would run without executing |
| `-y, --yes`               | Skip interactive confirmation prompt     |
| `-v, --verbose`           | Show full AI output during execution     |
| `-q, --quiet`             | Minimal output (only errors and summary) |
| `--notify / --no-notify`  | Desktop notifications on completion      |

### AI Provider

| Flag             | Default  | Description                                     |
| ---------------- | -------- | ----------------------------------------------- |
| `--ai-provider`  | `claude` | `claude` (Claude CLI) or `github` (Copilot CLI) |
| `--bmad-dir`     | `_bmad`  | Path to the `_bmad/` directory                  |

### Story Selection

| Flag               | Description                               |
| ------------------ | ----------------------------------------- |
| `--epic N`         | Only process stories for epic(s) N        |
| `--limit N`        | Process at most N stories (0 = unlimited) |
| `--start-from KEY` | Resume from specific story key            |
| `[stories...]`     | Specific story keys to process            |

### Step Control

| Flag                      | Description                                                       |
| ------------------------- | ----------------------------------------------------------------- |
| `--only STEPS`            | Run only these steps (comma-separated: create,dev,review,commit,pull) |
| `--skip-create`           | Skip create-story step                                            |
| `--skip-dev`              | Skip dev-story step                                               |
| `--skip-review`           | Skip code-review step                                             |
| `--skip-commit`           | Skip git commit step                                              |
| `--skip-pull`             | Skip git pull/merge step                                          |
| `--skip-retro`            | Skip automatic retrospective after completing an epic             |
| `--skip-course-correct`   | Skip scrum-master course correction                               |
| `--skip-retro-impl`       | Skip implementing retrospective learnings                         |
| `--skip-next-epic-prep`   | Skip preparation for the next epic                                |
| `--after-epic N`          | Explicitly run after-epic pipeline for epic(s) N                  |

### Parallelisation

| Flag                | Default | Description                                          |
| ------------------- | ------- | ---------------------------------------------------- |
| `--parallel-epics N`| 1       | Process up to N epics concurrently in git worktrees  |

When `--parallel-epics` is greater than 1:
- Each epic runs in its own git worktree (`.bmad-worktrees/epic-<N>`)
- Stories within each epic still run sequentially
- Completed epics are merged back to main via a serial merge queue
- Failed worktrees are preserved for inspection
- Dependency analysis from `sprint-status.yaml` comments or structured `epic_dependencies:` block

### Retry & Timeout

| Flag          | Default | Description                 |
| ------------- | ------- | --------------------------- |
| `--retries N` | 1       | Retries per step on failure |
| `--timeout N` | 3600    | Timeout per step in seconds |

### Paths

| Flag              | Default                                                    | Description           |
| ----------------- | ---------------------------------------------------------- | --------------------- |
| `--sprint-status` | `_bmad-output/implementation-artifacts/sprint-status.yaml` | Sprint status file    |
| `--story-dir`     | `_bmad-output/implementation-artifacts`                    | Story files directory |
| `--log-file`      | `bmad-automation.log`                                      | Log file path         |

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

## Architecture

```
src/bmad_automate/
├── cli.py               # Typer CLI entry point
├── context.py            # RunContext (EventBus + RunControl + LogBroker)
├── models.py             # Config, enums, constants
├── stories.py            # YAML parsing and story filtering
├── git.py                # Subprocess helpers, git operations
├── pipeline.py           # Story/epic processing orchestration
├── ui.py                 # Terminal UI, summaries, notifications
├── events.py             # EventBus — decouples execution from output
├── consumers.py          # CliConsumer — renders events as Rich output
├── logging.py            # LogBroker — thread-safe multi-sink logging
├── control.py            # RunControl — per-epic pause/resume/abort
├── orchestrator.py       # Parallel epic orchestrator + StatusManager
├── worker.py             # EpicWorker — per-epic story processing
├── worktree.py           # Git worktree management
├── merge_queue.py        # Serial merge queue for parallel epics
├── dependencies.py       # Epic dependency DAG and parsing
├── rate_limit.py         # Concurrency throttling and backoff
└── web/                  # Optional web dashboard
    ├── app.py            # FastAPI backend
    ├── lock.py           # Server process coordination
    ├── templates/        # Jinja2/htmx templates
    └── static/           # Frontend assets
```

## Story Selection Priority

Stories are processed in this order:

1. **review** — Resume interrupted code reviews (skips create + dev steps)
2. **in-progress** — Resume interrupted work first
3. **ready-for-dev** — Stories ready to implement
4. **backlog** — New stories to start

## Smart Behaviors

- **Auto-skip create**: If story file already exists, create-story is skipped
- **Status-aware step skipping**: Stories marked `review` automatically skip create-story and dev-story
- **Automatic retrospectives**: When all stories in an epic are done, runs the retrospective pipeline automatically
- **After-epic pipeline**: retrospective -> course-correction -> retro-implementation -> next-epic-preparation -> commit & push
- **Parallel epic processing**: Independent epics run concurrently in git worktrees with serial merge-back
- **Dependency-aware scheduling**: Parses epic dependencies from YAML comments or structured blocks
- **Rate limiting**: Exponential backoff on API rate limits with graceful degradation to sequential mode
- **Event-driven architecture**: EventBus decouples pipeline execution from output rendering
- **Graceful interruption**: Ctrl+C shows partial summary; parallel worktrees are preserved for resumption
- **Web dashboard**: Real-time monitoring with pause/resume/skip/retry controls

## Development

```bash
# Run tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=bmad_automate
```

## Upgrading

```bash
uv tool upgrade bmad-automate
```

## License

MIT
