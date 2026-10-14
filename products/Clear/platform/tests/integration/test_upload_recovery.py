"""D16 actual ASGI binary upload and durable SQLite recovery boundaries."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from packages.contracts.models import Error
from packages.contracts.uploads import UPLOAD_MAX_BYTES, UploadBinding, UploadStatus
from workbench_platform.assets.registry import AssetChanged, AssetRegistry, UploadConflict
from workbench_platform.assets.routes import router
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.store import WorkspaceStore

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766", "content-type": "application/octet-stream"}


@pytest.fixture
def uploads(tmp_path):
    uploaded, blobs = tmp_path / "uploads", tmp_path / "blobs"
    uploaded.mkdir()
    blobs.mkdir()
    registry = AssetRegistry(tmp_path / "assets.sqlite3", blobs, {"uploads": uploaded})
    spaces = WorkspaceStore(tmp_path / "workspaces.sqlite3")
    spaces.create_workspace("甲", workspace_id="workspaceA")
    spaces.create_workspace("乙", workspace_id="workspaceB")
    app = FastAPI()
    app.include_router(router)
    app.state.asset_library = (registry, AssetSetStore(registry), uploaded)
    app.state.workspace_store = spaces
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        yield client, app, registry, uploaded, blobs


def binding(request_id="uploadA", name="资料 甲.csv", body=b"fixed bytes", workspace_id="workspaceA"):
    return UploadBinding(schema_version="1.0", workspace_id=workspace_id, request_id=request_id,
                         name=name, sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))


def path(value):
    return f"/api/platform/v1/workspaces/{value.workspace_id}/assets/uploads/{value.request_id}"


def post(client, value, body=b"fixed bytes", **kwargs):
    params = {key: getattr(value, key) for key in ("name", "sha256", "size_bytes")}
    return client.post(path(value), headers=LOCAL, params=params, content=body, **kwargs)


def stage(uploaded, name, body=b"fixed bytes"):
    source = uploaded / (".incoming-" + name)
    source.write_bytes(body)
    return source


def test_d16_real_binary_post_get_replay_fixed_receipt_and_legacy_compatibility(uploads):
    client, _, registry, uploaded, blobs = uploads
    value = binding()
    assert client.get(path(value)).status_code == 404
    first = post(client, value)
    assert first.status_code == 201, first.text
    fixed = UploadStatus.model_validate(first.json())
    assert fixed.status == "committed" and fixed.receipt.asset.original_name == value.name
    assert fixed.receipt.asset.blob_hash == value.sha256 and fixed.receipt.asset.revision == 1
    assert client.get(path(value)).json() == first.json()
    replay = post(client, value)
    assert replay.status_code == 200 and replay.json() == first.json()
    asset = fixed.receipt.asset
    source = registry.source(value.workspace_id, asset.asset_id)
    assert (uploaded / source["relative_path"]).read_bytes() == b"fixed bytes"
    registry.set_tags(value.workspace_id, asset.asset_id, ["人工标签"])
    assert post(client, value).json() == first.json()
    legacy = client.post("/api/platform/v1/workspaces/workspaceA/assets/uploads", headers=LOCAL,
                         params={"name": "旧入口.txt"}, content=b"fixed bytes")
    assert legacy.status_code == 201 and legacy.json()["asset_id"] != asset.asset_id
    assert legacy.json()["blob_hash"] == asset.blob_hash
    assert registry.list("workspaceA")[1] == 2
    assert len(list(blobs.glob("[0-9a-f][0-9a-f]/[0-9a-f]*"))) == 1
    assert not list(uploaded.glob(".incoming-*"))


@pytest.mark.parametrize("change", [{"name": "另一个.csv"}, {"sha256": "f" * 64}, {"size_bytes": 1}])
def test_d16_same_request_different_binding_preserves_original_commit(uploads, change):
    client, _, registry, uploaded, _ = uploads
    value = binding()
    fixed = post(client, value).json()
    altered = UploadBinding(**{**value.model_dump(), **change})
    response = post(client, altered)
    assert response.status_code == 409
    assert Error.model_validate(response.json()).code == "IDEMPOTENCY_CONFLICT"
    assert client.get(path(value)).json() == fixed and registry.list("workspaceA")[1] == 1
    assert not list(uploaded.glob(".incoming-*"))


@pytest.mark.parametrize("bad_body", [b"different!!", b"short", b"fixed bytes plus extra"])
def test_d16_body_mismatch_is_rejected_before_registration_and_original_binding_can_retry(uploads, bad_body):
    client, _, registry, uploaded, _ = uploads
    value = binding()
    failed = post(client, value, bad_body)
    assert failed.status_code == 409 and Error.model_validate(failed.json()).code == "INTEGRITY_MISMATCH"
    rejected = UploadStatus.model_validate(client.get(path(value)).json())
    assert rejected.status == "rejected" and rejected.error.details["effect"] == "no_registration"
    assert registry.list("workspaceA")[1] == 0 and not list(uploaded.iterdir())
    completed = post(client, value)
    assert completed.status_code == 201
    assert post(client, value, bad_body).status_code == 409
    assert client.get(path(value)).json() == completed.json() and registry.list("workspaceA")[1] == 1


def test_d16_lost_reply_after_commit_is_queryable_after_registry_reload(uploads, monkeypatch):
    client, app, registry, uploaded, blobs = uploads
    value = binding()
    commit = registry.commit_upload

    def lose_reply(*args, **kwargs):
        commit(*args, **kwargs)
        raise OSError("response lost after durable commit")

    monkeypatch.setattr(registry, "commit_upload", lose_reply)
    response = post(client, value)
    assert response.status_code == 503
    restored = AssetRegistry(registry.db_path, blobs, {"uploads": uploaded})
    app.state.asset_library = (restored, AssetSetStore(restored), uploaded)
    fixed = client.get(path(value)).json()
    assert UploadStatus.model_validate(fixed).status == "committed"
    replay = post(client, value)
    assert replay.status_code == 200 and replay.json() == fixed
    assert restored.list("workspaceA")[1] == 1 and len(list(uploaded.iterdir())) == 1


def test_d16_interrupted_stream_leaves_pending_then_same_request_resumes(uploads):
    client, _, registry, uploaded, _ = uploads
    value = binding()

    def interrupted():
        yield b"fixed "
        raise OSError("connection interrupted")

    response = post(client, value, interrupted())
    assert response.status_code == 503
    pending = UploadStatus.model_validate(client.get(path(value)).json())
    assert pending.status == "pending" and pending.receipt is None
    assert registry.list("workspaceA")[1] == 0 and not list(uploaded.iterdir())
    assert post(client, value).status_code == 201
    assert registry.list("workspaceA")[1] == 1


def test_d16_two_registry_instances_concurrently_commit_one_asset_and_source(uploads):
    _, _, first, uploaded, blobs = uploads
    second = AssetRegistry(first.db_path, blobs, {"uploads": uploaded})
    value = binding()
    first.reserve_upload(value)
    second.reserve_upload(value)
    staged = [stage(uploaded, "attemptA"), stage(uploaded, "attemptB")]
    gate = threading.Barrier(2)

    def commit(registry, index):
        gate.wait(timeout=5)
        return registry.commit_upload(value, staged[index], media_type="text/csv")

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(commit, first, 0), workers.submit(commit, second, 1)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert first.list("workspaceA")[1] == second.list("workspaceA")[1] == 1
    with sqlite3.connect(first.db_path) as db:
        assert db.execute("SELECT count(*) FROM asset_upload_request").fetchone()[0] == 1
        assert db.execute("SELECT count(distinct source_id) FROM registered_asset").fetchone()[0] == 1
    for source in staged:
        source.unlink()
    assert len(list(uploaded.iterdir())) == 1


def test_d16_registration_and_receipt_roll_back_together_then_resume_original_source(uploads, monkeypatch):
    client, app, registry, uploaded, blobs = uploads
    value = binding()
    save = registry._save_upload_in

    def fail_receipt(db, status):
        if status.status == "committed":
            assert db.execute("SELECT count(*) FROM registered_asset").fetchone()[0] == 1
            raise sqlite3.OperationalError("receipt storage failed")
        return save(db, status)

    monkeypatch.setattr(registry, "_save_upload_in", fail_receipt)
    assert post(client, value).status_code == 503
    assert registry.list("workspaceA")[1] == 0 and not list(uploaded.iterdir())
    restored = AssetRegistry(registry.db_path, blobs, {"uploads": uploaded})
    app.state.asset_library = (restored, AssetSetStore(restored), uploaded)
    assert UploadStatus.model_validate(client.get(path(value)).json()).status == "pending"
    expected_relative, _ = restored._upload_source(value)
    committed = post(client, value)
    assert committed.status_code == 201
    asset = UploadStatus.model_validate(committed.json()).receipt.asset
    assert restored.source("workspaceA", asset.asset_id)["relative_path"] == expected_relative
    assert restored.list("workspaceA")[1] == 1


@pytest.mark.parametrize("name", ["../逃逸.txt", "A/B.txt", "NUL.txt", "COM¹.log", "a?.csv", "b|c.csv", "尾点."])
def test_d16_strict_single_filename_rejected_without_reservation_or_staging(uploads, name):
    client, _, registry, uploaded, _ = uploads
    value = binding()
    params = {"name": name, "sha256": value.sha256, "size_bytes": value.size_bytes}
    response = client.post(path(value), headers=LOCAL, params=params, content=b"fixed bytes")
    assert response.status_code == 400
    assert client.get(path(value)).status_code == 404 and registry.list("workspaceA")[1] == 0
    assert not list(uploaded.iterdir())


def test_d16_size_query_binary_type_and_origin_limits_leave_no_effect(uploads):
    client, _, registry, uploaded, _ = uploads
    value = binding()
    params = {"name": value.name, "sha256": value.sha256, "size_bytes": value.size_bytes}
    too_big = client.post(path(value), headers=LOCAL, params={**params, "size_bytes": UPLOAD_MAX_BYTES + 1}, content=b"x")
    assert too_big.status_code == 413
    repeated = client.post(path(value), headers=LOCAL, params=[*params.items(), ("name", "other.csv")], content=b"fixed bytes")
    assert repeated.status_code == 400
    not_binary = client.post(path(value), headers={**LOCAL, "content-type": "multipart/form-data"}, params=params, content=b"fixed bytes")
    assert not_binary.status_code == 400
    outside = client.post(path(value), headers={**LOCAL, "origin": "https://outside.invalid"}, params=params, content=b"fixed bytes")
    assert outside.status_code == 403
    assert client.get(path(value)).status_code == 404 and registry.list("workspaceA")[1] == 0
    assert not list(uploaded.iterdir())


def test_d16_workspace_and_request_identity_are_distinct_while_blobs_are_reused(uploads):
    client, _, registry, _, blobs = uploads
    values = [binding(), binding("uploadB"), binding(workspace_id="workspaceB")]
    receipts = [UploadStatus.model_validate(post(client, value).json()).receipt for value in values]
    assert len({receipt.asset.asset_id for receipt in receipts}) == 3
    assert len({receipt.asset.source_id for receipt in receipts}) == 3
    assert len({receipt.asset.blob_hash for receipt in receipts}) == 1
    assert registry.list("workspaceA")[1] == 2 and registry.list("workspaceB")[1] == 1
    assert len(list(blobs.glob("[0-9a-f][0-9a-f]/[0-9a-f]*"))) == 1
