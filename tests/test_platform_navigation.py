"""Platform entry is an ephemeral navigation hint, not native write authority."""

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from image_platform.api import create_app


NONCE = "n" + "a" * 32


def native_client(settings, monkeypatch):
    monkeypatch.setenv("WORKBENCH_APP_ID", "classification")
    monkeypatch.setenv("WORKBENCH_INSTANCE_ID", "instanceA")
    monkeypatch.setenv("WORKBENCH_NONCE", NONCE)
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765",
                      headers={"X-Platform-Request": "1"})


def context(**overrides):
    value = dict(app_id="classification", instance_id="instanceA", workspace_id="workspaceA",
                 link_id="linkA", module_project_id="p", asset_id=None,
                 return_route="/apps/classification", platform_origin="http://127.0.0.1:8766")
    return value | overrides


def issue(client, value, **headers):
    return client.post("/api/v1/platform/navigation/issue", json=value,
                       headers={"X-Workbench-Launch-Nonce": NONCE} | headers)


def test_one_use_context_maps_real_project_and_rejects_replay(settings, store, monkeypatch):
    with native_client(settings, monkeypatch) as client:
        issued = issue(client, context())
        assert issued.status_code == 201
        assert issued.headers["Cache-Control"] == "no-store"
        ticket = issued.json()["ticket"]
        assert len(ticket) >= 32 and "workspaceA" not in ticket
        opened = client.get(f"/api/v1/platform/navigation/tickets/{ticket}")
        assert opened.status_code == 200
        assert opened.json() == {"app_id": "classification", "instance_id": "instanceA",
                                 "workspace_id": "workspaceA", "link_id": "linkA",
                                 "module_project_id": "p", "asset_id": None,
                                 "return_url": "http://127.0.0.1:8766/apps/classification?workspace=workspaceA"}
        assert opened.headers["Referrer-Policy"] == "no-referrer"
        assert client.get(f"/api/v1/platform/navigation/tickets/{ticket}").status_code == 404


def test_optional_asset_requires_project_membership_and_does_not_change_labels(settings, store, monkeypatch):
    with native_client(settings, monkeypatch) as client:
        raw = BytesIO()
        Image.new("RGB", (32, 32), "red").save(raw, format="PNG")
        asset_id = client.post("/api/v1/projects/p/uploads", files=[("files", ("source.png", raw.getvalue(), "image/png"))]).json()["asset_ids"][0]
        with store.connect(write=True) as con:
            con.execute("INSERT INTO projects(id,name,labels,created_at) SELECT 'other',name,labels,created_at FROM projects WHERE id='p'")
        assert issue(client, context(module_project_id="other", asset_id=asset_id)).status_code == 400
        assert client.get(f"/api/v1/projects/other/assets/{asset_id}").status_code == 404
        assert client.get(f"/api/v1/projects/p/assets/{asset_id}").json()["name"] == "source.png"
        ticket = issue(client, context(asset_id=asset_id)).json()["ticket"]
        assert client.get(f"/api/v1/platform/navigation/tickets/{ticket}").json()["asset_id"] == asset_id
        assert store.one("SELECT label_id FROM assets WHERE id=?", (asset_id,))["label_id"] is None
        with store.connect(write=True) as con:
            con.execute("UPDATE assets SET archived=1 WHERE id=?", (asset_id,))
        assert client.get(f"/api/v1/projects/p/assets/{asset_id}").status_code == 404
        ticket = issue(client, context()).json()["ticket"]
        assert client.get(f"/api/v1/platform/navigation/tickets/{ticket}").status_code == 200
        assert issue(client, context(asset_id=asset_id)).status_code == 400


def test_launch_boundaries_and_invalid_origins(settings, store, monkeypatch):
    with native_client(settings, monkeypatch) as client:
        assert issue(client, context(), **{"X-Workbench-Launch-Nonce": "bad"}).status_code == 403
        assert issue(client, context(), Origin="https://untrusted.example").status_code == 403
        assert issue(client, context(), **{"X-Platform-Request": ""}).status_code == 403
        for invalid in (context(module_project_id="workspaceA"), context(instance_id="other"),
                        context(platform_origin="http://127.0.0.1:8766@evil.example"),
                        context(return_route="//evil.example"), context(return_route="/apps/classification?x=1"),
                        context(extra="field")):
            assert issue(client, invalid).status_code == 400
    monkeypatch.delenv("WORKBENCH_APP_ID")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765",
                    headers={"X-Platform-Request": "1"}) as independent:
        assert issue(independent, context()).status_code == 403
        assert independent.get("/api/v1/platform/navigation/tickets/" + "t" * 43).status_code == 404
