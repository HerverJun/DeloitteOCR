"""Completed data exports become workspace-scoped, replayable artifacts."""

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def finished_export(service):
    session = service.create("对账成果")
    session_id = session["id"]
    upload_id = "uSourceA"
    source = service.session_dir(session_id) / "uploads" / "uSourceA.csv"
    source.write_bytes("编号,金额\n甲,10\n".encode())
    source_bytes = source.read_bytes()
    service.upload(session_id, upload_id, "原始表.csv", ".csv", source,
                   sha(source_bytes), len(source_bytes))
    export_id = "jExportA"
    folder = service.session_dir(session_id) / "exports" / export_id
    folder.mkdir(parents=True)
    source_ref = {"asset_id": upload_id, "sha256": sha(source_bytes), "format": "csv"}
    contents = {
        "cleaned.csv": b"source_row,amount\n2,10\n",
        "cleaned.xlsx": b"xlsx-fixture",
        "cleaned.parquet": b"parquet-fixture",
        "summary.json": json.dumps({"revision": "rOriginal", "source": source_ref,
                                    "rows": 1}, ensure_ascii=False).encode(),
        "provenance.json": json.dumps({"source": source_ref, "rows": [2]}).encode(),
    }
    files = []
    for name, data in contents.items():
        path = folder / name
        path.write_bytes(data)
        files.append({"name": name, "relative": path.relative_to(service.session_dir(session_id)).as_posix(),
                      "sha256": sha(data), "bytes": len(data)})
    state = session["state"]
    state["exports"] = [{"id": export_id, "result_id": None, "files": files}]
    with sqlite3.connect(service.db_path) as db:
        db.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(state), session_id))
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                   (export_id, session_id, "export_table", "succeeded", None, "{}",
                    "2026-09-27T00:00:00Z", "2026-09-27T00:00:00Z"))
    return session_id, export_id, contents, source


def test_publish_replay_download_and_source_links(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WORKBENCH_DATA_PYTHON", raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        first = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        second = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                             json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        session_id, export_id, contents, _ = finished_export(app.state.data_workbench)
        base = f"/api/platform/v1/workspaces/{first}/artifacts"
        other = f"/api/platform/v1/workspaces/{second}/artifacts"
        payload = {"session_id": session_id, "export_id": export_id}
        assert client.post(base + "/data-exports", json=payload).status_code == 403
        created = client.post(base + "/data-exports", headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        bundle = created.json()
        assert len(bundle["files"]) == 5
        assert [item["name"] for item in bundle["inputs"]] == ["原始表.csv"]
        assert bundle["input_set_revision"] == 1
        replay = client.post(base + "/data-exports", headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json() == bundle
        page = client.get(base).json()
        assert page["total"] == 1 and page["items"][0] == bundle
        assert client.get(other).json()["total"] == 0
        assert client.get(other + "/" + bundle["bundle_id"]).status_code == 404
        for name, original in contents.items():
            result = client.get(base + f"/{bundle['bundle_id']}/files/{name}")
            assert result.status_code == 200 and result.content == original
        asset_id = bundle["inputs"][0]["asset_id"]
        asset = client.get(f"/api/platform/v1/workspaces/{first}/assets/{asset_id}").json()
        assert asset["original_name"] == "原始表.csv" and asset["source"]["root_id"] == "data"
        assert client.get(f"/api/platform/v1/workspaces/{first}/assets/{asset_id}/content",
                          params={"revision": 1}).content == "编号,金额\n甲,10\n".encode()


def test_source_change_blocks_publication(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WORKBENCH_DATA_PYTHON", raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace_id = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                   json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        session_id, export_id, _, source = finished_export(app.state.data_workbench)
        source.write_bytes(b"changed")
        base = f"/api/platform/v1/workspaces/{workspace_id}/artifacts"
        response = client.post(base + "/data-exports", headers=LOCAL,
                               json={"session_id": session_id, "export_id": export_id})
        assert response.status_code == 409
        assert client.get(base).json()["total"] == 0


def test_fixed_asset_set_stages_only_its_pinned_member(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WORKBENCH_DATA_PYTHON", raising=False)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        first = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        second = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                             json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{first}/assets"
        asset = client.post(base + "/uploads?name=原始台账.csv", headers=LOCAL,
                            content="编号,金额\n甲,10\n".encode()).json()
        fixed = client.post(base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        session = client.post("/api/platform/v1/data/sessions", headers=LOCAL,
                              json={"name": "资料集导入"}).json()
        path = f"/api/platform/v1/data/sessions/{session['id']}/asset-uploads"
        payload = {"workspace_id": first, "asset_set_id": fixed["asset_set_id"],
                   "asset_set_revision": fixed["revision"], "item_id": fixed["items"][0]["item_id"]}
        assert client.post(path, json=payload).status_code == 403
        staged = client.post(path, headers=LOCAL, json=payload)
        assert staged.status_code == 201, staged.text
        upload = app.state.data_workbench.upload_source(session["id"], staged.json()["id"])
        assert upload["source_workspace_id"] == first
        assert upload["source_asset_id"] == asset["asset_id"]
        assert upload["source_asset_revision"] == 1
        assert upload["source_blob_hash"] == asset["blob_hash"]
        assert client.post(path, headers=LOCAL, json={**payload, "workspace_id": second}).status_code == 409
