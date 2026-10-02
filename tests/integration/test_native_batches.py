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
    runner.jobs.action = lambda *_: {"status": "cancelling"}
    monkeypatch.setattr("workbench_platform.batches.native.time.sleep", lambda _: None)
    result = runner.run(batch, checkpoint, lambda: True)
    assert result["status"] == "cancelled"
    assert trace.count("native_progress") == 3
    assert trace.count("native_cancel") == 1
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


def test_review_only_preflight_reports_real_capability(tmp_path):
    runner, _, _, _, _, _ = fixture(tmp_path, scenario="ocr")
    runner.app.state.asset_library = (SimpleNamespace(get=lambda *_: SimpleNamespace(
        asset_id="assetA", original_name="page.png", revision=1, blob_hash="a" * 64)),)
    runner.options = lambda *_: {"recognition": False, "reason": "review_only", "engines": []}
    issues = runner.preflight({"workspace_id": "workspaceA", "scenario": "ocr", "config": {
        "link_id": "linkA", "engines": ["engineA"]}}, [{"asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}])
    assert any(item["category"] == "capability" and "仅校对" in item["detail"] for item in issues)
    assert all(isinstance(item["source"], dict) and isinstance(item["raw_output"], dict) for item in issues)
