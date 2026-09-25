"""Minimum platform health endpoint; module capabilities are not assumed."""

from fastapi import FastAPI

app = FastAPI(title="Workbench Platform", version="0.1.0")


@app.get("/api/platform/v1/health")
def health() -> dict[str, str]:
    return {
        "service": "ready",
        "ui": "unknown",
        "cpu": "unknown",
        "gpu": "unknown",
        "models": "unknown",
    }
