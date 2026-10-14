"""Classification projects expose native category sets without typed IDs."""

from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.jobs.classification_projects import ClassificationProjects

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_list_link_and_create_with_explicit_categories(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    projects = [{"id": "projectA", "name": "已有分类项目", "labels": [
        {"label_id": "invoice", "display_name": "发票"},
        {"label_id": "receipt", "display_name": "收据"}]}]
    created_payloads = []

    def native_request(_self, method, path, payload=None):
        if (method, path) == ("GET", "/api/v1/projects"):
            return list(projects)
        if (method, path) == ("POST", "/api/v1/projects"):
            created_payloads.append(payload)
            project = {"id": "projectB", "name": payload["name"], "labels": payload["labels"]}
            projects.append(project)
            return project
        raise AssertionError((method, path))

    monkeypatch.setattr(ClassificationProjects, "_request", native_request)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        first = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        second = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                             json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{first}/classification"
        listing = client.get(base + "/projects")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["labels"] == [
            {"label_id": "invoice", "display_name": "发票"},
            {"label_id": "receipt", "display_name": "收据"}]
        assert client.post(base + "/links", json={"project_id": "projectA"}).status_code == 403
        linked = client.post(base + "/links", headers=LOCAL, json={"project_id": "projectA"})
        assert linked.status_code == 201 and linked.json()["link_id"]
        assert client.post(base + "/links", headers=LOCAL,
                           json={"project_id": "projectA"}).json() == linked.json()
        assert client.post(base + "/links", headers=LOCAL,
                           json={"project_id": "unknown"}).status_code == 404
        assert client.post(base + "/projects", headers=LOCAL, json={
            "name": "新项目", "categories": ["发票", "发票"]}).status_code == 400
        created = client.post(base + "/projects", headers=LOCAL, json={
            "name": "新项目", "categories": ["合同", "票据"]})
        assert created.status_code == 201 and created.json()["link_id"]
        assert created_payloads == [{"name": "新项目", "labels": [
            {"label_id": "class_1", "display_name": "合同", "definition": "合同"},
            {"label_id": "class_2", "display_name": "票据", "definition": "票据"}]}]
        assert len(app.state.workspace_store.list_links(first)) == 2
        other = client.get(f"/api/platform/v1/workspaces/{second}/classification/projects").json()["items"]
        assert all(item["link_id"] is None for item in other)
