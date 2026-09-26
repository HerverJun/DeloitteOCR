"""Local platform composition; application services live for one ASGI lifespan."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .catalog.routes import contract_error, router as catalog_router
from .catalog.routes import get_registry
from .catalog.launch import LaunchService, router as launch_router
from .catalog.recent import RecentWorkStore
from .bridge import ContextBridge, bridge_router
from .jobs.routes import configure_jobs, router as jobs_router
from .supervisor import Supervisor
from .workspaces.routes import configure_workspace_store, router as workspace_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_workspace_store(app)
    supervisor = Supervisor(get_registry())
    try:
        configured = os.environ.get("WORKBENCH_DATA_DIR")
        app.state.recent_store = RecentWorkStore(Path(configured).resolve() / "recent-work.sqlite3") if configured else None
        app.state.supervisor = supervisor
        app.state.launch_service = LaunchService(supervisor, app.state.recent_store)
        app.state.context_bridge = (ContextBridge(app.state.workspace_store, supervisor)
                                    if app.state.workspace_store is not None else None)
        configure_jobs(app)
        yield
    finally:
        app.state.job_service = None
        app.state.context_bridge = None
        app.state.launch_service = None
        app.state.supervisor = None
        supervisor.close()


app = FastAPI(title="Workbench Platform", version="0.1.0", lifespan=lifespan)
app.include_router(catalog_router)
app.include_router(workspace_router)
app.include_router(launch_router)
app.include_router(bridge_router(lambda request: getattr(request.app.state, "context_bridge", None)))
app.include_router(jobs_router)


@app.exception_handler(RequestValidationError)
def catalog_validation_error(request, error):
    # Pydantic's error string can include untrusted input; never echo it.
    return contract_error(400, "VALIDATION_FAILED", "Invalid request")


@app.get("/api/platform/v1/health")
def health() -> dict[str, str]:
    return {
        "service": "ready",
        "ui": "unknown",
        "cpu": "unknown",
        "gpu": "unknown",
        "models": "unknown",
    }
