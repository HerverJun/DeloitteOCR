"""Native classification imports persist per-item identity across replays."""

import hashlib
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from image_platform.api import create_app
from image_platform.store import Store


def png():
    image = Image.new("RGB", (2, 2), "white")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def test_import_replay_keeps_two_sources_and_labels(store, client, settings):
    raw = png()
    digest = hashlib.sha256(raw).hexdigest()
    items = [{"item_id": f"item{number}", "asset_id": f"asset{number}",
              "blob_hash": digest, "size_bytes": len(raw), "name": f"来源{number}.png",
              "label_id": "a" if number == 1 else None} for number in (1, 2)]
    manifest = {"schema_version": "1.0", "asset_set_id": "setA",
                "asset_set_revision": 1, "manifest_hash": "a" * 64,
                "import_mode": "copy", "items": items}
    base = "/api/v1/platform/imports/p/requestA1"
    first = client.post(base, json=manifest)
    assert first.status_code == 201 and first.json()["state"] == "prepared"
    assert client.post(base, json=manifest).json() == first.json()
    conflict = client.post(base, json={**manifest, "manifest_hash": "b" * 64})
    assert conflict.status_code == 409
    assert client.put(base + "/items/item1", content=b"bad",
                      headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert client.get("/api/v1/projects/p/assets").json()["total"] == 0
    accepted = client.put(base + "/items/item1", content=raw,
                          headers={"Content-Type": "application/octet-stream"})
    assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"
    first_id = accepted.json()["items"][0]["target_asset_id"]
    assert first_id

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765",
                    headers={"X-Platform-Request": "1"}) as reopened:
        restored = reopened.get(base).json()
        assert restored["items"][0]["target_asset_id"] == first_id
        assert restored["items"][1]["status"] == "pending"
        replay = reopened.put(base + "/items/item1", content=raw,
                              headers={"Content-Type": "application/octet-stream"})
        assert replay.json()["items"][0]["target_asset_id"] == first_id
        completed = reopened.put(base + "/items/item2", content=raw,
                                 headers={"Content-Type": "application/octet-stream"})
        assert completed.status_code == 200 and completed.json()["state"] == "completed"
        second_id = completed.json()["items"][1]["target_asset_id"]
        assert second_id != first_id
        assets = reopened.get("/api/v1/projects/p/assets").json()
        assert assets["total"] == 2
        assert {row["source_id"] for row in assets["items"]} == {"asset1", "asset2"}
        assert {row["name"] for row in assets["items"]} == {"来源1.png", "来源2.png"}
        assert {row["label_id"] for row in assets["items"]} == {"a", None}
        with Store(settings).connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 5
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        with Store(settings).connect(write=True) as db:
            db.execute("UPDATE assets SET archived=1 WHERE id=?", (first_id,))
        stale = reopened.get(base).json()
        assert stale["state"] == "unknown" and stale["items"][0]["status"] == "missing"
