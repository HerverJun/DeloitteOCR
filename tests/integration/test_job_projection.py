"""Native-ledger and restart checks for durable task projections."""

import sys
from pathlib import Path

import pytest

from adapters.classification.adapter import JobStatus, ReviewItem
from adapters.ocr.adapter import OcrAdapter
from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.jobs.projection import (
    JobProjectionStore, ProjectionConflict, ProjectionUnavailable,
)

CLASSIFICATION_SRC = Path("E:/project/platform-dev-runs/dev-20260925-001/workspaces/classification/src")
OCR_SRC = Path("E:/project/platform-dev-runs/dev-20260925-001/workspaces/ocr/src")
sys.path.insert(0, str(CLASSIFICATION_SRC))
sys.path.insert(0, str(OCR_SRC))

from image_platform.settings import Settings  # noqa: E402
from image_platform.store import Store as ClassificationStore  # noqa: E402
from ocr_workbench.store import Store as OcrStore  # noqa: E402


def context(app_id="classification", workspace_id="workspaceA", instance_id="instanceA"):
    return ResolvedContext(workspace_id=workspace_id, app_id=app_id,
                           instance_id=instance_id, link_id="linkA",
                           module_project_id="projectA", role="editor",
                           return_route=f"/apps/{app_id}",
                           base_url="http://127.0.0.1:8765/")


def register(ledger, ctx, native_ids):
    return ledger.register(ctx, platform_job_id="platformJobA", operation_id="operationA",
                           input_manifest_hash="a" * 64, native_job_ids=native_ids,
                           generation=7)


def item(item_id, state):
    return ReviewItem(item_id, "assetA", state, None, None, None, None, None)


def classification_status(job_id, state, *, total, done, failed):
    return JobStatus(job_id, "inference", state, "native stage", total, done, failed,
                     ("retry_failed",) if state == "failed" else ("cancel",), False)


def test_classification_failed_retains_real_native_pending_items(tmp_path):
    """A native failed job leaves its unprocessed items pending for retry."""
    native = ClassificationStore(Settings(tmp_path / "classification"))
    with native.connect(write=True) as db:
        db.execute("INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)",
                   ("projectA", "Project", "[]", "2026-01-01T00:00:00+00:00"))
    job_id = native.submit("projectA", "inference", {"device": "cpu"},
                           ({"id": f"asset{number}", "path": f"input{number}",
                             "sha256": "a" * 64} for number in range(3)))
    owned = native.claim("test-worker")
    assert owned["id"] == job_id
    native.finish(job_id, owned["lease_token"], "failed", error="injected")
    job = native.job(job_id)
    rows = native.rows("SELECT id,state FROM job_items WHERE job_id=? ORDER BY id", (job_id,))
    assert job["state"] == "failed" and [row["state"] for row in rows] == ["pending"] * 3

    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    registered = register(ledger, ctx, [job_id])
    assert registered["status"] == "unknown"
    snapshot = classification_status(job_id, job["state"], total=job["total"],
                                     done=job["done"], failed=job["failed"])
    projected = ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                               native=snapshot, items=[item(row["id"], row["state"]) for row in rows])
    assert projected["status"] == "failed"
    assert projected["counts"] == {"total": 3, "succeeded": 0, "failed": 0,
                                   "cancelled": 0, "pending": 3}
    assert projected["capabilities"] == ["retry_failed"]
    assert projected["last_event_seq"] == 1
    # Reopening the platform ledger retains the old job and a durable resume cursor.
    reopened = JobProjectionStore(tmp_path / "jobs.sqlite")
    assert reopened.get("workspaceA", "platformJobA") == projected
    events = reopened.events_since("workspaceA", 0)
    assert [event["event_seq"] for event in events] == [0, 1]
    assert reopened.events_since("workspaceA", events[0]["cursor"])[0]["status"] == "failed"
    assert reopened.events_since("otherWorkspace", 0) == ()


def test_classification_all_failed_is_not_partial(tmp_path):
    native = ClassificationStore(Settings(tmp_path / "classification"))
    with native.connect(write=True) as db:
        db.execute("INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)",
                   ("projectA", "Project", "[]", "2026-01-01T00:00:00+00:00"))
    job_id = native.submit("projectA", "inference", {"device": "cpu"},
                           ({"id": f"asset{number}", "path": f"input{number}",
                             "sha256": "a" * 64} for number in range(2)))
    owned = native.claim("test-worker")
    with native.connect(write=True) as db:
        db.execute("UPDATE job_items SET state='failed',error='injected' WHERE job_id=?", (job_id,))
        db.execute("UPDATE jobs SET failed=2 WHERE id=?", (job_id,))
    native.finish(job_id, owned["lease_token"], "completed_with_errors")
    job = native.job(job_id)
    rows = native.rows("SELECT id,state FROM job_items WHERE job_id=? ORDER BY id", (job_id,))
    assert job["state"] == "completed_with_errors" and job["failed"] == 2

    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    register(ledger, ctx, [job_id])
    projected = ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                               native=classification_status(job_id, job["state"],
                                                            total=job["total"], done=job["done"],
                                                            failed=job["failed"]),
                               items=[item(row["id"], row["state"]) for row in rows])
    assert projected["native_status"] == "completed_with_errors"
    assert projected["status"] == "failed"
    assert projected["counts"] == {"total": 2, "succeeded": 0, "failed": 2,
                                   "cancelled": 0, "pending": 0}


def test_ocr_recovery_is_projected_as_paused_then_cancelled(tmp_path):
    native = OcrStore(tmp_path / "ocr")
    project_id = native.project("Project")["id"]
    image_id, version_id = "imageA", "versionA"
    with native.transaction() as db:
        db.execute("INSERT INTO images(id,project_id,name,original_path,sha256,active_version,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (image_id, project_id, "Image", "image.png", "a" * 64, version_id, "2026-01-01"))
        db.execute("INSERT INTO versions(id,image_id,path,width,height,sha256,operations,created) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (version_id, image_id, "version.png", 10, 10, "a" * 64, "[]", "2026-01-01"))
        for task_id, state in (("taskA", "running"), ("taskB", "queued")):
            db.execute("INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, project_id, image_id, version_id, "ppocr", "batchA",
                        state, "working", "2026-01-01"))
    native.recover()
    snapshot = native.project_snapshot(project_id)
    batch = OcrAdapter._batch_from_snapshot(["taskA", "taskB"], snapshot)
    assert {task.native_status for task in batch.tasks} == {"interrupted", "paused"}

    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context(app_id="ocr")
    register(ledger, ctx, ["taskA", "taskB"])
    projected = ledger.observe(ctx, "platformJobA", generation=7,
                               expected_event_seq=0, native=batch)
    assert projected["status"] == "paused" and projected["counts"]["pending"] == 2
    assert projected["capabilities"] == ["cancel"]
    with native.transaction() as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id IN ('taskA','taskB')")
    batch = OcrAdapter._batch_from_snapshot(["taskA", "taskB"], native.project_snapshot(project_id))
    projected = ledger.observe(ctx, "platformJobA", generation=7,
                               expected_event_seq=1, native=batch)
    assert projected["status"] == "cancelled" and projected["counts"]["cancelled"] == 2
    assert projected["capabilities"] == []


def test_late_generation_stale_cursor_and_terminal_reversal_are_rejected(tmp_path):
    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    register(ledger, ctx, ["jobA"])
    complete = classification_status("jobA", "succeeded", total=1, done=1, failed=0)
    ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                   native=complete, items=[item("itemA", "succeeded")])
    with pytest.raises(ProjectionConflict):
        ledger.observe(ctx, "platformJobA", generation=6, expected_event_seq=1,
                       native=complete, items=[item("itemA", "succeeded")])
    with pytest.raises(ProjectionConflict):
        ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                       native=complete, items=[item("itemA", "succeeded")])
    with pytest.raises(ProjectionConflict):
        ledger.observe(context(instance_id="instanceB"), "platformJobA", generation=7,
                       expected_event_seq=1, native=complete,
                       items=[item("itemA", "succeeded")])
    failed = classification_status("jobA", "failed", total=1, done=0, failed=0)
    with pytest.raises(ProjectionConflict):
        ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=1,
                       native=failed, items=[item("itemA", "pending")])
    assert ledger.get("workspaceA", "platformJobA")["status"] == "succeeded"
    assert len(ledger.events_since("workspaceA")) == 2


def test_transport_failure_is_unknown_and_never_invents_completion(tmp_path):
    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    register(ledger, ctx, ["jobA"])
    lost = ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                          failure="unreachable")
    assert lost["status"] == "unknown" and lost["availability"] == "unreachable"
    assert lost["capabilities"] == []
    running = classification_status("jobA", "running", total=1, done=0, failed=0)
    recovered = ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=1,
                               native=running, items=[item("itemA", "pending")])
    assert recovered["status"] == "running" and recovered["availability"] == "ready"
    with pytest.raises(ProjectionConflict):
        ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=2,
                       failure="secret/native-error-text")
    with pytest.raises(ProjectionUnavailable):
        ledger.get("anotherWorkspace", "platformJobA")


def test_registration_and_item_mismatch_fail_closed(tmp_path):
    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    register(ledger, ctx, ["jobA"])
    assert register(ledger, ctx, ["jobA"])["last_event_seq"] == 0
    with pytest.raises(ProjectionConflict):
        register(ledger, ctx, ["jobB"])
    state = classification_status("jobA", "failed", total=2, done=0, failed=0)
    with pytest.raises(ProjectionConflict):
        ledger.observe(ctx, "platformJobA", generation=7, expected_event_seq=0,
                       native=state, items=[item("onlyOne", "pending")])
    assert ledger.get("workspaceA", "platformJobA")["last_event_seq"] == 0


def test_training_progress_is_not_misreported_as_item_coverage(tmp_path):
    ledger = JobProjectionStore(tmp_path / "jobs.sqlite")
    ctx = context()
    register(ledger, ctx, ["trainA"])
    native = JobStatus("trainA", "train", "running", "epoch 3", 10, 3, 0,
                       ("cancel",), False)
    projected = ledger.observe(ctx, "platformJobA", generation=7,
                               expected_event_seq=0, native=native)
    assert projected["counts"]["total"] is None
    assert projected["progress_done"] == 3 and projected["progress_total"] == 10
