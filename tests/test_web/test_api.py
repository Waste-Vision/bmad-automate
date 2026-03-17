"""Tests for web API endpoints."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bmad_automate.control import RunControl
from bmad_automate.events import EventBus
from bmad_automate.logging import LogBroker
from bmad_automate.web.app import create_app


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Set up a minimal BMAD project for web API tests."""
    # sprint-status.yaml
    impl = tmp_path / "_bmad-output" / "implementation-artifacts"
    impl.mkdir(parents=True)
    ss = impl / "sprint-status.yaml"
    ss.write_text(
        textwrap.dedent("""\
            development_status:
              1-1-setup: ready-for-dev
              1-2-auth: backlog
        """),
        encoding="utf-8",
    )
    # _bmad dir
    (tmp_path / "_bmad").mkdir()
    return tmp_path


@pytest.fixture()
def app(project_dir: Path):
    return create_app(
        project_dir=project_dir,
        log_broker=LogBroker(buffer_size=100),
        run_control=RunControl(),
        event_bus=EventBus(),
    )


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
class TestStatusEndpoint:
    async def test_status_idle(self, client):
        resp = await client.get("/api/v1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "idle"


@pytest.mark.anyio
class TestRunEndpoint:
    async def test_start_run(self, client, project_dir: Path):
        resp = await client.post("/api/v1/run", json={
            "project_dir": str(project_dir),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert "run_id" in data
        assert "stories" in data
        assert len(data["stories"]) > 0

    async def test_conflict_on_second_run(self, client, project_dir: Path):
        await client.post("/api/v1/run", json={"project_dir": str(project_dir)})
        resp = await client.post("/api/v1/run", json={"project_dir": str(project_dir)})
        assert resp.status_code == 409

    async def test_invalid_ai_provider(self, client, project_dir: Path):
        resp = await client.post("/api/v1/run", json={
            "project_dir": str(project_dir),
            "ai_provider": "openai",
        })
        assert resp.status_code == 400


@pytest.mark.anyio
class TestControlEndpoint:
    async def test_no_active_run(self, client):
        resp = await client.post("/api/v1/control", json={
            "action": "abort",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is False

    async def test_abort(self, client, project_dir: Path):
        await client.post("/api/v1/run", json={"project_dir": str(project_dir)})
        resp = await client.post("/api/v1/control", json={
            "action": "abort",
        })
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

    async def test_set_concurrency(self, client, project_dir: Path):
        await client.post("/api/v1/run", json={"project_dir": str(project_dir)})
        resp = await client.post("/api/v1/control", json={
            "action": "set_concurrency",
            "value": 3,
        })
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True


@pytest.mark.anyio
class TestDependenciesEndpoint:
    async def test_returns_graph(self, client, project_dir: Path):
        # Add dependencies to the sprint-status
        ss = project_dir / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        ss.write_text(
            textwrap.dedent("""\
                epic_dependencies:
                  2: [1]
                  3: [2]
                development_status:
                  1-1-setup: done
                  1-2-auth: done
                  2-1-api: ready-for-dev
                  3-1-dash: backlog
            """),
            encoding="utf-8",
        )
        resp = await client.get("/api/v1/dependencies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 3
        assert len(data["edges"]) == 2
        assert len(data["tiers"]) == 3
        # Epic 1 should be done
        epic1 = next(n for n in data["nodes"] if n["id"] == 1)
        assert epic1["status"] == "done"
        assert epic1["tier"] == 0

    async def test_no_epics(self, client, project_dir: Path):
        ss = project_dir / "_bmad-output" / "implementation-artifacts" / "sprint-status.yaml"
        ss.write_text("development_status:\n  epic-1-retro: done\n", encoding="utf-8")
        resp = await client.get("/api/v1/dependencies")
        assert resp.status_code == 200
        assert resp.json()["nodes"] == []


@pytest.mark.anyio
class TestHistoryEndpoint:
    async def test_empty_history(self, client):
        resp = await client.get("/api/v1/history")
        assert resp.status_code == 200
        assert resp.json() == {"runs": []}

    async def test_reads_existing_history(self, client, project_dir: Path):
        history = project_dir / "runs.json"
        entry = {"run_id": "run-test", "duration": 100}
        history.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        resp = await client.get("/api/v1/history")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-test"
