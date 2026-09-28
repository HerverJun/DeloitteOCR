"""OCR result bundles keep edited versions and verified source links."""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.jobs.native_ocr import OcrResultConflict, OcrUnavailable, ResultArtifact

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_ocr_bundle_replay_revision_and_workspace_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        other = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        source = client.post(f"/api/platform/v1/workspaces/{workspace}/assets/uploads?name=票据.png",
                             headers=LOCAL, content=b"fixture-bytes").json()
        locator = {"schema_version": "1.0", "kind": "file", "asset_id": source["asset_id"],
                   "asset_revision": 1, "blob_hash": source["blob_hash"], "precision": "exact"}
        inputs = [{"role": "image", "name": "票据.png", "asset_id": source["asset_id"],
                   "revision": 1, "blob_hash": source["blob_hash"], "source_locator": locator}]
        item = {"native_result_id": "resultA", "native_task_id": "taskA",
                "native_asset_id": "nativeAssetA", "native_version_id": "versionA",
                "asset_id": source["asset_id"], "revision": 0, "selected": True}
        projection = {"instance_id": "instanceA", "operation_id": "operationA",
                      "input_manifest_hash": "a" * 64, "native_status": "succeeded",
                      "status": "succeeded", "stage": "OCR completed",
                      "counts": {"total": 1, "succeeded": 1, "failed": 0, "cancelled": 0, "pending": 0},
                      "last_event_seq": 3, "updated_at": "2026-09-28T00:00:00Z"}
        export = {"id": "resultA", "revision": 0, "edited": {"text": "原始文字", "tables": []}}
        snapshot = {"workspace_id": workspace, "handoff_id": "handoffA",
                    "project_id": "projectA", "project_name": "OCR 项目",
                    "platform_job_id": "platformJobA", "native_task_ids": ["taskA"],
                    "engines": ["ppocr"], "input_set_id": "setA", "input_set_revision": 1,
                    "input_manifest_hash": "b" * 64, "inputs": inputs, "items": [item],
                    "platform_job": projection, "media_type": "application/json"}
        snapshot["export"] = json.dumps(export, ensure_ascii=False).encode()
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot",
                            lambda space, _handoff: snapshot if space == workspace else None)
        base = f"/api/platform/v1/workspaces/{workspace}/artifacts"
        payload = {"handoff_id": "handoffA"}
        assert client.post(base + "/ocr-exports", json=payload).status_code == 403
        created = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["kind"] == "ocr_result" and len(first["files"]) == 2
        replay = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json()["bundle_id"] == first["bundle_id"]
        def stale(*_args):
            raise OcrResultConflict()
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot", stale)
        assert client.post(base + "/ocr-exports", headers=LOCAL, json=payload).status_code == 409
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot",
                            lambda space, _handoff: snapshot if space == workspace else None)
        path = base + f"/{first['bundle_id']}/files/ocr-result.json"
        old = client.get(path)
        assert old.status_code == 200 and json.loads(old.content)["edited"]["text"] == "原始文字"
        provenance = client.get(base + f"/{first['bundle_id']}/files/provenance.json").json()
        assert provenance["results"][0]["asset_id"] == source["asset_id"]
        assert client.get(f"/api/platform/v1/workspaces/{other}/artifacts/{first['bundle_id']}").status_code == 404
        export["revision"] = item["revision"] = 1
        export["edited"]["text"] = "人工修订"
        snapshot["export"] = json.dumps(export, ensure_ascii=False).encode()
        changed = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert changed.status_code == 201 and changed.json()["bundle_id"] != first["bundle_id"]
        assert client.get(path).content == old.content
        listing = client.get(base).json()
        assert listing["total"] == 2


def test_pdf_pages_keep_document_page_source_and_require_full_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    native = {"import": None, "second_page": False}

    class FakeAdapter:
        def import_status(self, _request_id):
            if native["import"] is None:
                raise OcrUnavailable()
            return native["import"]

        def begin_import(self, request_id, manifest):
            digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            native["import"] = {"request_id": request_id, "payload_hash": digest,
                                "state": "prepared", "items": [{"item_id": manifest["items"][0]["item_id"],
                                                                   "status": "pending"}]}
            return native["import"]

        def import_item(self, *_args, **_kwargs):
            native["import"]["items"][0].update(
                status="imported", target_document_id="docA", page_count=2, page_numbers=[1, 2])
            native["import"]["state"] = "completed"
            return native["import"]

        def snapshot(self):
            images = [{"id": f"image{page}", "document_id": "docA",
                       "page_number": page,
                       "selected_result": f"result{page}" if page == 1 or native["second_page"] else None}
                      for page in (1, 2)]
            tasks = [{"id": f"task{page}", "kind": "document", "status": "succeeded",
                      "result_id": f"result{page}", "image_id": f"image{page}",
                      "version_id": f"version{page}", "engine": "pdf-native"}
                     for page in (1, 2)]
            return {"project": {"name": "文档项目"},
                    "documents": [{"id": "docA", "page_count": 2}],
                    "images": images, "tasks": tasks}

        def results(self, task_ids):
            return tuple(ResultArtifact(f"result{page}", f"task{page}",
                                        f"image{page}", f"version{page}", True,
                                        {"document_id": "docA", "page_number": page})
                         for page in (1, 2) if f"task{page}" in task_ids)

        def result_revision(self, _result):
            return 0

        def export_results(self, pinned):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for page, _ in enumerate(pinned, 1):
                    archive.writestr(f"{page:04d}.json",
                                     json.dumps({"id": f"result{page}", "revision": 0}))
            return buffer.getvalue(), "application/zip"

    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "PDF"}).json()["workspace_id"]
        asset_base = f"/api/platform/v1/workspaces/{workspace}/assets"
        asset = client.post(asset_base + "/uploads?name=双页.pdf", headers=LOCAL,
                            content=b"%PDF-1.7 fixture").json()
        fixed = client.post(asset_base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        handoff = app.state.ocr_handoff
        monkeypatch.setattr(handoff, "_context", lambda *_: SimpleNamespace(
            role="owner", module_project_id="projectA"))
        monkeypatch.setattr(handoff, "_adapter", lambda _: FakeAdapter())
        route = f"/api/platform/v1/workspaces/{workspace}/ocr/handoffs"
        transfer = client.post(route, headers=LOCAL,
                               json={"link_id": "linkA", "asset_set_id": fixed["asset_set_id"],
                                     "asset_set_revision": fixed["revision"]})
        assert transfer.status_code == 201 and transfer.json()["state"] == "completed"
        projection = {"platform_job_id": "platformPdfJob", "instance_id": "instanceA",
                      "operation_id": "operationA", "input_manifest_hash": "a" * 64,
                      "native_status": "succeeded", "status": "succeeded",
                      "stage": "PDF completed", "counts": {"total": 2, "succeeded": 2,
                      "failed": 0, "cancelled": 0, "pending": 0},
                      "last_event_seq": 1, "updated_at": "2026-09-28T00:00:00Z"}
        monkeypatch.setattr(app.state.job_service, "register", lambda *_: projection)
        base = f"/api/platform/v1/workspaces/{workspace}/artifacts"
        payload = {"handoff_id": transfer.json()["handoff_id"]}
        assert client.post(base + "/ocr-exports", headers=LOCAL, json=payload).status_code == 409
        native["second_page"] = True
        created = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        bundle = created.json()
        assert [item["page_number"] for item in bundle["items"]] == [1, 2]
        assert client.post(base + "/ocr-exports", headers=LOCAL,
                           json=payload).json()["bundle_id"] == bundle["bundle_id"]
        provenance = client.get(base + f"/{bundle['bundle_id']}/files/provenance.json").json()
        assert provenance["results"][1]["source_locator"]["page_number"] == 2
        archive = client.get(base + f"/{bundle['bundle_id']}/files/ocr-results.zip")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as exported:
            assert exported.namelist() == ["0001.json", "0002.json"]
