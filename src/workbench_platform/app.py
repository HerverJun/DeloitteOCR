"""Minimum platform health endpoint; module capabilities are not assumed."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .catalog.routes import contract_error, router as catalog_router
from .workspaces.routes import configure_workspace_store, router as workspace_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_workspace_store(app)
    yield


app = FastAPI(title="Workbench Platform", version="0.1.0", lifespan=lifespan)
app.include_router(catalog_router)
app.include_router(workspace_router)


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
