"""Real FastAPI catalog routes against the existing strict wire models."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from packages.contracts.models import AppHealth, AppManifest, AppManifestPage, Error
from workbench_platform.app import app
from workbench_platform.catalog.registry import AppRegistry, DEFAULT_CONFIG
from workbench_platform.catalog.routes import get_registry


@pytest.fixture
def catalog(tmp_path):
    config = tmp_path / "config/apps"
    config.mkdir(parents=True)
    for item in DEFAULT_CONFIG.glob("*.json"):
        shutil.copy2(item, config / item.name)
    registry = AppRegistry(config, install_root=tmp_path / "bundles")
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        yield config, registry, TestClient(app)
    finally:
        app.dependency_overrides.clear()


def trust_manifest(config, app_id):
    record_path = config / f"{app_id}.install.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["manifest_sha256"] = hashlib.sha256((config / f"{app_id}.json").read_bytes()).hexdigest()
    record_path.write_text(json.dumps(record), encoding="utf-8")


def test_routes_paginate_manifest_snapshot_and_reject_stale_cursor(catalog):
    config, registry, client = catalog
    first = client.get("/api/platform/v1/apps", params={"limit": 1})
    assert first.status_code == 200
    page = AppManifestPage.model_validate(first.json())
    assert [item.app_id for item in page.items] == ["classification"]
    assert page.next_cursor == "after_classification" and page.snapshot_revision > 0
    second = client.get("/api/platform/v1/apps", params={"limit": 1,
                       "cursor": page.next_cursor, "snapshot_revision": page.snapshot_revision})
    assert second.status_code == 200
    other = AppManifestPage.model_validate(second.json())
    assert [item.app_id for item in other.items] == ["ocr"] and other.next_cursor is None
    missing_snapshot = client.get("/api/platform/v1/apps", params={"limit": 1, "cursor": page.next_cursor})
    assert missing_snapshot.status_code == 400
    assert Error.model_validate(missing_snapshot.json()).code == "VALIDATION_FAILED"
    for app_id in ("ocr", "classification"):
        response = client.get(f"/api/platform/v1/apps/{app_id}")
        assert response.status_code == 200 and AppManifest.model_validate(response.json()).app_id == app_id
    assert registry.snapshot_revision == page.snapshot_revision
    (config / "classification.install.json").unlink()
    registry.reload()
    assert set(registry.manifests) == {"ocr"}
    stale = client.get("/api/platform/v1/apps", params={"limit": 1,
                       "cursor": page.next_cursor, "snapshot_revision": page.snapshot_revision})
    assert stale.status_code == 409
    assert Error.model_validate(stale.json()).code == "SNAPSHOT_EXPIRED"
    assert client.get("/api/platform/v1/apps/ocr").status_code == 200


def test_reload_expires_cursor_and_clears_observed_health(catalog):
    _, registry, client = catalog
    page = AppManifestPage.model_validate(client.get("/api/platform/v1/apps?limit=1").json())
    observation = AppHealth.model_validate({"schema_version": "1.0", **{
        key: {"schema_version": "1.0", "state": "ready", "reason": "Previously probed"}
        for key in ("service", "ui", "cpu", "gpu", "models")}})
    registry.observe_health("classification", observation, snapshot_revision=page.snapshot_revision)
    assert registry.health("classification").service.state == "ready"
    registry.reload()
    assert registry.snapshot_revision != page.snapshot_revision
    assert registry.health("classification").service.state == "unavailable"
    with pytest.raises(ValueError, match="snapshot changed"):
        registry.observe_health("classification", observation, snapshot_revision=page.snapshot_revision)
    assert registry.health("classification").service.state == "unavailable"
    response = client.get("/api/platform/v1/apps", params={"limit": 1,
                          "cursor": page.next_cursor, "snapshot_revision": page.snapshot_revision})
    assert response.status_code == 409
    assert Error.model_validate(response.json()).code == "SNAPSHOT_EXPIRED"


def test_missing_uninstalled_and_independent_health_states(catalog):
    _, registry, client = catalog
    for app_id in ("ocr", "classification"):
        response = client.get(f"/api/platform/v1/apps/{app_id}/health")
        assert response.status_code == 200
        health = AppHealth.model_validate(response.json())
        assert health.service.state == "unavailable" and health.service.reason
        assert health.ui.state == "unavailable" and health.cpu.state == "unavailable"
        assert health.models.state == "unavailable"
    assert registry.health("classification").gpu.state == "unavailable"
    assert registry.health("ocr").gpu.state == "unavailable"
    observation = AppHealth.model_validate({"schema_version": "1.0", **{
        key: {"schema_version": "1.0", "state": state, "reason": reason}
        for key, state, reason in (("service", "unavailable", "Connection refused"),
                                   ("ui", "unknown", "UI not probed"),
                                   ("cpu", "unknown", "CPU not probed"),
                                   ("gpu", "unavailable", "GPU is waiting"),
                                   ("models", "unavailable", "Model files missing"))}})
    registry.observe_health("classification", observation, snapshot_revision=registry.snapshot_revision)
    assert AppHealth.model_validate(client.get("/api/platform/v1/apps/classification/health").json()) == observation
    unknown = client.get("/api/platform/v1/apps/unknown/health")
    assert unknown.status_code == 404 and Error.model_validate(unknown.json()).code == "NOT_FOUND"
    assert client.get("/api/platform/v1/apps/unknown").status_code == 404


def test_manifest_upload_and_unsafe_declarations_never_choose_execution(catalog):
    config, registry, client = catalog
    before = registry.installations["ocr"]
    response = client.post("/api/platform/v1/apps", json={"app_id": "ocr", "executable": "C:/evil.exe"})
    assert response.status_code == 405 and registry.installations["ocr"] == before
    path = config / "ocr.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update({"executable": "C:/evil.exe", "proxy_target": "http://evil.test:80",
                     "file_root": "C:/private"})
    path.write_text(json.dumps(manifest), encoding="utf-8")
    trust_manifest(config, "ocr")
    registry.reload()
    assert "ocr" not in registry.manifests and "classification" in registry.manifests
    assert client.get("/api/platform/v1/apps/ocr").status_code == 404
    assert "evil" not in client.get("/api/platform/v1/apps/ocr").text


def test_trusted_install_record_pins_declaration_and_entry_only(catalog, tmp_path):
    config, registry, client = catalog
    bundle = tmp_path / "bundles/ocr"
    bundle.mkdir(parents=True)
    executable = bundle / "app.exe"
    executable.write_bytes(b"synthetic trusted executable")
    payload = bundle / "model.bin"
    payload.write_bytes(b"first model payload")
    executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    declaration = {"app_id": "ocr", "files": [
        {"path": "app.exe", "sha256": executable_sha},
        {"path": "model.bin", "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}]}
    manifest_path = bundle / "bundle.manifest.json"
    manifest_path.write_text(json.dumps(declaration), encoding="utf-8")
    record = {"app_id": "ocr", "manifest_sha256": hashlib.sha256((config / "ocr.json").read_bytes()).hexdigest(),
              "installed": True, "launch": {"bundle_root": str(bundle), "executable": str(executable),
              "working_directory": str(bundle), "bundle_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "executable_sha256": executable_sha, "allowed_arguments": ["--fixed"]}}
    path = config / "ocr.install.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    registry.reload()
    assert registry.installations["ocr"].installed
    assert registry.health("ocr").service.state == "unknown"  # registered is not running
    assert registry.health("ocr").models.state == "unknown"  # catalog does not scan models
    assert registry.installations["ocr"].executable_sha256 == executable_sha
    payload.write_bytes(b"changed model payload")
    registry.reload()
    assert registry.installations["ocr"].installed  # other payload bytes are not read by the catalog
    for mutation in (lambda value: value["launch"].update(proxy_target="http://evil.test:8765"),
                     lambda value: value["launch"].update(executable="C:/Windows/System32/cmd.exe"),
                     lambda value: value["launch"].update(allowed_arguments=["--fixed&echo"]),
                     lambda value: value["launch"].update(executable_sha256="0" * 64),
                     lambda value: value["launch"].update(bundle_manifest_sha256="0" * 64)):
        candidate = json.loads(json.dumps(record))
        mutation(candidate)
        path.write_text(json.dumps(candidate), encoding="utf-8")
        registry.reload()
        assert "ocr" not in registry.manifests and "classification" in registry.manifests
        assert client.get("/api/platform/v1/apps/classification").status_code == 200
    path.write_text(json.dumps(record), encoding="utf-8")
    for changed in (lambda value: value["files"][1].update(sha256="0" * 64),
                    lambda value: value["files"][1].update(path="../escape"),
                    lambda value: value["files"].append(value["files"][0])):
        candidate = json.loads(json.dumps(declaration))
        changed(candidate)
        manifest_path.write_text(json.dumps(candidate), encoding="utf-8")
        registry.reload()
        assert "ocr" not in registry.manifests  # the trusted declaration changed even though exe did not
    executable.write_bytes(b"tampered executable")
    manifest_path.write_text(json.dumps(declaration), encoding="utf-8")
    registry.reload()
    assert "ocr" not in registry.manifests


def test_manifest_minimum_platform_version_is_checked(catalog):
    config, registry, _ = catalog
    path = config / "ocr.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for version in ("0.1.1", "01.0.0", "unbounded"):
        manifest = {**original, "min_platform_version": version}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        trust_manifest(config, "ocr")
        registry.reload()
        assert "ocr" not in registry.manifests and "classification" in registry.manifests


def test_validation_errors_are_contract_errors_without_input_echo(catalog):
    _, _, client = catalog
    for url in ("/api/platform/v1/apps", "/api/platform/v1/apps?limit=0",
                "/api/platform/v1/apps?limit=1&extra=C:/private/secret",
                "/api/platform/v1/apps?limit=1&limit=2"):
        response = client.get(url)
        assert response.status_code == 400
        assert Error.model_validate(response.json()).code == "VALIDATION_FAILED"
        assert "C:/private/secret" not in response.text


def test_catalog_import_does_not_import_native_apps_or_models():
    root = Path(__file__).resolve().parents[2]
    probe = ("import sys; "
             f"sys.path.insert(0, {str(root)!r}); sys.path.insert(0, {str(root / 'src')!r}); "
             "from workbench_platform.app import app; "
             "from fastapi.testclient import TestClient; "
             "assert TestClient(app).get('/api/platform/v1/apps?limit=1').status_code == 200; "
             "assert 'ocr_workbench' not in sys.modules and 'image_platform' not in sys.modules; "
             "print('catalog-only')")
    result = subprocess.run([sys.executable, "-I", "-c", probe], cwd=root,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and result.stdout.strip() == "catalog-only", result.stderr
