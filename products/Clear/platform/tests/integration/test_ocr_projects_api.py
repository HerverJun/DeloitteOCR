"""OCR project selection uses native state and creates durable workspace links."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.jobs.ocr_projects import OcrProjects
from workbench_platform.catalog.launch import LaunchStatus

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_direct_workbench_open_authenticates_without_creating_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    native_token = "s" * 48
    supplied_token = native_token
    requests = []

    class Native(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            authorized = self.headers.get("Authorization") == "Bearer " + native_token
            raw = json.dumps({"projects": []} if authorized else {"message": "expired"}).encode()
            self.send_response(200 if authorized else 401)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Native)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    receipt = SimpleNamespace(instance_id="instanceA", base_url=f"http://127.0.0.1:{server.server_port}/")

    def get(app_id, launch_id):
        if app_id != "ocr" or launch_id != "launchA":
            raise LaunchStatus(410, "APP_UNAVAILABLE")
        return receipt

    try:
        with TestClient(app, base_url="http://127.0.0.1:8766") as client:
            monkeypatch.setattr(app.state, "job_service", SimpleNamespace(
                launches=SimpleNamespace(get=get, current=lambda _: receipt),
                _token=lambda: supplied_token))
            route = "/api/platform/v1/apps/ocr/launches/launchA/navigation"
            assert client.post(route).status_code == 403
            assert client.post(route, headers=LOCAL, content=b"{}").status_code == 400
            assert client.post(route + "?url=http://example.com", headers=LOCAL).status_code == 400
            assert client.get(route).status_code == 405
            opened = client.post(route, headers=LOCAL)
            assert opened.status_code == 200
            assert opened.json() == {"url": receipt.base_url + "#token=" + native_token}
            assert opened.headers["Cache-Control"] == "no-store"
            assert requests == ["/api/state"]
            supplied_token = "w" * 48
            assert client.post(route, headers=LOCAL).status_code == 503
            assert client.post(route.replace("launchA", "oldLaunch"), headers=LOCAL).status_code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_project_listing_linking_and_creation_without_manual_native_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    projects = [{"id": "nativeA", "name": "已有 OCR 项目"}]

    def native_request(_self, method, path, payload=None):
        if method == "GET" and path == "/api/state":
            return {"projects": list(projects)}
        if method == "POST" and path == "/api/projects":
            project = {"id": "nativeB", "name": payload["name"]}
            projects.append(project)
            return project
        raise AssertionError((method, path))

    monkeypatch.setattr(OcrProjects, "_request", native_request)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        first = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        second = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                             json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{first}/ocr"
        listing = client.get(base + "/projects")
        assert listing.status_code == 200 and listing.json()["items"] == [
            {"project_id": "nativeA", "name": "已有 OCR 项目", "link_id": None, "role": None}]
        assert client.post(base + "/links", json={"project_id": "nativeA"}).status_code == 403
        linked = client.post(base + "/links", headers=LOCAL, json={"project_id": "nativeA"})
        assert linked.status_code == 201 and linked.json()["link_id"]
        assert client.post(base + "/links", headers=LOCAL,
                           json={"project_id": "nativeA"}).json() == linked.json()
        assert client.post(base + "/links", headers=LOCAL,
                           json={"project_id": "invented"}).status_code == 404
        created = client.post(base + "/projects", headers=LOCAL, json={"name": "新项目"})
        assert created.status_code == 201 and created.json()["name"] == "新项目"
        assert created.json()["link_id"] and created.json()["project_id"] == "nativeB"
        assert len(app.state.workspace_store.list_links(first)) == 2
        other = client.get(f"/api/platform/v1/workspaces/{second}/ocr/projects").json()["items"]
        assert all(item["link_id"] is None for item in other)
        monkeypatch.setattr(app.state.job_service, "_context", lambda *_: ResolvedContext(
            first, "ocr", "instanceA", linked.json()["link_id"], "nativeA", "owner",
            "/jobs", "http://127.0.0.1:8767/"))
        projects.clear()
        missing = client.post(base + f"/links/{linked.json()['link_id']}/navigation", headers=LOCAL)
        assert missing.status_code == 404
