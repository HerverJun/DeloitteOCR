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
from .assets.routes import configure_assets, router as assets_router
from .artifacts.routes import configure_artifacts, router as artifacts_router
from .jobs.ocr_projects import router as ocr_projects_router
from .jobs.ocr_handoff import configure_ocr_handoff, router as ocr_handoff_router
from .jobs.classification_projects import router as classification_projects_router
from .jobs.classification_handoff import configure_classification_handoff, router as classification_handoff_router
from .batches.routes import configure_batches, router as batches_router
from .audit.routes import configure_audit, router as audit_router
from .forensics.routes import configure_forensics, router as forensics_router
from .multimodal.routes import configure_multimodal, router as multimodal_router
from .agent.routes import configure_agent, router as agent_router
from .agent.skills_routes import router as agent_skills_router
from .documents.routes import configure_documents, router as documents_router
from .comparisons.routes import configure_comparisons, router as comparisons_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_workspace_store(app)
    configure_data(app)
    configure_assets(app)
    configured = os.environ.get("WORKBENCH_DATA_DIR")
    data_root = Path(configured).resolve() if configured else None
    startup_seconds = float(os.environ.get('WORKBENCH_NATIVE_STARTUP_SECONDS', '20'))
    if not 1 <= startup_seconds <= 120:
        raise ValueError('原生启动等待配置无效')
    supervisor = Supervisor(get_registry(), handshake_timeout=startup_seconds, data_root=data_root)
    try:
        app.state.recent_store = RecentWorkStore(data_root / "recent-work.sqlite3") if data_root else None
        app.state.supervisor = supervisor
        app.state.launch_service = LaunchService(supervisor, app.state.recent_store)
        app.state.context_bridge = (ContextBridge(app.state.workspace_store, supervisor)
                                    if app.state.workspace_store is not None else None)
        configure_jobs(app)
        configure_ocr_handoff(app)
        configure_classification_handoff(app)
        configure_artifacts(app)
        configure_batches(app)
        configure_audit(app)
        configure_forensics(app)
        configure_multimodal(app)
        configure_documents(app)
        configure_comparisons(app)
        await configure_agent(app)
        yield
    finally:
        comparison_service = getattr(app.state, 'comparison_service', None)
        if comparison_service is not None:
            comparison_service.close()
        app.state.comparison_service = None
        document_service = getattr(app.state, 'document_service', None)
        if document_service is not None:
            document_service.close()
        app.state.document_service = None
        manual_service = getattr(app.state, 'multimodal_service', None)
        if manual_service is not None:
            manual_service.close()
        app.state.multimodal_service = None
        agent = getattr(app.state, 'agent', None)
        if agent is not None:
            await agent.close()
        app.state.agent = None
        forensic_service = getattr(app.state, "forensic_service", None)
        if forensic_service is not None:
            forensic_service.close()
        app.state.forensic_service = None
        app.state.audit_service = None
        batch_service = getattr(app.state, 'batch_service', None)
        if batch_service is not None:
            batch_service.close()
        app.state.batch_service = None
        data_service = getattr(app.state, 'data_workbench', None)
        if data_service is not None:
            data_service.close()
        app.state.job_service = None
        app.state.ocr_handoff = None
        app.state.classification_handoff = None
        app.state.asset_library = None
        app.state.artifact_center = None
        app.state.context_bridge = None
        app.state.launch_service = None
        app.state.supervisor = None
        supervisor.close()


app = FastAPI(title="Workbench Platform", version="0.1.0", lifespan=lifespan)


@app.get('/api/platform/v2/mode')
def local_mode():
    return {'multiuser': False, 'contract_version': '2.0', 'login_required': False}
app.include_router(catalog_router)
app.include_router(workspace_router)
app.include_router(launch_router)
app.include_router(bridge_router(lambda request: getattr(request.app.state, "context_bridge", None)))
app.include_router(jobs_router)
app.include_router(data_router)
app.include_router(assets_router)
app.include_router(artifacts_router)
app.include_router(ocr_projects_router)
app.include_router(ocr_handoff_router)
app.include_router(classification_projects_router)
app.include_router(classification_handoff_router)
app.include_router(batches_router)
app.include_router(audit_router)
app.include_router(forensics_router)
app.include_router(multimodal_router)
app.include_router(agent_router)
app.include_router(agent_skills_router)
app.include_router(documents_router)
app.include_router(comparisons_router)


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
        target = candidate if candidate.is_file() else portal_root / 'index.html'
        # Revalidate the SPA entry so an existing browser loads the current build.
        return FileResponse(target, headers={'Cache-Control': 'no-cache'} if target.name == 'index.html' else None)
