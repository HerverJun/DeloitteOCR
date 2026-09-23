import numpy as np

from image_platform import worker
from image_platform.store import now


class Session:
    def __init__(self, store=None, pause_on=None):
        self.calls = 0
        self.store, self.pause_on, self.job_id = store, pause_on, None

    def run(self, outputs, feeds):
        self.calls += 1
        count = len(feeds["images"])
        if self.pause_on == self.calls:
            self.store.action(self.job_id, "pause")
        if count > 2:
            raise RuntimeError("CUDA out of memory")
        return [np.tile(np.array([[2.0, 1.0]], dtype=np.float32), (count, 1))]

    def get_providers(self):
        return ["CUDAExecutionProvider"]


def prepare_job(store, monkeypatch, session, total=13, bad=True):
    with store.connect(write=True) as con:
        con.execute("INSERT INTO models VALUES('m','p','j','model','fake','{}','{}',?)", (now(),))
    metadata = {"provenance": {"model_onnx_sha256": "hash"}, "input": {"height": 1, "width": 1}, "preprocessing": {},
                "labels": [{"class_index": 0, "label_id": "a", "display_name": "A"}, {"class_index": 1, "label_id": "b", "display_name": "B"}],
                "decision_policy": {"version": "test", "thresholds_by_label_id": {"a": 0.8, "b": 0.8}}}
    monkeypatch.setattr(worker, "validate_package", lambda _: metadata)
    monkeypatch.setattr(worker.WORKER_SESSIONS, "get", lambda *args, **kwargs: (session, False))
    def preprocess(path, *args):
        if path == "bad":
            raise ValueError("input_changed")
        return np.zeros((3, 1, 1), dtype=np.float32)
    monkeypatch.setattr(worker, "preprocess", preprocess)
    items = [{"id": f"asset-{i}", "path": "bad" if bad and i == 0 else str(i), "sha256": "sha"} for i in range(total)]
    job_id = store.submit("p", "inference", {"model_id": "m", "model_sha256": "hash", "device": "cuda", "batch_size": 4, "auto_accept": False}, items)
    session.job_id = job_id
    return job_id


def test_prefetch_oom_and_bad_image_do_not_skip_or_duplicate_rows(store, monkeypatch):
    session = Session()
    job_id = prepare_job(store, monkeypatch, session)
    worker.run_job(store, store.claim("test"))
    job = store.job(job_id)
    assert (job["state"], job["done"], job["failed"]) == ("completed_with_errors", 12, 1)
    assert job["result"]["batch_size_final"] == 2
    assert job["result"]["prefetch_batches"] == 1
    assert all(row["attempts"] == 1 for row in store.rows("SELECT attempts FROM job_items WHERE job_id=?", (job_id,)))


def test_prefetch_pause_and_resume_preserve_committed_batches(store, monkeypatch):
    session = Session(store, pause_on=4)
    job_id = prepare_job(store, monkeypatch, session, total=12, bad=False)
    worker.run_job(store, store.claim("test"))
    paused = store.job(job_id)
    assert paused["state"] == "paused" and paused["done"] == 4
    assert len(store.rows("SELECT id FROM job_items WHERE job_id=? AND state='pending'", (job_id,))) == 8
    session.pause_on = None
    store.action(job_id, "resume")
    worker.run_job(store, store.claim("test"))
    completed = store.job(job_id)
    assert completed["state"] == "succeeded" and completed["done"] == 12
    assert all(row["attempts"] == 1 for row in store.rows("SELECT attempts FROM job_items WHERE job_id=?", (job_id,)))
