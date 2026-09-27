"""Local platform composition; application services live for one ASGI lifespan."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse

from .catalog.routes import contract_error, router as catalog_router
from .catalog.routes import get_registry
from .catalog.launch import LaunchService, router as launch_router
from .catalog.recent import RecentWorkStore
from .bridge import ContextBridge, bridge_router
from .jobs.routes import configure_jobs, router as jobs_router
from .supervisor import Supervisor
from .workspaces.routes import configure_workspace_store, router as workspace_router
from .data_api.routes import configure_data, router as data_router
from .data_api.service import DataError


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
        configure_data(app)
        yield
    finally:
        data_service = getattr(app.state, 'data_workbench', None)
        if data_service is not None:
            data_service.close()
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
app.include_router(data_router)


@app.exception_handler(DataError)
async def data_error(request, error):
    return JSONResponse({'detail': str(error)}, status_code=error.status)


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


portal_directory = os.environ.get('WORKBENCH_PORTAL_DIST')
if portal_directory:
    portal_root = Path(portal_directory).resolve(strict=True)

    @app.get('/{requested_path:path}', include_in_schema=False)
    def portal(requested_path: str):
        if requested_path.startswith('api/'):
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        candidate = (portal_root / requested_path).resolve()
        if not candidate.is_relative_to(portal_root):
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        return FileResponse(candidate if candidate.is_file() else portal_root / 'index.html')
