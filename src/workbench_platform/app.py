"""Minimum platform health endpoint; module capabilities are not assumed."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .catalog.routes import contract_error, router as catalog_router

app = FastAPI(title="Workbench Platform", version="0.1.0")
app.include_router(catalog_router)


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
