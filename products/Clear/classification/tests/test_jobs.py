import time

import pytest

from image_platform.worker import commit_results


def submit(store):
    return store.submit("p", "inference", {"model_id": "model"}, [{"id": f"asset_{index}", "path": f"{index}.png", "sha256": "a" * 64} for index in range(3)])


def test_exclusive_worker_and_idempotent_submission(store):
    first = store.submit("p", "train", {"config": 1}, key="request")
    assert store.submit("p", "train", {"config": 1}, key="request") == first
    with pytest.raises(ValueError, match="幂等"):
        store.submit("p", "train", {"config": 2}, key="request")
    assert store.claim("one")["id"] == first
    assert store.claim("two") is None


def test_restart_skips_committed_results_and_fences_old_worker(store):
    job_id = submit(store)
    job = store.claim("one")
    item = store.rows("SELECT * FROM job_items WHERE job_id=? ORDER BY id", (job_id,))[0]
    commit_results(store, job, [(item, {"label_id": "a", "score": 0.9}, None)])
    commit_results(store, job, [(item, {"label_id": "b", "score": 0.8}, None)])
    assert store.job(job_id)["done"] == 1
    with store.connect(write=True) as con:
        con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job_id))
        con.execute("UPDATE leases SET expires=?", (time.time() - 1,))
    new_job = store.claim("two")
    assert new_job["id"] == job_id
    assert new_job["lease_token"] != job["lease_token"]
    assert len(store.rows("SELECT * FROM job_items WHERE state='pending'")) == 2
    with pytest.raises(RuntimeError, match="租约"):
        commit_results(store, job, [(item, {"label_id": "b"}, None)])


def test_pause_resume_cancel_state_transitions(store):
    job_id = submit(store)
    assert store.action(job_id, "pause")["state"] == "paused"
    assert store.claim("one") is None
    assert store.action(job_id, "resume")["state"] == "queued"
    job = store.claim("one")
    assert store.action(job_id, "pause")["state"] == "pausing"
    store.finish(job_id, job["lease_token"], "paused")
    assert store.action(job_id, "cancel")["state"] == "cancelled"
    assert len(store.rows("SELECT * FROM job_items WHERE state='cancelled'")) == 3
    with pytest.raises(ValueError):
        store.action(job_id, "resume")


def test_crashed_training_is_failed_without_automatic_retraining(store):
    job_id = store.submit("p", "train", {})
    job = store.claim("one")
    with store.connect(write=True) as con:
        con.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job_id))
    store.recover()
    assert store.job(job_id)["state"] == "failed"
    assert not store.heartbeat(job_id, job["lease_token"])
