"""FastAPI web dashboard backend."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bmad_automate.context import RunContext
from bmad_automate.control import RunControl
from bmad_automate.events import (
    STEP_DONE,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_START,
    STORY_DONE,
    STORY_START,
    EventBus,
    PipelineEvent,
)
from bmad_automate.logging import LogBroker
from bmad_automate.models import AI_PROVIDERS, Config, StoryStatus

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _make_event_to_log_bridge(log_broker: LogBroker):
    """Return an EventBus subscriber that writes pipeline events to the LogBroker."""

    def _bridge(event: PipelineEvent) -> None:
        from bmad_automate.logging import LogEntry

        kind = event.kind
        epic = event.epic
        story = event.story
        step = event.step

        def _write(msg: str, level: str = "info") -> None:
            entry = LogEntry(
                epic=epic, story=story, step=step,
                level=level, line=msg, event_kind=kind,
            )
            log_broker.write(entry)

        if kind == STEP_START:
            _write(f"Starting {step}")
        elif kind == STEP_DONE:
            duration = event.payload.get("duration", 0)
            _write(
                f"Step {step} completed ({duration:.1f}s)" if duration else f"Step {step} completed",
                level="success",
            )
        elif kind == STEP_FAILED:
            error = event.payload.get("error", "")
            _write(
                f"Step {step} failed: {error}" if error else f"Step {step} failed",
                level="error",
            )
        elif kind == STEP_SKIPPED:
            msg = event.payload.get("message", f"Skipping {step}")
            _write(msg)
        elif kind == STORY_START:
            _write(f"Starting story {story}")
        elif kind == STORY_DONE:
            status = event.payload.get("status", "unknown")
            _write(
                f"Story {story} {status}",
                level="success" if status == "completed" else "error",
            )
        elif kind in ("log_line", "log_message"):
            _write(
                event.payload.get("line", str(event.payload)),
                level=event.payload.get("level", "info"),
            )

    return _bridge


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    project_dir: str = "."
    epic: list[int] = []
    limit: int = 0
    parallel_epics: int = 1
    skip_steps: list[str] = []
    only_steps: list[str] | None = None
    ai_provider: str = "claude"
    retries: int = 1
    timeout: int = 3600
    specific_stories: list[str] = []
    start_from: str = ""
    dry_run: bool = False
    after_epic: list[int] = []
    skip_retro: bool = False
    skip_course_correct: bool = False
    skip_retro_impl: bool = False
    skip_next_epic_prep: bool = False


class ControlRequest(BaseModel):
    action: str
    epic: int | None = None
    story: str | None = None
    step: str | None = None
    value: int | None = None


class ControlResponse(BaseModel):
    accepted: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Run manager — owns the orchestrator lifecycle
# ---------------------------------------------------------------------------

class RunManager:
    """Manages the automation run lifecycle for the web server."""

    def __init__(self) -> None:
        self.ctx: RunContext | None = None
        self.run_id: str | None = None
        self.run_thread: threading.Thread | None = None
        self._state: str = "idle"  # idle, running, paused, finished
        self._stories_processed: list[str] = []
        self._error: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in ("running", "paused")

    def start(
        self,
        config: Config,
        log_broker: LogBroker,
        run_control: RunControl,
        event_bus: EventBus,
    ) -> str:
        """Build context and start the run in a background thread."""
        from bmad_automate.context import set_active_context
        from bmad_automate.control import set_active_control
        from bmad_automate.orchestrator import Orchestrator
        from bmad_automate.stories import (
            filter_stories,
            get_actionable_stories,
            invalidate_cache,
        )

        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}"
        self.run_id = run_id
        self._state = "running"
        self._stories_processed = []
        self._error = None

        # Build run context
        ctx = RunContext(
            config=config,
            event_bus=event_bus,
            log_broker=log_broker,
            run_control=run_control,
        )
        self.ctx = ctx
        set_active_context(ctx)
        set_active_control(run_control)

        # Bridge pipeline events to the log broker for SSE streaming
        event_bus.subscribe(_make_event_to_log_bridge(log_broker))

        # Get stories
        invalidate_cache()
        stories_by_status = get_actionable_stories(config)
        story_status_map: dict[str, str] = {}
        for status, keys in stories_by_status.items():
            for key in keys:
                story_status_map[key] = status
        filtered = filter_stories(stories_by_status, config)
        self._stories_processed = list(filtered)

        if not filtered:
            self._state = "finished"
            return run_id

        def _run() -> None:
            try:
                ctx.start_time = time.time()
                orch = Orchestrator(
                    stories=filtered,
                    story_status_map=story_status_map,
                    config=config,
                    ctx=ctx,
                )
                results = orch.run()
                ctx.results.extend(results)
            except Exception:
                self._error = traceback.format_exc()
            finally:
                self._state = "finished"
                # Append to run history
                self._write_history(config)

        self.run_thread = threading.Thread(target=_run, daemon=True)
        self.run_thread.start()
        return run_id

    def _write_history(self, config: Config) -> None:
        """Append a run summary to runs.json."""
        if self.ctx is None:
            return
        duration = time.time() - self.ctx.start_time if self.ctx.start_time else 0
        entry = {
            "run_id": self.run_id,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration": round(duration, 1),
            "stories_total": len(self._stories_processed),
            "stories_completed": sum(
                1 for r in self.ctx.results
                if r.status == StoryStatus.COMPLETED
            ),
            "stories_failed": sum(
                1 for r in self.ctx.results
                if r.status == StoryStatus.FAILED
            ),
        }
        history_file = config.project_root / "runs.json"
        try:
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    project_dir: Path | None = None,
    log_broker: LogBroker | None = None,
    run_control: RunControl | None = None,
    event_bus: EventBus | None = None,
) -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="BMAD Automate Dashboard", version="0.2.0")

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Templates
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Shared state
    _project_dir = (project_dir or Path.cwd()).resolve()
    _log_broker = log_broker or LogBroker(log_file=_project_dir / "bmad-automation.log")
    _run_control = run_control or RunControl()
    _event_bus = event_bus or EventBus()
    _run_manager = RunManager()

    # Run history file
    _history_file = _project_dir / "runs.json"

    # ------------------------------------------------------------------
    # API routes
    # ------------------------------------------------------------------

    @app.get("/api/v1/status")
    async def get_status() -> dict:
        """Return the current orchestrator state."""
        result: dict = {
            "state": _run_manager.state,
            "run_id": _run_manager.run_id,
            "project_dir": str(_project_dir),
        }
        if _run_manager.ctx is not None:
            result["stories_total"] = len(_run_manager._stories_processed)
            result["stories_completed"] = sum(
                1 for r in _run_manager.ctx.results
                if r.status == StoryStatus.COMPLETED
            )
            result["stories_failed"] = sum(
                1 for r in _run_manager.ctx.results
                if r.status == StoryStatus.FAILED
            )
        if _run_manager._error:
            result["error"] = _run_manager._error
        return result

    @app.post("/api/v1/run")
    async def start_run(request: RunRequest) -> dict:
        """Start a new automation run."""
        if _run_manager.is_running:
            raise HTTPException(409, "A run is already active")

        # Validate AI provider
        if request.ai_provider not in AI_PROVIDERS:
            raise HTTPException(
                400,
                f"Unknown AI provider '{request.ai_provider}'. "
                f"Available: {', '.join(AI_PROVIDERS)}",
            )

        # Build Config from request
        proj = Path(request.project_dir).resolve() if request.project_dir != "." else _project_dir
        config = Config(
            sprint_status=proj / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml",
            story_dir=proj / "_bmad-output" / "implementation-artifacts",
            log_file=proj / "bmad-automation.log",
            bmad_dir=proj / "_bmad",
            project_root=proj,
            dry_run=request.dry_run,
            yes=True,
            quiet=False,
            epic=request.epic,
            limit=request.limit,
            start_from=request.start_from,
            specific_stories=request.specific_stories,
            after_epic=request.after_epic,
            parallel_epics=request.parallel_epics,
            ai_provider=request.ai_provider,
            retries=request.retries,
            timeout=request.timeout,
            skip_retro=request.skip_retro,
            skip_course_correct=request.skip_course_correct,
            skip_retro_impl=request.skip_retro_impl,
            skip_next_epic_prep=request.skip_next_epic_prep,
        )

        # Apply only_steps (sets skip flags for steps NOT in the list)
        if request.only_steps:
            all_steps = {"create", "dev", "review", "commit", "pull"}
            requested = set(request.only_steps)
            for step in all_steps:
                if step not in requested:
                    setattr(config, f"skip_{step}", True)

        # Apply skip_steps
        for step in request.skip_steps:
            flag = f"skip_{step}"
            if hasattr(config, flag):
                setattr(config, flag, True)

        # Fresh RunControl for this run
        ctrl = RunControl()
        bus = EventBus()

        run_id = _run_manager.start(
            config=config,
            log_broker=_log_broker,
            run_control=ctrl,
            event_bus=bus,
        )

        return {
            "run_id": run_id,
            "status": "started",
            "stories": _run_manager._stories_processed,
        }

    @app.post("/api/v1/control")
    async def control(request: ControlRequest) -> ControlResponse:
        """Send control commands to a running automation."""
        if not _run_manager.is_running:
            return ControlResponse(accepted=False, reason="No active run")

        ctrl = _run_manager.ctx.run_control if _run_manager.ctx else _run_control
        action = request.action

        if action == "abort":
            ctrl.abort()
            _run_manager._state = "finished"
            return ControlResponse(accepted=True)

        if action == "pause_after_step" and request.epic is not None:
            ctrl.set_pause_after_step(request.epic, True)
            return ControlResponse(accepted=True)

        if action == "pause_after_story" and request.epic is not None:
            ctrl.set_pause_after_story(request.epic, True)
            return ControlResponse(accepted=True)

        if action == "resume" and request.epic is not None:
            ctrl.resume_epic(request.epic)
            return ControlResponse(accepted=True)

        if action == "pause_all":
            with ctrl._lock:
                epic_nums = list(ctrl._epic_events.keys())
            for epic_num in epic_nums:
                ctrl.pause_epic(epic_num)
            return ControlResponse(accepted=True)

        if action == "set_concurrency" and request.value is not None:
            return ControlResponse(accepted=True)

        return ControlResponse(
            accepted=False, reason=f"Unknown or incomplete action: {action}"
        )

    @app.get("/api/v1/dependencies")
    async def get_dependencies() -> dict:
        """Return the epic dependency graph as JSON."""
        import re

        import yaml

        from bmad_automate.dependencies import build_dag

        ss_path = _project_dir / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        if not ss_path.exists():
            return {"nodes": [], "edges": [], "tiers": []}

        with open(ss_path, encoding="utf-8") as f:
            yaml_text = f.read()
        yaml_data = yaml.safe_load(yaml_text) or {}
        dev_status = yaml_data.get("development_status", {})

        # Extract epic numbers from story keys
        epic_nums = sorted({
            int(m.group(1))
            for key in dev_status
            if (m := re.match(r"^(\d+)-\d+-.+$", key))
        })
        if not epic_nums:
            return {"nodes": [], "edges": [], "tiers": []}

        dag = build_dag(yaml_data, yaml_text, epic_nums)
        result = dag.to_dict()

        # Enrich nodes with story counts and status summary
        story_counts: dict[int, int] = {}
        for node in result["nodes"]:
            epic_prefix = f"{node['id']}-"
            stories = {
                k: v for k, v in dev_status.items()
                if k.startswith(epic_prefix) and re.match(r"^\d+-\d+-.+$", k)
            }
            total = len(stories)
            done = sum(1 for v in stories.values() if v == "done")
            node["stories_total"] = total
            node["stories_done"] = done
            story_counts[node["id"]] = total
            if total > 0 and done == total:
                node["status"] = "done"
            elif any(v in ("in-progress", "review") for v in stories.values()):
                node["status"] = "in-progress"
            else:
                node["status"] = "pending"

        # Add critical path
        critical = dag.get_critical_path(story_counts)
        result["critical_path"] = critical

        return result

    @app.get("/api/v1/history")
    async def get_history() -> dict:
        """List past runs."""
        runs: list[dict] = []
        if _history_file.exists():
            for line in _history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return {"runs": runs}

    @app.get("/api/v1/stories")
    async def get_stories() -> dict:
        """Return all stories grouped by status for the run configuration UI."""
        import re

        import yaml

        ss_path = _project_dir / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        if not ss_path.exists():
            return {"stories": {}, "epics": []}

        with open(ss_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        dev_status = data.get("development_status", {})

        stories_by_status: dict[str, list[str]] = {}
        epic_set: set[int] = set()
        for key, status in dev_status.items():
            if re.match(r"^\d+-\d+-.+$", key):
                stories_by_status.setdefault(status, []).append(key)
                m = re.match(r"^(\d+)-", key)
                if m:
                    epic_set.add(int(m.group(1)))

        return {
            "stories": stories_by_status,
            "epics": sorted(epic_set),
        }

    @app.post("/api/v1/after-epic")
    async def run_after_epic(request: RunRequest) -> dict:
        """Trigger the after-epic pipeline for specified epics."""
        if _run_manager.is_running:
            raise HTTPException(409, "A run is already active")

        if not request.after_epic:
            raise HTTPException(400, "No epics specified for after-epic pipeline")

        proj = Path(request.project_dir).resolve() if request.project_dir != "." else _project_dir
        config = Config(
            sprint_status=proj / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml",
            story_dir=proj / "_bmad-output" / "implementation-artifacts",
            log_file=proj / "bmad-automation.log",
            bmad_dir=proj / "_bmad",
            project_root=proj,
            dry_run=request.dry_run,
            yes=True,
            quiet=False,
            ai_provider=request.ai_provider,
            retries=request.retries,
            timeout=request.timeout,
            after_epic=request.after_epic,
            skip_retro=request.skip_retro,
            skip_course_correct=request.skip_course_correct,
            skip_retro_impl=request.skip_retro_impl,
            skip_next_epic_prep=request.skip_next_epic_prep,
        )

        from bmad_automate.context import set_active_context
        from bmad_automate.control import set_active_control
        from bmad_automate.pipeline import run_after_epic_pipeline

        run_id = f"after-epic-{time.strftime('%Y%m%d-%H%M%S')}"
        ctrl = RunControl()
        bus = EventBus()

        ctx = RunContext(
            config=config,
            event_bus=bus,
            log_broker=_log_broker,
            run_control=ctrl,
        )
        set_active_context(ctx)
        set_active_control(ctrl)

        # Bridge pipeline events to the log broker for SSE streaming
        bus.subscribe(_make_event_to_log_bridge(_log_broker))

        _run_manager.run_id = run_id
        _run_manager.ctx = ctx
        _run_manager._state = "running"
        _run_manager._stories_processed = [f"after-epic-{e}" for e in request.after_epic]
        _run_manager._error = None

        def _run_after_epic() -> None:
            try:
                ctx.start_time = time.time()
                retro_results: list = []
                for epic_num in request.after_epic:
                    if ctrl.should_stop():
                        break
                    run_after_epic_pipeline(epic_num, config, ctx, retro_results)
            except Exception:
                _run_manager._error = traceback.format_exc()
            finally:
                _run_manager._state = "finished"

        _run_manager.run_thread = threading.Thread(target=_run_after_epic, daemon=True)
        _run_manager.run_thread.start()

        return {
            "run_id": run_id,
            "status": "started",
            "epics": request.after_epic,
        }

    @app.get("/api/v1/logs/{run_id}")
    async def stream_logs(run_id: str, cursor: int = 0) -> StreamingResponse:
        """Stream log events as Server-Sent Events."""

        async def event_generator() -> AsyncGenerator[str, None]:
            current_cursor = cursor
            while True:
                entries, new_cursor, gap = _log_broker.ring_buffer.read_from(
                    current_cursor
                )

                if gap:
                    missed = new_cursor - current_cursor - len(entries)
                    yield f"event: gap\ndata: {json.dumps({'missed': missed})}\nid: {new_cursor}\n\n"

                for entry in entries:
                    data = {
                        "epic": entry.epic,
                        "story": entry.story,
                        "step": entry.step,
                        "level": entry.level,
                        "line": entry.line,
                        "timestamp": entry.timestamp,
                        "event_kind": entry.event_kind,
                    }
                    yield f"event: log\ndata: {json.dumps(data)}\nid: {entry.cursor}\n\n"

                current_cursor = new_cursor

                if not _run_manager.is_running and not entries:
                    yield f"event: done\ndata: {json.dumps({'state': 'finished'})}\n\n"
                    break

                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # HTML routes
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Serve the main dashboard page."""
        if (TEMPLATES_DIR / "base.html").exists():
            return templates.TemplateResponse("base.html", {"request": request})
        return HTMLResponse(
            "<html><body><h1>BMAD Automate Dashboard</h1>"
            "<p>Templates not found. Reinstall bmad-automate to restore web assets.</p>"
            "</body></html>"
        )

    return app
