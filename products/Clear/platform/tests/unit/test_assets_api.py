"""Asset library real-HTTP upload, search, origin and fixed-input behavior."""

from fastapi.testclient import TestClient

from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def workspace(client, name):
    response = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                           json={"schema_version": "1.0", "name": name})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def test_upload_search_tags_page_and_fixed_set(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        first_space = workspace(client, "甲")
        second_space = workspace(client, "乙")
        base = f"/api/platform/v1/workspaces/{first_space}/assets"
        other = f"/api/platform/v1/workspaces/{second_space}/assets"
        first = client.post(base + "/uploads", params={"name": "发票 甲.pdf"},
                            headers=LOCAL, content=b"same bytes")
        second = client.post(base + "/uploads", params={"name": "发票 乙.pdf"},
                             headers=LOCAL, content=b"same bytes")
        assert first.status_code == second.status_code == 201
        a, b = first.json(), second.json()
        assert a["asset_id"] != b["asset_id"]
        assert a["blob_hash"] == b["blob_hash"]
        assert a["source"]["root_id"] == "uploads"
        assert a["source"]["relative_path"].endswith("/发票 甲.pdf")

        tag = client.put(base + f"/{a['asset_id']}/tags", headers=LOCAL,
                         json={"tags": ["财务", "九月"]})
        assert tag.status_code == 200
        found = client.get(base, params={"keyword": "发票 甲", "tag": "财务",
                                          "media_type": "application/pdf"}).json()
        assert found["total"] == 1 and found["items"][0]["asset_id"] == a["asset_id"]
        assert "财务" in found["facets"]["tags"]
        assert client.get(base, params={"keyword": "九月"}).json()["total"] == 1
        page = client.get(base, params={"limit": 1}).json()
        assert page["total"] == 2 and page["next_offset"] == 1
        assert len(client.get(base, params={"limit": 1, "offset": 1}).json()["items"]) == 1
        assert client.get(other).json()["total"] == 0
        assert client.get(other + f"/{a['asset_id']}").status_code == 404

        original = client.get(base + f"/{a['asset_id']}/content", params={"revision": 1})
        assert original.status_code == 200 and original.content == b"same bytes"
        assert original.headers["content-disposition"].startswith("inline;")
        assert client.get(base + f"/{a['asset_id']}/content", params={"revision": 2}).status_code == 409
        fixed = client.post(base + "/sets", headers=LOCAL,
                            json={"asset_ids": [b["asset_id"], a["asset_id"]]})
        assert fixed.status_code == 201
        saved = client.get(base + "/sets/" + fixed.json()["asset_set_id"]).json()
        assert [item["asset_id"] for item in saved["members"]] == [b["asset_id"], a["asset_id"]]
        assert saved["revision"] == 1


def test_upload_and_mutations_require_same_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        space = workspace(client, "安全")
        base = f"/api/platform/v1/workspaces/{space}/assets"
        assert client.post(base + "/uploads?name=bad.txt", content=b"data").status_code == 403
        assert client.post(base + "/uploads?name=../bad.txt", headers=LOCAL,
                           content=b"data").status_code == 400
        assert client.post(base + "/uploads?name=empty.txt", headers=LOCAL,
                           content=b"").status_code == 400
        assert client.get(base).json()["total"] == 0
