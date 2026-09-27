"""PDF handoff commits one native document with ordered page provenance."""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from ocr_workbench.service import create_app


def test_pdf_replay_preserves_document_and_page_numbers(tmp_path):
    bundle = tmp_path / "bundle"
    config = bundle / "config"
    config.mkdir(parents=True)
    (config / "engines.json").write_text("{}", encoding="utf-8")
    app = create_app(bundle, tmp_path / "data", "t" * 40, start_queue=False,
                     review_only=True, agent_enabled=False)
    metadata = {"pages": [{"page_number": number, "crop_box": [0, 0, 100, 100],
                           "render_dpi": 300} for number in (1, 2)],
                "encrypted": False}
    app.state.documents.cpu.call = lambda request: metadata
    project_id = app.state.store.project("PDF 项目")["id"]
    content = b"%PDF-fixture-bytes"
    manifest = {"schema_version": "1.0", "asset_set_id": "setA",
                "asset_set_revision": 1, "manifest_hash": "a" * 64,
                "import_mode": "copy", "items": [{"item_id": "itemA", "asset_id": "assetA",
                "blob_hash": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
                "name": "两页资料.pdf"}]}
    base = f"/api/platform/imports/{project_id}/requestA1"
    headers = {"Authorization": "Bearer " + "t" * 40}
    with TestClient(app) as client:
        created = client.post(base, headers=headers, json=manifest)
        assert created.status_code == 200 and created.json()["state"] == "prepared"
        accepted = client.put(base + "/items/itemA", headers={**headers,
                              "Content-Type": "application/octet-stream"}, content=content)
        assert accepted.status_code == 200, accepted.text
        item = accepted.json()["items"][0]
        assert item["status"] == "imported" and item["page_count"] == 2
        assert item["page_numbers"] == [1, 2]
        document_id = item["target_document_id"]
        replay = client.put(base + "/items/itemA", headers={**headers,
                            "Content-Type": "application/octet-stream"}, content=content)
        assert replay.status_code == 200 and replay.json()["items"][0]["target_document_id"] == document_id
        pages = client.get(f"/api/documents/{document_id}/pages", headers=headers).json()
        assert [page["page_number"] for page in pages["pages"]] == [1, 2]
        assert len(app.state.store.rows("SELECT id FROM documents WHERE id=?", (document_id,))) == 1
    reopened = create_app(bundle, tmp_path / "data", "t" * 40, start_queue=False,
                          review_only=True, agent_enabled=False)
    restored = reopened.state.platform_imports.status(project_id, "requestA1")
    assert restored["items"][0]["target_document_id"] == document_id
    assert restored["items"][0]["page_numbers"] == [1, 2]
