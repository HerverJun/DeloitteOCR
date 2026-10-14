"""Real loopback protocol checks for classification ownership and semantics."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import threading

import pytest

from adapters.classification.adapter import ClassificationAdapter, ClassificationUnavailable
from workbench_platform.jobs.native_classification import ClassificationAdapter as PackagedAdapter
from workbench_platform.jobs.native_classification import ClassificationUnavailable as PackagedUnavailable
from workbench_platform.bridge.context import ResolvedContext

PROJECT = "project_" + "a" * 16
ASSET = "asset_" + "b" * 16
DATASET = "dataset_" + "c" * 16
MODEL = "model_" + "d" * 16
TRAIN = "job_" + "e" * 16
INFER = "job_" + "f" * 16
RETRY = "job_" + "a" * 16


class Native(BaseHTTPRequestHandler):
    calls = []
    job_kind = "inference"
    job_state = "running"
    bad_project = False
    human_label = None
    disposition_revision = 0
    operations = {}
    history = []

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append((self.command, self.path, dict(self.headers), raw))
        if self.command == "POST" and self.headers.get("X-Platform-Request") != "1":
            return self.reply(403, {"detail": "missing header"})
        prefix = f"/api/v1/projects/{PROJECT}"
        operation_prefix = prefix + "/disposition-operations/"
        if self.path.startswith(operation_prefix):
            operation = self.path.removeprefix(operation_prefix).split("?")[0]
            return self.reply(200, self.operations.get(operation) or {"operation_id": operation, "state": "not_found"})
        if self.path == f"/api/v1/jobs/{INFER}/results/item_" + "a" * 16 + "/dispositions":
            return self.reply(200, {"items": self.history})
        if self.path == f"/api/v1/jobs/{INFER}/dispositions" and self.command == "POST":
            body = json.loads(raw)
            prior = self.operations.get(body["operation_id"])
            identity = {"job_id": INFER, **{key: value for key, value in body.items()
                                           if key not in {"operation_id", "request_id"}}}
            identity["item_ids"] = sorted(identity["item_ids"])
            fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if prior:
                return self.reply(200 if prior["payload_hash"] == fingerprint else 409, prior)
            item_id = "item_" + "a" * 16
            if body["expected_revisions"] != {item_id: self.disposition_revision}:
                return self.reply(409, {"detail": "stale revision"})
            assert body["instance_id"] == "instanceA" and body["actor"]["source"] == "local"
            Native.disposition_revision += 1
            Native.human_label = body["label_id"]
            receipt = {"operation_id": body["operation_id"], "request_id": body["request_id"],
                       "payload_hash": fingerprint, "state": "committed", "project_id": PROJECT,
                       "instance_id": "instanceA", "job_id": INFER, "human_label_id": body["label_id"],
                       "disposition_state": body["state"], "revisions": {item_id: self.disposition_revision},
                       "items": [{"item_id": item_id, "asset_id": ASSET, "original_sha256": "a" * 64,
                                  "revision": self.disposition_revision}], "actor": body["actor"], "note": body["note"]}
            Native.operations[body["operation_id"]] = receipt
            Native.history.append({"job_id": INFER, "item_id": item_id, "revision": self.disposition_revision,
                                   "operation_id": body["operation_id"], "actor": body["actor"]})
            return self.reply(200, receipt)
        if self.path == prefix:
            return self.reply(200, {"id": "other" if self.bad_project else PROJECT,
                                    "labels": [{"label_id": "a"}, {"label_id": "b"}],
                                    "active_model_id": MODEL})
        if self.path == prefix + "/assets?limit=48&offset=0":
            return self.reply(200, {"items": [{"id": ASSET, "project_id": PROJECT, "label_id": None}], "total": 1})
        if self.path == prefix + f"/assets/{ASSET}":
            return self.reply(200, {"id": ASSET, "project_id": PROJECT, "label_id": None})
        if self.path == prefix + "/annotations" and self.command == "POST":
            assert json.loads(raw) == {"asset_ids": [ASSET], "label_id": "b", "group_id": None}
            return self.reply(200, {"updated": 1})
        if self.path == prefix + "/dataset-versions":
            return self.reply(200, [{"id": DATASET, "stats": {"total": 20}}])
        if self.path == prefix + "/models":
            return self.reply(200, [{"id": MODEL, "project_id": PROJECT, "metadata": {}, "metrics": {}}])
        if self.path == prefix + "/experiments" and self.command == "POST":
            assert self.headers.get("Idempotency-Key") == "stable_key"
            assert json.loads(raw) == {"dataset_id": DATASET, "epochs": 1, "device": "cpu"}
            return self.reply(202, {"job_id": TRAIN})
        if self.path == prefix + "/batch-jobs" and self.command == "POST":
            assert self.headers.get("Idempotency-Key") == "stable_key"
            assert json.loads(raw) == {"model_id": MODEL, "asset_ids": [ASSET],
                                       "device": "cpu", "auto_accept": False}
            return self.reply(202, {"job_id": INFER})
        if self.path == f"/api/v1/jobs/{INFER}/results?limit=48&offset=0":
            return self.reply(200, {"items": [{"id": "item_" + "a" * 16, "job_id": INFER,
                                               "asset_id": ASSET, "state": "succeeded",
                                               "prediction": {"label_id": "a", "status": "needs_review", "score": 0.8},
                                               "confirmed_label": "b", "error": None,
                                               "human_label_id": self.human_label,
                                               "disposition_state": "confirmed" if self.human_label else "unreviewed",
                                               "disposition_revision": self.disposition_revision}], "total": 1})
        if self.path in {f"/api/v1/jobs/{TRAIN}", f"/api/v1/jobs/{INFER}"} and self.command == "GET":
            job_id = self.path.rsplit("/", 1)[1]
            return self.reply(200, {"id": job_id, "project_id": PROJECT,
                                    "kind": "train" if job_id == TRAIN else self.job_kind,
                                    "state": self.job_state, "stage": "working", "total": 2, "done": 1,
                                    "failed": 0, "result": None})
        if self.path == f"/api/v1/jobs/{INFER}/actions" and self.command == "POST":
            action = json.loads(raw)["action"]
            if action == "retry_failed":
                return self.reply(200, {"job_id": RETRY})
            Native.job_state = "pausing" if action == "pause" else "queued" if action == "resume" else "cancelling"
            return self.reply(200, {"id": INFER})
        return self.reply(404, {"detail": "not found"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def log_message(self, *_args):
        pass


@pytest.fixture
def native():
    Native.calls, Native.job_kind, Native.job_state, Native.bad_project = [], "inference", "running", False
    Native.human_label, Native.disposition_revision, Native.operations, Native.history = None, 0, {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Native)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def context(native, **overrides):
    value = dict(workspace_id="workspaceA", app_id="classification", instance_id="instanceA",
                 link_id="linkA", module_project_id=PROJECT, role="editor",
                 return_route="/apps/classification", base_url=f"http://127.0.0.1:{native.server_port}/")
    return ResolvedContext(**(value | overrides))


def adapter(native, **overrides):
    return ClassificationAdapter(context(native, **overrides), live=lambda _context: True)


def test_project_annotation_training_inference_and_label_separation(native):
    client = adapter(native)
    assert client.project()["id"] == PROJECT
    assert client.assets()[0]["id"] == ASSET
    assert client.annotate([ASSET], "b") == 1
    assert client.dataset_versions()[0]["id"] == DATASET
    assert client.train(DATASET, key="stable_key") == TRAIN
    assert client.infer(MODEL, [ASSET], key="stable_key") == INFER
    result = client.results(INFER)[0]
    assert result.prediction_label_id == "a" and result.human_label_id is None
    assert result.current_annotation_label_id == "b" and result.disposition_state == "unreviewed"
    assert result.prediction_status == "needs_review" and result.prediction_score == 0.8
    assert Native.calls[-1][1].endswith("/results?limit=48&offset=0")


def test_control_matrix_never_promises_training_resume(native):
    client = adapter(native)
    train = client.job(TRAIN)
    assert train.actions == ("cancel",) and not train.interrupted_training_requires_new_job
    Native.job_state = "paused"
    assert client.job(TRAIN).actions == ("cancel",)
    assert client.job(TRAIN).interrupted_training_requires_new_job
    with pytest.raises(ClassificationUnavailable):
        client.action(TRAIN, "resume")
    Native.job_state = "failed"
    train = client.job(TRAIN)
    assert train.actions == () and train.interrupted_training_requires_new_job
    with pytest.raises(ClassificationUnavailable):
        client.action(TRAIN, "retry_failed")
    Native.job_state = "running"
    assert client.job(INFER).actions == ("pause", "cancel")
    assert client.action(INFER, "pause").state == "pausing"
    Native.job_state = "paused"  # Worker commits its batch boundary asynchronously.
    assert client.job(INFER).actions == ("resume", "cancel")
    assert client.action(INFER, "resume").state == "queued"
    Native.job_state = "completed_with_errors"
    assert client.action(INFER, "retry_failed") == RETRY
    assert RETRY != INFER


def test_wrong_project_asset_label_role_and_model_fail_closed(native):
    client = adapter(native)
    Native.bad_project = True
    with pytest.raises(ClassificationUnavailable):
        client.project()
    Native.bad_project = False
    before = len(Native.calls)
    with pytest.raises(ClassificationUnavailable):
        client.annotate([ASSET], "unknown")
    assert not any(call[0] == "POST" for call in Native.calls[before:])
    with pytest.raises(ClassificationUnavailable):
        client.train("foreignDataset", key="stable_key")
    with pytest.raises(ClassificationUnavailable):
        client.infer("foreignModel", [ASSET], key="stable_key")
    with pytest.raises(ClassificationUnavailable):
        adapter(native, role="viewer").annotate([ASSET], "b")
    with pytest.raises(ClassificationUnavailable):
        adapter(native, role="viewer").action(INFER, "cancel")


@pytest.mark.parametrize("url", ["http://example.invalid:8765/", "http://127.0.0.1:8765@evil.invalid/",
                                  "http://localhost:8765/", "http://127.0.0.1:8765/else"])
def test_untrusted_destination_rejected(native, url):
    with pytest.raises(ClassificationUnavailable):
        adapter(native, base_url=url)
    assert Native.calls == []


def test_liveness_and_cross_project_job_rejected(native):
    with pytest.raises(ClassificationUnavailable):
        ClassificationAdapter(context(native), live=lambda _context: False).project()
    assert Native.calls == []
    with pytest.raises(ClassificationUnavailable):
        adapter(native).job("job_other")


@pytest.mark.parametrize("adapter_type", [ClassificationAdapter, PackagedAdapter])
def test_adopts_b_with_original_expected_revision_and_reconciles_one_operation(native, adapter_type):
    client = adapter_type(context(native), live=lambda _: True)
    original = client.results(INFER)[0]
    Native.human_label, Native.disposition_revision = "b", 1
    manual = client.results(INFER)[0]
    assert manual.prediction_label_id == "a" and manual.effective_label_id == "b"
    actor = {"id": "localReviewer", "display_name": "本机复核员", "source": "local"}
    receipt = client.adopt(INFER, manual, 1, "b", operation_id="adoptB", actor=actor, note="核对原图")
    assert receipt["revisions"][manual.native_item_id] == 2
    assert client.operation("adoptB") == receipt
    assert client.adopt(INFER, manual, 1, "b", operation_id="adoptB", actor=actor, note="核对原图") == receipt
    assert Native.disposition_revision == 2 and len(Native.history) == 1
    assert client.disposition_history(INFER, manual.native_item_id)[0]["actor"] == actor
    with pytest.raises((ClassificationUnavailable, PackagedUnavailable)):
        client.adopt(INFER, manual, 0, "b", operation_id="staleAdoption", actor=actor)
    with pytest.raises((ClassificationUnavailable, PackagedUnavailable)):
        client.dispose(INFER, [original], "confirmed", "a", operation_id="oldPrediction", actor=actor)
    posts = [json.loads(call[3]) for call in Native.calls if call[0] == "POST"]
    assert posts[-1]["expected_revisions"] == {original.native_item_id: 0}
    assert Native.human_label == "b"
    assert client.operation("unknownOperation") is None
    new_client = adapter_type(context(native, instance_id="instanceB"), live=lambda _: True)
    post_count = len([call for call in Native.calls if call[0] == "POST"])
    assert new_client.operation("adoptB", expected_instance_id="instanceA") == receipt
    assert len([call for call in Native.calls if call[0] == "POST"]) == post_count
    with pytest.raises((ClassificationUnavailable, PackagedUnavailable)):
        new_client.operation("adoptB")
    Native.operations["adoptB"]["instance_id"] = "foreignInstance"
    with pytest.raises((ClassificationUnavailable, PackagedUnavailable)):
        client.operation("adoptB")
