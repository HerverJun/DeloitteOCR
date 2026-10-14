"""Workspace HTTP boundaries and persisted pages."""

from fastapi.testclient import TestClient

from packages.contracts.models import Workspace, WorkspacePage
from workbench_platform.app import app
from workbench_platform.store import WorkspaceStore

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_unconfigured_never_returns_a_false_empty_list(monkeypatch):
    monkeypatch.delenv("WORKBENCH_DATA_DIR", raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.get("/api/platform/v1/workspaces?limit=10")
        assert response.status_code == 503 and response.json()["code"] == "APP_UNAVAILABLE"


def test_create_read_page_and_snapshot_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        path = "/api/platform/v1/workspaces"
        assert client.get(path + "?limit=1").json()["items"] == []
        first = client.post(path, headers=LOCAL, json={"schema_version": "1.0", "name": "资料甲"})
        assert first.status_code == 201
        item = Workspace.model_validate(first.json())
        assert client.get(path + "/" + item.workspace_id).json() == first.json()
        second = client.post(path, headers=LOCAL, json={"schema_version": "1.0", "name": "资料乙"})
        assert second.status_code == 201
        page = WorkspacePage.model_validate(client.get(path + "?limit=1").json())
        assert page.next_cursor == "1" and 1 <= page.snapshot_revision <= 9_007_199_254_740_991
        next_page = client.get(path, params={"limit": 1, "cursor": page.next_cursor,
                                             "snapshot_revision": page.snapshot_revision})
        assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
        WorkspaceStore(tmp_path / "workspaces.sqlite3").rename_workspace(item.workspace_id, "改名", expected_revision=1)
        stale = client.get(path, params={"limit": 1, "cursor": page.next_cursor,
                                         "snapshot_revision": page.snapshot_revision})
        assert stale.status_code == 409 and stale.json()["code"] == "SNAPSHOT_EXPIRED"
        assert client.get(path + "?limit=1&limit=2").status_code == 400
        assert client.get(path + "?limit=1&cursor=1").status_code == 400


def test_host_origin_json_and_size_guards_do_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        path = "/api/platform/v1/workspaces"
        payload = {"schema_version": "1.0", "name": "Case"}
        assert client.get(path + "?limit=10", headers={"host": "evil.example"}).status_code == 403
        assert client.get(path + "/workspaceA", headers={"host": "evil.example"}).status_code == 403
        assert client.post(path, json=payload).status_code == 403
        assert client.post(path, headers={"host": "evil.example", "origin": "http://evil.example"}, json=payload).status_code == 403
        assert client.post(path, headers={**LOCAL, "origin": "http://127.0.0.1:9999"}, json=payload).status_code == 403
        assert client.post(path, headers=LOCAL, content=b'name=Case').status_code == 400
        assert client.post(path, headers={**LOCAL, "content-type": "application/json"}, content=b"x" * 5000).status_code == 400
        assert client.post(path, headers=LOCAL, json={**payload, "unexpected": "path"}).status_code == 400
        assert client.get(path + "?limit=10").json()["items"] == []
