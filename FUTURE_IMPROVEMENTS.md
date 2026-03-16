# Future Improvements

Ideas and enhancements that are worth pursuing but not yet scheduled.

Both features share foundational infrastructure (event bus, state
management, run control) that should be built once in the core and
consumed by both the CLI and web frontends. The shared components are
described in section 3.

### Implementation order

Section 3 (Shared Infrastructure) must be built first — it replaces the
current `RunContext`, `log_to_file`, and direct `console.print` calls
with thread-safe equivalents that both features depend on. This is a
big-bang refactor of the core internals; the CLI's external behaviour
remains identical.

After section 3, sections 1 and 2 can be built independently or in
parallel. Neither depends on the other at the code level. However,
building parallelisation first is recommended because:

- It exercises the EventBus and RunControl under real concurrency,
  validating the design before the web UI adds a second consumer.
- The web dashboard benefits from showing parallel state, but can launch
  with sequential-only support and add parallel views later.

**Minimum viable slices:**

- **Parallelisation MVP:** Section 3 + Section 1 (without retry
  coordination — use existing simple retry logic, add RetryController
  when the web UI needs interactive retry).
- **Web dashboard MVP:** Section 3 + Section 2 (without parallel worktree
  state or merge queue views — add those after section 1 ships).

---

## 1. Parallelisation

### Parallel Epic Processing

**Problem:** Epics are processed sequentially. A sprint with 3 independent
epics of 4 stories each (~30 minutes per story) takes ~6 hours wall-clock
time, even when the epics have no code overlap and could safely run
concurrently.

**Proposed solution:** Add a `--parallel-epics N` flag that processes up to
*N* epics concurrently. Each epic's stories still run sequentially within
their own **git worktree**, but multiple epics execute in parallel.

### Dependency analysis

BMAD does not use structured YAML fields for epic dependencies. Instead,
dependencies are declared as natural-language comments and ASCII diagrams
in two locations:

1. **`sprint-status.yaml` comments** — inline dependency graphs using
   arrow notation:
   ```yaml
   # Dependency: Epic 20 → 21 [BENCHMARK GATE] → {22, 23, 24} → 25
   #
   # Dependency graph:
   #   Epic 29 (Foundation)
   #     ├── Epic 30 (Device Lifecycle) → Epic 31 (Bulk Ops)
   #     ├── Epic 32 (Manufacturer API) — independent after 29
   #     └── Epic 36 (Fleet Dashboard) → Epic 37 → Epic 38
   ```

2. **Epic breakdown documents** (`_bmad-output/planning-artifacts/`) —
   formal "Dependency Graph" sections and narrative notes like
   "Can run in parallel with Epic 2b after Epic 2a completes."

The tool must parse these to build its dependency graph:

1. **Primary source:** Scan `sprint-status.yaml` comments for dependency
   declarations. Parse arrow notation (`→`, `──→`), set notation
   (`{22, 23, 24}` for parallel groups), and gate markers
   (`[BENCHMARK GATE]`).
2. **Fallback:** If no dependency comments are found in sprint-status,
   scan epic breakdown documents in the planning artifacts directory for
   "Dependency Graph" sections.
3. **AI-assisted parsing:** Since the format is natural-language, use a
   lightweight regex parser for the common patterns (arrow chains, set
   notation) and fall back to prompting the configured AI CLI to extract
   a structured dependency list from ambiguous text.
4. **Manual override:** Support an optional structured block in
   `sprint-status.yaml` for users who want machine-readable declarations:
   ```yaml
   epic_dependencies:
     5: [4]           # Epic 5 depends on Epic 4
     6: [4, 5]        # Epic 6 depends on 4 and 5
     22: [21]         # gate dependency
     23: [21]         # parallel with 22 after 21
   ```
   When this block exists, it takes precedence over comment parsing.
5. **Build a directed acyclic graph** from the parsed dependencies.
   Validate that the graph is acyclic — if cycles are detected, report
   them and refuse to parallelise (fall back to sequential).
6. **Surface the analysis** in both `--dry-run` output and the
   interactive Y/n confirmation prompt. Display which epics will run in
   parallel, which are sequentially gated, and any unresolved
   dependencies. The user must confirm before execution begins.

### Design sketch

1. For each independent epic, create a worktree branching from the current
   HEAD (`git worktree add .bmad-worktrees/epic-<N> -b auto/epic-<N>`).
2. Spawn one worker per epic using `concurrent.futures.ThreadPoolExecutor`.
   Each worker processes its epic's stories sequentially inside its
   worktree directory (`cwd=` arg to `subprocess.run`).
3. On epic completion, enqueue a merge request to the `MergeQueue`
   (see below). The orchestrator drains the queue one at a time:
   fast-forward merge (`git merge --ff-only auto/epic-<N>`), falling back
   to a regular merge with AI-assisted conflict resolution if needed.
4. Run the after-epic pipeline (retro, course-correct, etc.) only after a
   given epic's merge completes — not while other epics are still running
   in that worktree.
5. Clean up worktrees after merging (`git worktree remove`).

### Sprint-status state management

Each worktree has its own copy of `sprint-status.yaml`, but the source of
truth is an in-memory state map owned by the orchestrator (main thread).
Story status follows a one-way state machine (`backlog → ready-for-dev →
in-progress → review → done`) so merging is deterministic — state can
only move forward.

- Workers call `mark_story_done()` in **worktree mode**: writes to the
  local worktree's `sprint-status.yaml` only, does not touch the shared
  YAML cache, does not broadcast. This keeps the AI CLI's local view
  consistent.
- Workers also emit a `(story_key, new_status)` event via the `EventBus`
  (see section 3).
- The orchestrator consumes these events on the main thread, advancing
  state only forward (ignoring stale/duplicate messages), and flushes the
  authoritative YAML to the main branch's `sprint-status.yaml`
  periodically and before each merge.
- At merge time, the worktree's copy of `sprint-status.yaml` is
  overwritten by the orchestrator's authoritative version using
  `git checkout --ours sprint-status.yaml` during the merge — no YAML
  merge conflict is possible.
- The YAML cache (currently in `stories.py`) is not used by workers in
  parallel mode. Each worker reads its local file directly. The cache is
  only used by the orchestrator for sequential-mode backwards
  compatibility.

### Merge queue

Merges back into main are inherently serial. A `MergeQueue` makes this
explicit and visible:

- When an epic's worktree finishes, it enqueues a merge request.
- The orchestrator processes the queue one at a time: merge, run
  after-epic pipeline, clean up worktree, then take the next entry.
- The web dashboard shows the queue: "Epic 3: merging", "Epic 5: waiting
  to merge (position 2)".
- The CLI shows it inline:
  `Epic 5 complete — queued for merge (1 ahead)`.

**Merge failure handling:**

- If AI-assisted conflict resolution fails after retries, the merge is
  marked as failed. The worktree is **not** cleaned up — it is left in
  place for manual inspection.
- The failed epic is removed from the queue. Subsequent epics continue
  to merge independently (their worktrees branched from the same base,
  so another epic's failure does not block them).
- If the after-epic pipeline (retro, course-correct) fails after a
  successful merge, the merge stands. The pipeline failure is logged and
  reported in the summary, but the worktree is cleaned up since the code
  merge itself succeeded.
- If the user aborts mid-merge (Ctrl+C / abort button), `git merge
  --abort` is run to restore the main branch to a clean state. The
  worktree is left in place. The remaining queue is drained without
  processing (all enqueued merges are skipped and reported as aborted).

### Resumability

When the automation is interrupted or fails, worktrees may be left in
place. On the next run, the tool must detect and handle leftover state:

1. **Detect existing worktrees:** On startup, scan `.bmad-worktrees/` for
   directories matching the `epic-<N>` pattern.
2. **Assess worktree state:** For each found worktree, check:
   - Are there uncommitted changes? (`git status --porcelain`)
   - What was the last completed story? (read the worktree's
     `sprint-status.yaml`)
   - Is the worktree branch ahead of main? (`git log main..HEAD`)
3. **Present recovery options:** Show the user what was found and offer:
   - **Resume:** Continue processing from the last completed story in
     each worktree. Re-queue finished worktrees for merge.
   - **Discard:** Remove all leftover worktrees and start fresh.
   - **Merge only:** Merge any completed worktrees without running
     further stories.
4. **Persist run state:** Write a `.bmad-worktrees/run-state.json` file
   that records the run configuration, which epics are assigned to which
   worktrees, and the last completed story per epic. This file is updated
   after each story completes, so recovery has precise information.

### Rate limiting

AI CLI providers enforce rate limits (requests per minute, tokens per
minute, concurrent sessions) that parallel runs will hit sooner than
sequential mode.

- **Detection:** Parse stderr / exit codes from the AI CLI for rate-limit
  signals (HTTP 429, "rate limit exceeded", "too many requests",
  provider-specific error patterns).
- **Back-off strategy:** On rate-limit detection, pause the affected
  worker with exponential back-off (e.g., 30s → 60s → 120s → 240s, capped
  at ~5 minutes). Other workers on different API keys or providers can
  continue.
- **Concurrency throttle:** A shared `threading.BoundedSemaphore` limits
  the number of *active* AI CLI sessions across all workers. Start
  conservatively (e.g., 2) and let users tune via `--parallel-epics`.
  The semaphore count should be dynamically adjustable — the web UI can
  expose a slider to reduce concurrency mid-run without pausing/aborting.
- **Semaphore and pause coordination:** Each worker's step loop acquires
  the semaphore *after* checking the pause flag (see section 3,
  RunControl). A paused worker never holds a semaphore slot; when
  unpaused it competes fairly with other workers for the next slot.
- **Per-provider limits:** Different providers have different ceilings.
  Claude CLI and GitHub Copilot may have entirely different rate-limit
  profiles. The throttle should be provider-aware.
- **Graceful degradation:** If rate limits persist beyond the max back-off
  window, pause all parallel workers and fall back to sequential mode for
  the remainder of the run, logging a warning.
- **Dry-run estimate:** In `--dry-run` mode, estimate the expected API
  load (stories × steps × avg tokens) and warn if it is likely to exceed
  known rate limits.

### Retry coordination

All retries — automatic back-off and manual UI retry — go through a
single `RetryController` per step execution.

**Lifecycle:**

1. The worker creates a `RetryController` when a step is about to
   execute.
2. If the step succeeds, the controller is discarded immediately.
3. If the step fails, the controller enters back-off state and is
   registered in a shared `retry_registry: dict[tuple[int, str, str],
   RetryController]` keyed by `(epic, story, step)`.
4. The controller auto-expires after `max_retries` exhausted (configurable
   via `--retries`). On expiry, the step is marked as failed and the
   controller is removed from the registry.
5. If the user triggers a manual retry (web UI) or the back-off timer
   fires, the controller re-executes the step and returns to state 2 or 3.

**Coordination rules:**

- On failure, the controller enters back-off state with a countdown.
- The web UI shows the countdown: "Retrying in 45s... [Retry Now] [Skip]".
- "Retry Now" cancels the back-off timer and immediately re-executes.
- "Skip" marks the step as failed, removes the controller from the
  registry, and the worker moves on.
- The CLI equivalent: automatic back-off proceeds without intervention.
- There is only ever one pending retry per step — the manual button
  accelerates the existing retry, it does not queue a second attempt.

### Other considerations

- **Progress UI:** The Rich progress bar would need to show multiple
  concurrent epic tracks instead of a single sequential bar.
- **Resource usage:** Each AI CLI session consumes significant memory and
  API tokens. The `--parallel-epics` flag should default to 1 (sequential)
  so users opt in explicitly.
- **Logging:** Each worker writes log lines to the shared `LogBroker`
  (see section 3). Per-epic log files
  (`bmad-automation-epic-<N>.log`) are a secondary sink. The final summary
  references all of them.
- **Failure isolation:** A failure in one epic should not kill workers for
  other independent epics. The failed epic's worktree is left in place for
  debugging; other epics continue to completion.

### Acceptance criteria

- With 3 independent epics (`--parallel-epics 3`), total wall-clock time
  is under 40% of sequential time for the same workload.
- Dependent epics (A → B) never start B before A's merge completes.
- A rate-limited step retries with exponential back-off and eventually
  succeeds (or exhausts retries and fails cleanly).
- Interrupting a parallel run (Ctrl+C) leaves worktrees in a resumable
  state. Re-running with the same flags offers to resume.
- `--dry-run` displays the dependency graph, parallel schedule, and
  estimated API load.
- The Y/n confirmation prompt shows which epics run in parallel and
  which are gated.
- A failed merge in one epic does not block merges for other epics.
- `sprint-status.yaml` on the main branch is always consistent — never
  contains conflicting or stale status values.

---

## 2. Web Dashboard

### Overview

A browser-based GUI that wraps the CLI, providing visual project
management, real-time run monitoring, and interactive control over the
automation pipeline.

**Technology:** FastAPI (backend) + htmx (frontend). This keeps the
frontend lightweight — no React/Vue build chain — while giving full layout
power for dashboards, tables, and real-time updates. The server also
doubles as an API surface for future integrations (CI triggers, webhooks,
etc.).

### Core features

#### Project setup
- **Folder picker:** Select or drag-and-drop a project directory. The UI
  validates that `_bmad/` and `sprint-status.yaml` exist before
  proceeding.
- **Configuration panel:** Visual equivalents of all CLI flags (AI
  provider, timeouts, retries, skip/only steps, epic selection). Settings
  are persisted per-project so they don't need to be re-entered.

#### Plan & confirmation
- **Sprint overview:** Parse and display `sprint-status.yaml` as a
  visual board — columns for backlog, ready-for-dev, in-progress, review,
  done. Each story is a card showing its epic, key, and current status.
- **Run plan:** Before execution, show the ordered list of stories and
  steps that will run (equivalent to `--dry-run`), including the
  dependency analysis for parallel epics if enabled.
- **One-click start:** Replace the Y/n terminal prompt with a confirmation
  modal that shows the full plan.

#### Live execution monitoring
- **Real-time log streaming:** The web UI consumes the `LogBroker`
  (see section 3) via SSE. Each story gets its own collapsible log panel
  so users can watch any active story without losing context on others.
- **Progress dashboard:** Show a live view of all stories grouped by epic,
  with per-step status indicators (pending / running / success / failed /
  skipped) and elapsed time.
- **Parallel worktree state:** When parallel epic processing is enabled,
  the dashboard reads from the orchestrator's in-memory state map (see
  section 1, Sprint-status state management) rather than reading YAML
  files from individual worktrees. The state map is the single source
  of truth for all display. State priority for display: failed >
  in-progress > review > ready-for-dev > done > backlog.
- **Merge queue visibility:** Show the merge queue status when parallel
  epics are active — which epic is merging, which are waiting, and
  position in the queue.

#### Interactive run control

All controls operate through the shared `RunControl` (see section 3).
When parallel epics are active, each control can target a specific epic
or all epics globally.

- **Pause after this step:** Halt execution after the current step
  completes, allowing the user to inspect results before continuing.
  Targets a specific epic.
- **Pause after this story:** Let the current story finish all its steps,
  then pause before starting the next story. Targets a specific epic.
- **Pause all:** Global pause across all active epic workers.
- **Skip step:** Skip a specific upcoming step for the current story
  mid-run (e.g., skip code-review for a trivial change).
- **Retry failed step:** Re-run a failed step without restarting the
  entire story. Coordinates with the `RetryController` (see section 1,
  Retry coordination) — if a back-off countdown is active, the button
  cancels it and retries immediately.
- **Concurrency slider:** Dynamically adjust the rate-limit semaphore
  count mid-run without pausing or aborting.
- **Abort:** Graceful stop equivalent to Ctrl+C — finishes the current
  operation, then shows the summary.

#### Run history
- **Past runs:** Display a table of previous runs parsed from log files,
  showing date, duration, stories processed, success rate, and failures.
- **Pattern detection:** Highlight stories or steps that fail repeatedly
  across runs (e.g., "story 3-4 has failed at code-review in 3 of the
  last 5 runs").
- **Log viewer:** Click into any past run to view its full log output,
  filterable by story and step.

### API contract

All endpoints are prefixed with `/api/v1/`. Payloads are JSON.

#### `GET /api/v1/status`

Returns the current orchestrator state.

```json
{
  "state": "running",           // "idle" | "running" | "paused" | "finished"
  "epics": {
    "3": {
      "state": "running",       // "pending" | "running" | "paused" | "merging"
                                //   | "done" | "failed"
      "current_story": "3-2-feature",
      "current_step": "dev-story",
      "stories": {
        "3-1-setup": {"status": "done", "duration": 1234.5},
        "3-2-feature": {"status": "in-progress", "step": "dev-story"},
        "3-3-api": {"status": "ready-for-dev"}
      }
    }
  },
  "merge_queue": ["5", "4"],
  "elapsed": 3456.7,
  "rate_limit": {
    "semaphore_total": 3,
    "semaphore_available": 1
  }
}
```

#### `POST /api/v1/run`

Start a new automation run.

```json
// Request
{
  "project_dir": "/path/to/project",
  "epic": [3, 4, 5],
  "limit": 0,
  "parallel_epics": 2,
  "skip_steps": ["pull"],
  "only_steps": null,
  "ai_provider": "claude",
  "retries": 1,
  "timeout": 3600
}

// Response
{
  "run_id": "run-20260316-143022",
  "status": "started",
  "stories": ["3-1-setup", "3-2-feature", "4-1-api"]
}
```

Returns `409 Conflict` if a run is already active.

#### `POST /api/v1/control`

Send control commands to a running automation.

```json
// Pause a specific epic after current story
{"action": "pause_after_story", "epic": 3}

// Resume a paused epic
{"action": "resume", "epic": 3}

// Pause all epics after current step
{"action": "pause_after_step", "epic": null}

// Skip a step for the current story in an epic
{"action": "skip_step", "epic": 3, "step": "code-review"}

// Retry a failed step
{"action": "retry", "epic": 3, "story": "3-2-feature",
 "step": "dev-story"}

// Adjust concurrency
{"action": "set_concurrency", "value": 2}

// Abort the entire run
{"action": "abort"}
```

Response: `{"accepted": true}` or `{"accepted": false, "reason": "..."}`.

#### `GET /api/v1/history`

List past runs.

```json
{
  "runs": [
    {
      "run_id": "run-20260316-143022",
      "started": "2026-03-16T14:30:22Z",
      "duration": 5432.1,
      "stories_total": 8,
      "stories_completed": 7,
      "stories_failed": 1,
      "log_files": ["bmad-automation-epic-3.log"]
    }
  ]
}
```

#### `GET /api/v1/logs/{run_id}` (SSE)

Streams log events as Server-Sent Events. Each event:

```
event: log
data: {"epic": 3, "story": "3-2-feature", "step": "dev-story",
       "level": "stdout", "line": "Running tests...",
       "timestamp": "2026-03-16T14:35:12Z"}

event: state
data: {"epic": 3, "story": "3-2-feature", "step": "dev-story",
       "kind": "step_done", "duration": 342.1}
```

Query parameters for filtering:
- `?epic=3` — only events for epic 3
- `?story=3-2-feature` — only events for a specific story
- `?cursor=1742` — resume from a specific position in the ring buffer

### Architecture

```
bmad-automate serve [--port 8080] [--project-dir .]
       |
       +-- FastAPI app (uvicorn, async)
       |     +-- /api/v1/status    -- orchestrator state map as JSON
       |     +-- /api/v1/run       -- start a run (409 if already active)
       |     +-- /api/v1/control   -- per-epic pause/resume/skip/retry
       |     +-- /api/v1/history   -- past run summaries
       |     +-- /api/v1/logs/{id} -- SSE stream from LogBroker
       |     +-- /                 -- serves htmx templates
       |
       +-- htmx templates (Jinja2)
       |     +-- dashboard.html    -- main board view
       |     +-- run.html          -- active run monitoring + merge queue
       |     +-- history.html      -- past run browser
       |
       +-- Runner (background thread)
             +-- Reuses pipeline.py / git.py / stories.py
             +-- Produces events to EventBus
             +-- Reads commands from RunControl
```

The backend reuses the existing CLI modules directly — `pipeline.py`,
`git.py`, `stories.py` — with the `EventBus` for state propagation.
The CLI and web UI are two frontends over the same core logic.

### Process coordination

When `bmad-automate serve` is running, it acquires an exclusive file lock
on `.bmad-automate.lock` (using `msvcrt.locking` on Windows,
`fcntl.flock` on Unix). The lock file contains the PID and port.

The CLI checks for this lock on startup:

1. **Try to acquire the lock.** If it succeeds, no server is running —
   release the lock and run directly as today.
2. **If the lock is held**, read the PID and port from the file.
3. **Validate the PID** is still alive (`os.kill(pid, 0)` or
   `psutil.pid_exists`). If the process is dead, the lock is stale —
   delete the file, acquire the lock, and run directly.
4. **If the server is alive**, the CLI becomes a thin client: submit the
   run via `POST /api/v1/run` and stream results via SSE from
   `/api/v1/logs/{run_id}`, rendering them as Rich output in the
   terminal.

This eliminates the TOCTOU race — the file lock is atomic on both
platforms. There is never two processes managing worktrees or writing to
`sprint-status.yaml`.

The server releases the lock and cleans up the lock file on shutdown
(via `atexit` and signal handlers).

### Considerations

- **CLI parity:** The web UI should never have features the CLI can't
  access. Both are frontends to the same pipeline. Any new capability
  (e.g., pause-after-step) should be implemented in the core and exposed
  to both interfaces.
- **Single-user focus:** Initially, the server is designed for local
  single-user use (localhost only). Multi-user auth and shared access can
  be added later if needed.
- **Startup ergonomics:** `bmad-automate serve` launches the server and
  opens the browser automatically. The existing `bmad-automate` command
  remains the CLI entry point — no breaking changes.
- **Dependencies:** FastAPI, uvicorn, Jinja2, and htmx are added as
  optional extras (`pip install bmad-automate[web]`) so the CLI stays
  lightweight for users who don't need the GUI.

### Acceptance criteria

- The dashboard displays the sprint board from `sprint-status.yaml` with
  correct story statuses and epic grouping.
- Starting a run from the web UI produces identical results to running
  the equivalent CLI command.
- Log streaming shows AI CLI output within 1 second of it being produced.
- Pause-after-step halts the worker within 5 seconds of the step
  completing.
- Retry-now on a back-off countdown re-executes the step within 2 seconds
  of clicking the button.
- The concurrency slider takes effect before the next step starts (no
  in-flight steps are affected).
- Running the CLI while the server is active delegates to the server
  transparently — the user sees the same Rich output.
- The server handles browser disconnects gracefully (SSE consumers are
  removed, no orphaned state).
- Run history correctly lists all past runs with accurate stats.

### Implementation notes

- **SSE reconnection:** Use the browser's native `EventSource`
  auto-reconnect (default 3-second retry). The server includes an
  incrementing `id:` field on every SSE event corresponding to the ring
  buffer cursor position. On reconnect, the browser sends
  `Last-Event-ID`, and the server resumes from that cursor. If the cursor
  has been wrapped past (gap), the server sends a single `event: gap`
  with the number of missed entries, then resumes from the current
  buffer head. The client displays a "some log lines were skipped"
  notice. No custom retry logic needed on the client side.
- **htmx template design:** Use a single-page layout with 3 tab panels
  (Dashboard, Run, History) rather than 3 separate HTML pages. This
  avoids full page reloads and keeps SSE connections alive during tab
  switches. Each panel is an htmx fragment loaded via `hx-get` on tab
  click. The Dashboard tab is the default view. The Run tab auto-activates
  when a run starts. Use a minimal CSS framework (Pico CSS or similar)
  for baseline styling — no custom CSS beyond layout overrides.

---

## 3. Shared Infrastructure

Both features above depend on core infrastructure that should be built
once and consumed by the CLI, the parallelisation engine, and the web
dashboard. These components replace the current simple globals and direct
file I/O with thread-safe, multi-consumer equivalents.

### EventBus

A tagged event channel that decouples pipeline execution from output
rendering.

```python
@dataclass
class PipelineEvent:
    epic: int
    story: str | None    # None for epic-level events
    step: str | None
    kind: str            # "step_start", "step_done", "step_failed",
                         # "story_done", "paused", "rate_limited",
                         # "status_change", ...
    payload: dict        # duration, error, retry_countdown,
                         # new_status, etc.
    timestamp: float
```

- Workers (one per epic in parallel mode, or a single worker in
  sequential mode) only *produce* events onto a `queue.Queue`.
- The orchestrator's main thread *drains* the queue and fans out to
  registered consumers.
- **CLI consumer:** Prints Rich output (current behaviour). Replaces
  direct `console.print` calls inside pipeline functions.
- **Web consumer:** Pushes events as SSE to connected browsers.
- The bus is a simple `queue.Queue` (thread-safe by default). Workers
  never consume events — no shared state, no locking beyond the queue.

This interface should be implemented in the parallelisation PR even though
the CLI initially has only one producer. The web dashboard then adds a
second consumer with zero changes to the core.

### RunControl

Replaces the single `RunContext.interrupted` boolean with per-epic
granularity.

```python
@dataclass
class RunControl:
    global_abort: bool = False
    pause_after_step: dict[int, bool]    # epic_num -> flag
    pause_after_story: dict[int, bool]   # epic_num -> flag
    epic_paused: dict[int, Event]        # epic_num -> threading.Event
```

- Each worker checks its own epic's flags between steps and between
  stories.
- `threading.Event` lets workers block until unpaused without
  busy-waiting.
- `global_abort` remains for Ctrl+C / the web abort button.
- The CLI sets flags globally (all epics). The web UI can target
  individual epics.
- Sequential mode (single epic) works identically — the dict just has
  one entry.

### LogBroker

Replaces direct file logging with a thread-safe, multi-sink channel.

- Workers write log lines to the `LogBroker`, tagged with
  `(epic, story, step, timestamp)`.
- The broker fans out to two sinks:
  1. **File writer:** Appends to per-epic log files
     (`bmad-automation-epic-<N>.log`). In sequential mode, writes to
     the single `bmad-automation.log` as today.
  2. **Ring buffer:** An in-memory ring buffer (fixed capacity, e.g.,
     50,000 entries) that SSE consumers read from. Each consumer
     registers a cursor position and reads forward. When the buffer
     wraps, old entries are discarded — consumers that fall behind
     receive a `gap` event and can fall back to the file sink.
- The CLI `--verbose` output and file logging both consume from the same
  broker.
- No file I/O for SSE consumers — eliminates file contention on Windows
  where file locking is mandatory.

### Acceptance criteria

- The EventBus handles 1,000 events/second from multiple producers
  without dropping events or blocking workers.
- RunControl pause flags take effect between steps (not mid-step) and
  resume within 1 second of the flag being cleared.
- LogBroker file output is identical to the current `log_to_file`
  behaviour (timestamps, format) for backwards compatibility.
- LogBroker ring buffer supports multiple concurrent SSE readers at
  different cursor positions without locking or data corruption.
- Sequential mode (single epic, no web UI) has no measurable performance
  regression compared to the current implementation.

### Implementation notes

- **Ring buffer sizing:** Use 10,000 entries as the default. A typical
  story produces ~50 log lines across 5 steps; a 10-epic parallel run
  with 5 stories each produces ~2,500 entries. 10,000 gives ~4x headroom
  for verbose runs. Expose as `BMAD_LOG_BUFFER_SIZE` environment variable
  for power users. Do not over-allocate — the file sink is the durable
  store, the ring buffer only serves live SSE consumers.
- **Run history storage:** Use a `runs.json` index file in the project
  root, appended after each run completes. Each entry stores the run ID,
  timestamps, story counts, and log file paths. The `/api/v1/history`
  endpoint reads this file directly. This avoids expensive log parsing on
  every request. The file is append-only JSON Lines (one JSON object per
  line) for safe concurrent writes and easy parsing.
- **`epic_dependencies:` validation:** On parse, validate that all keys
  and values are positive integers, all referenced epics exist in
  `sprint-status.yaml`, and the graph is acyclic (topological sort). On
  any validation failure, print the specific error (e.g., "Epic 5
  references non-existent epic 99", "Cycle detected: 3 → 4 → 3") and
  refuse to parallelise — fall back to sequential with a warning. Do not
  abort the run entirely.
