from types import SimpleNamespace

import pytest

from workbench_platform.batches.native import NativeBatchError, NativeBatchRunner


def fixture(tmp_path, *, scenario="classification"):
    trace = []
    job = {"platform_job_id": "jobA", "native_job_ids": ["nativeA"], "status": "succeeded",
           "availability": "ready", "capabilities": [], "counts": {"total": 1, "succeeded": 1,
           "failed": 0, "pending": 0, "cancelled": 0}}
    snapshot = {"workspace_id": "workspaceA", "handoff_id": "handoffA", "items": [
        {"native_item_id": "itemA", "asset_id": "assetA", "state": "succeeded",
         "prediction_label_id": "a", "prediction_status": "needs_review", "prediction_score": 0.7}],
        "inputs": [{"asset_id": "assetA", "name": "source.png"}]}
    def result_snapshot(*_):
        trace.append("snapshot")
        return snapshot
    def submit(*args, **kwargs):
        trace.append(("submit", kwargs))
        return {"not_submitted": True} if not kwargs.get("allow_new", True) else {"job": job}
    handoff = SimpleNamespace(result_snapshot=result_snapshot, submit=submit)
    def publish(*_, snapshot):
        trace.append(("publish", snapshot["items"][0]["prediction_label_id"]))
        if len([event for event in trace if isinstance(event, tuple) and event[0] == "publish"]) == 1:
            raise OSError("collection unavailable")
        return {"bundle_id": "bundleA"}
    artifacts = SimpleNamespace(publish=publish, get=lambda *_: {"bundle_id": "bundleA"})
    jobs = SimpleNamespace(ledger=SimpleNamespace(get=lambda *_: job), refresh=lambda *_: job)
    blob_root = tmp_path / "blobs"
    blob_root.mkdir()
    state = SimpleNamespace(job_service=jobs, classification_handoff=handoff, ocr_handoff=handoff,
        artifact_center=SimpleNamespace(store=SimpleNamespace(blob_root=blob_root), classification=artifacts, ocr=artifacts))
    batch = {"batch_id": "batchA", "workspace_id": "workspaceA", "template": {
        "scenario": scenario, "config": {"link_id": "linkA", "model_id": "modelA", "device": "cpu", "engines": ["engineA"]}},
        "inputs": [], "receipts": {"native_inputs": {"asset_set_id": "setA", "revision": 1},
        "native_import": {"handoff_id": "handoffA", "state": "completed"}, "native_submission": job}}
    def checkpoint(stage, receipt):
        batch["receipts"][stage] = receipt
        trace.append(stage)
    return NativeBatchRunner(SimpleNamespace(state=state)), batch, checkpoint, job, snapshot, trace


def test_collection_retry_uses_pinned_output_without_recomputing(tmp_path):
    runner, batch, checkpoint, job, snapshot, trace = fixture(tmp_path)
    first = runner.run(batch, checkpoint, lambda: False)
    assert first["artifact_state"] == "collection_failed" and first["status"] == "partial"
    snapshot["items"][0]["prediction_label_id"] = "changed-after-pin"
    second = runner.run(batch, checkpoint, lambda: False)
    assert second["artifact_state"] == "collected"
    assert second["issues"][0]["raw_output"]["prediction_label_id"] == "a"
    assert trace.count("snapshot") == 1
    assert [event for event in trace if isinstance(event, tuple) and event[0] == "publish"] == [("publish", "a"), ("publish", "a")]
    runner.run(batch, checkpoint, lambda: False)
    assert len([event for event in trace if isinstance(event, tuple) and event[0] == "publish"]) == 2


def test_lost_submit_response_cancel_queries_before_any_new_submission(tmp_path):
    runner, batch, checkpoint, _, _, trace = fixture(tmp_path)
    del batch["receipts"]["native_submission"]
    batch["receipts"]["native_dispatch"] = {"status": "queued"}
    result = runner.run(batch, checkpoint, lambda: True)
    assert result["status"] == "cancelled"
    assert ("submit", {"allow_new": False}) in trace
    assert "snapshot" not in trace


def test_cancel_waits_for_true_terminal_and_never_collects(tmp_path, monkeypatch):
    runner, batch, checkpoint, job, _, trace = fixture(tmp_path)
    polls = iter(("running", "cancelling", "cancelled"))
    def refresh(*_):
        return {**job, "status": next(polls), "capabilities": ["cancel"]}
    runner.jobs.refresh = refresh
    actions = []
    def action(*args):
        actions.append(args)
        return {"status": "cancelling"}
    runner.jobs.action = action
    monkeypatch.setattr("workbench_platform.batches.native.time.sleep", lambda _: None)
    def cancelled():
        return True
    cancelled.stopping = lambda: False
    result = runner.run(batch, checkpoint, cancelled)
    assert result["status"] == "cancelled"
    assert trace.count("native_progress") == 3
    assert trace.count("native_cancel") == 1
    assert actions == [("workspaceA", "jobA", "cancel")]
    assert "snapshot" not in trace


def test_shutdown_preserves_native_task_for_service_recovery(tmp_path):
    runner, batch, checkpoint, _, _, trace = fixture(tmp_path)
    def cancelled():
        return True
    cancelled.stopping = lambda: True
    with pytest.raises(NativeBatchError) as error:
        runner.run(batch, checkpoint, cancelled)
    assert error.value.status == "queued"
    assert not trace


@pytest.mark.parametrize("shutdown_at", ("refresh", "checkpoint", "cancel_callback"))
def test_shutdown_during_native_poll_never_cancels_and_resumes_same_receipt(tmp_path, shutdown_at):
    runner, batch, checkpoint, job, _, trace = fixture(tmp_path)
    closing = False
    polls = 0
    submitted = batch["receipts"]["native_submission"]
    actions = []

    def refresh(*_):
        nonlocal closing, polls
        polls += 1
        if shutdown_at == "refresh":
            closing = True
        return {**job, "status": "running", "capabilities": ["cancel"]}

    def durable_checkpoint(stage, receipt):
        nonlocal closing
        checkpoint(stage, receipt)
        if stage == "native_progress" and shutdown_at == "checkpoint":
            closing = True

    def cancelled():
        nonlocal closing
        # BatchService's existing callback combines closing and explicit cancel.
        if polls and shutdown_at == "cancel_callback":
            closing = True
        return closing

    cancelled.stopping = lambda: closing
    runner.jobs.refresh = refresh
    runner.jobs.action = lambda *args: actions.append(args)
    with pytest.raises(NativeBatchError) as error:
        runner.run(batch, durable_checkpoint, cancelled)
    assert error.value.status == "queued"
    assert not actions and "native_cancel" not in batch["receipts"]
    assert batch["receipts"]["native_submission"] is submitted
    assert batch["receipts"]["native_progress"]["status"] == "running"
    assert "snapshot" not in trace

    closing = False
    runner.jobs.refresh = lambda *_: job
    runner.app.state.artifact_center.classification.publish = lambda *_, **__: {"bundle_id": "bundleA"}
    recovered = runner.run(batch, checkpoint, lambda: False)
    assert recovered["status"] == "succeeded" and recovered["artifact_state"] == "collected"
    assert batch["receipts"]["native_submission"] is submitted
    assert not actions
    assert not any(isinstance(event, tuple) and event[0] == "submit" for event in trace)


@pytest.mark.parametrize("native_state", ("cancelled", "failed"))
def test_unfinished_classification_items_are_execution_issues_not_model_suggestions(tmp_path, native_state):
    runner, batch, checkpoint, job, snapshot, _ = fixture(tmp_path)
    job.update(status=native_state, counts={"total": 1, "succeeded": 0, "pending": 0,
               "failed": int(native_state == "failed"), "cancelled": int(native_state == "cancelled")})
    snapshot["items"][0].update(state=native_state, prediction_label_id=None,
                               prediction_status=None, prediction_score=None)
    runner.app.state.artifact_center.classification.publish = lambda *_, **__: {"bundle_id": "bundleA"}
    result = runner.run(batch, checkpoint, lambda: False)
    assert result["status"] == native_state
    assert result["summary"]["execution_status"] == native_state
    assert result["summary"]["needs_review"] == 0
    counts = result["summary"]
    assert counts["total"] == sum(counts[key] for key in ("succeeded", "failed", "cancelled", "pending"))
    assert len(result["issues"]) == 1 and result["issues"][0]["category"] == "execution"
    assert result["issues"][0]["raw_output"]["state"] == native_state
    assert result["issues"][0]["source"]["name"] == "source.png"


def test_review_only_preflight_reports_real_capability(tmp_path):
    runner, _, _, _, _, _ = fixture(tmp_path, scenario="ocr")
    runner.app.state.asset_library = (SimpleNamespace(get=lambda *_: SimpleNamespace(
        asset_id="assetA", original_name="page.png", revision=1, blob_hash="a" * 64)),)
    runner.options = lambda *_: {"recognition": False, "reason": "review_only", "engines": []}
    issues = runner.preflight({"workspace_id": "workspaceA", "scenario": "ocr", "config": {
        "link_id": "linkA", "engines": ["engineA"]}}, [{"asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}])
    assert any(item["category"] == "capability" and "仅校对" in item["detail"] for item in issues)
    assert all(isinstance(item["source"], dict) and isinstance(item["raw_output"], dict) for item in issues)


def test_lost_input_set_receipt_reuses_fixed_source_identity(tmp_path):
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetConflict, AssetSetStore
    inputs, blobs = tmp_path / "inputs", tmp_path / "blobs"
    inputs.mkdir()
    blobs.mkdir()
    (inputs / "one.png").write_bytes(b"source-one")
    (inputs / "two.png").write_bytes(b"source-two")
    registry = AssetRegistry(tmp_path / "assets.sqlite3", blobs, {"inputs": inputs})
    first = registry.register("workspaceA", "inputs", "one.png", media_type="image/png", storage_mode="managed")
    second = registry.register("workspaceA", "inputs", "two.png", media_type="image/png", storage_mode="managed")
    sets = AssetSetStore(registry)
    def member(asset):
        identity = {"asset_id": asset.asset_id, "asset_revision": asset.revision, "blob_hash": asset.blob_hash}
        return {"schema_version": "1.0", "item_id": "itemA", **identity, "source_locator": {
            "schema_version": "1.0", "kind": "file", "precision": "unknown", **identity}}
    original = sets.create("workspaceA", [member(first)], asset_set_id="setBatchA")
    assert sets.create("workspaceA", [member(first)], asset_set_id="setBatchA") == original
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [member(second)], asset_set_id="setBatchA")
