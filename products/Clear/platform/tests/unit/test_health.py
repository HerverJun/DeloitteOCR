from fastapi.testclient import TestClient

from workbench_platform.app import app


def test_health_reports_only_live_service() -> None:
    response = TestClient(app).get("/api/platform/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "service": "ready", "ui": "unknown", "cpu": "unknown",
        "gpu": "unknown", "models": "unknown",
    }


def test_unknown_app_has_no_fake_health() -> None:
    response = TestClient(app).get("/api/platform/v1/apps/not_registered/health")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
