"""Bounded real-HTTP OCR adapter contract and adverse state projections."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import importlib
import socket
import shutil
import sqlite3
import threading
from contextlib import closing
from urllib.parse import urlsplit, parse_qs

import pytest

from adapters.ocr.adapter import OcrAdapter, OcrUnavailable
from workbench_platform.bridge.context import ResolvedContext


PROJECT = "a" * 32
IMAGE = "b" * 32
VERSION = "c" * 32
TASK_A, TASK_B, TASK_C = (letter * 32 for letter in "def")
RESULT = "g" * 32
TOKEN = "s" * 40


class Native(BaseHTTPRequestHandler):
    calls = []
    review_only = False
    tasks = []
    corrupt = False
    revision = 2
    operations = {}
    drop_adoption_reply = False

    def reply(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"message": "unauthorized"})
        if self.path == "/api/health":
            return self.reply(200, {"review_only": self.review_only, "status": "ready",
                                    "queue": {"healthy": True}})
        if self.path == "/api/state":
            return self.reply(200, {"engines": {"ppocr": {"package_id": "installed"}}})
        path = urlsplit(self.path)
        if path.path.startswith(f"/api/results/{RESULT}/"):
            if path.path.endswith("/revisions"):
                params = parse_qs(path.query)
                offset, limit = int(params["offset"][0]), int(params["limit"][0])
                return self.reply(200, {"id": RESULT, "task_id": TASK_A, "project_id": PROJECT,
                                       "image_id": IMAGE, "version_id": VERSION, "current_revision": self.revision,
                                       "total": self.revision+1, "unavailable_revisions": 0,
                                       "offset": offset, "limit": limit,
                                       "items": [{"revision": rev} for rev in range(self.revision+1)][offset:offset+limit]})
            revision = self.revision if path.path.endswith("/snapshot") else int(path.path.rsplit("/", 1)[1])
            return self.reply(200, {"id": RESULT, "task_id": TASK_A, "project_id": "foreign" if self.corrupt else PROJECT,
                                   "image_id": IMAGE, "version_id": VERSION, "original_sha256": "a" * 64,
                                   "revision": revision, "current_revision": self.revision, "available": True,
                                   "edited": {"text": f"correction {revision}", "tables": []}})
        if path.path.startswith("/api/platform/adoptions/"):
            operation_id = path.path.rsplit("/", 1)[1]
            if self.command == "GET":
                receipt = self.operations.get(operation_id)
                return self.reply(200, {"operation_id": operation_id, "committed": receipt is not None,
                                       "receipt": receipt})
            value = json.loads(body)
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
            old = self.operations.get(operation_id)
            if old and old["payload_hash"] != digest:
                return self.reply(409, {"message": "conflict"})
            if not old and value["revision"] != self.revision:
                return self.reply(409, {"message": "stale revision"})
            receipt = old or {**value, "operation_id": operation_id, "payload_hash": digest,
                             "state": "committed", "previous": {"result_id": RESULT, "revision": 0, "version_id": VERSION}}
            self.operations[operation_id] = receipt
            if self.drop_adoption_reply:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.reply(200, receipt)
        if self.path == f"/api/projects/{PROJECT}" and self.command == "GET":
            return self.reply(200, {"project": {"id": "wrong" if self.corrupt else PROJECT},
                                    "revision": 8, "unchanged": False,
                                    "images": [{"id": IMAGE, "project_id": PROJECT, "selected_result": RESULT,
                                                "document_id": "h" * 32, "page_number": 4}],
                                    "versions": [{"id": VERSION, "image_id": IMAGE}],
                                    "tasks": self.tasks, "documents": [{"id": "h" * 32, "project_id": PROJECT}]})
        if self.path == f"/api/projects/{PROJECT}/tasks" and self.command == "POST":
            assert json.loads(body) == {"version_ids": [VERSION], "engines": ["ppocr"],
                                        "preprocess": [], "request_id": "request_123"}
            return self.reply(200, {"task_ids": [TASK_A, TASK_B, TASK_C]})
        if self.path == f"/api/projects/{PROJECT}/queue/cancel" and self.command == "POST":
            assert json.loads(body) == {"task_ids": [TASK_C]}
            return self.reply(200, {"task_ids": [TASK_C]})
        return self.reply(404, {"message": "not found"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def log_message(self, *_args):
        pass


@pytest.fixture
def native():
    Native.calls, Native.review_only, Native.tasks, Native.corrupt = [], False, [], False
    Native.revision, Native.operations, Native.drop_adoption_reply = 2, {}, False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Native)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def context(native, **changes):
    value = dict(workspace_id="workspaceA", app_id="ocr", instance_id="instanceA",
                 link_id="linkA", module_project_id=PROJECT, role="editor",
                 return_route="/apps/ocr", base_url=f"http://127.0.0.1:{native.server_port}/")
    return ResolvedContext(**(value | changes))


def adapter(native, **changes):
    return OcrAdapter(context(native, **changes), token=lambda: TOKEN, live=lambda _context: True)


def task(task_id, status, result_id=None):
    return dict(id=task_id, project_id=PROJECT, image_id=IMAGE, version_id=VERSION,
                status=status, phase=status, result_id=result_id, error=None, kind="ocr")


def test_native_ids_and_partial_work_remain_distinct(native):
    client = adapter(native)
    assert client.capabilities().recognition
    assert client.capabilities().native_extraction
    assert "xlsx" in client.capabilities().export_formats
    assert client.submit([VERSION], ["ppocr"], request_id="request_123") == (TASK_A, TASK_B, TASK_C)
    assert Native.calls[-1][1] == f"/api/projects/{PROJECT}/tasks"
    Native.tasks = [task(TASK_A, "succeeded", RESULT), task(TASK_B, "failed"), task(TASK_C, "queued")]
    batch = client.batch([TASK_A, TASK_B, TASK_C])
    assert (batch.succeeded, batch.failed, batch.cancelled, batch.pending, batch.status) == (1, 1, 0, 1, "running")
    assert batch.unfinished_ids == (TASK_C,)
    assert client.cancel([TASK_A, TASK_B, TASK_C]) == (TASK_C,)
    artifacts = client.results([TASK_A, TASK_B, TASK_C])
    assert len(artifacts) == 1 and artifacts[0].native_result_id == RESULT
    assert artifacts[0].selected
    assert artifacts[0].locator == {"kind": "image", "image_id": IMAGE, "version_id": VERSION,
                                    "document_id": "h" * 32, "page_number": 4}
    assert "cell" not in artifacts[0].locator and "polygon" not in artifacts[0].locator
    Native.tasks[-1]["status"] = "cancelled"
    closed = client.batch([TASK_A, TASK_B, TASK_C])
    assert (closed.status, closed.pending, closed.unfinished_ids) == ("partial", 0, ())


def test_review_only_and_wrong_native_mapping_rejected(native):
    Native.review_only = True
    client = adapter(native)
    assert client.capabilities().review and client.capabilities().export
    assert not client.capabilities().recognition and client.capabilities().reason == "review_only"
    assert not client.capabilities().native_extraction
    with pytest.raises(OcrUnavailable):
        client.submit([VERSION], ["ppocr"], request_id="request_123")
    Native.review_only = False
    before = len(Native.calls)
    with pytest.raises(OcrUnavailable):
        client.submit(["otherVersion"], ["ppocr"], request_id="request_123")
    assert not any(call[0] == "POST" for call in Native.calls[before:])
    Native.corrupt = True
    with pytest.raises(OcrUnavailable):
        client.snapshot()
    Native.corrupt = False
    Native.tasks = [task(TASK_A, "queued")]
    with pytest.raises(OcrUnavailable):
        client.batch(["foreignTask"])
    with pytest.raises(OcrUnavailable):
        adapter(native, role="viewer").cancel([TASK_A])


@pytest.mark.parametrize("target", ["http://example.invalid:8765/", "http://127.0.0.1:8765@evil.invalid/",
                                      "http://127.0.0.1:8765/other", "http://localhost:8765/"])
def test_untrusted_destination_rejected_without_network(native, target):
    with pytest.raises(OcrUnavailable):
        adapter(native, base_url=target)
    assert Native.calls == []


def test_credentials_and_liveness_fail_closed(native):
    with pytest.raises(OcrUnavailable):
        OcrAdapter(context(native), token=lambda: "secret\r\ninjected", live=lambda _context: True).snapshot()
    with pytest.raises(OcrUnavailable):
        OcrAdapter(context(native), token=lambda: TOKEN, live=lambda _context: False).snapshot()
    assert Native.calls == []


def test_invalid_rows_never_fabricate_success(native):
    Native.tasks = [task(TASK_A, "succeeded", None)]
    with pytest.raises(OcrUnavailable):
        adapter(native).batch([TASK_A])
    Native.tasks = [task(TASK_A, "queued", RESULT)]
    with pytest.raises(OcrUnavailable):
        adapter(native).batch([TASK_A])
    Native.tasks = [task(TASK_A, "queued")]
    assert adapter(native).batch([TASK_A]).status == "running"


@pytest.mark.parametrize("module_name", ["adapters.ocr.adapter", "workbench_platform.jobs.native_ocr"])
def test_explicit_revision_adoption_replay_and_old_window_conflict(native, module_name):
    module = importlib.import_module(module_name)
    client = module.OcrAdapter(context(native), token=lambda: TOKEN, live=lambda _: True)
    result = module.ResultArtifact(RESULT, TASK_A, IMAGE, VERSION, True, {"kind": "image"})
    actor = {"id": "localUser", "display_name": "本机编制人", "source": "local"}
    old = client.result_snapshot(result, 1)
    assert (old["revision"], old["current_revision"], old["edited"]["text"]) == (1, 2, "correction 1")
    assert client.result_history(result, 1, 2)["items"] == [{"revision": 1}, {"revision": 2}]
    with pytest.raises(module.OcrResultConflict):
        client.adopt(result, 1, operation_id="operation_old", actor=actor)
    assert Native.operations == {}
    receipt = client.adopt(result, 2, operation_id="operation_new", actor=actor, note="采用 revision 2")
    assert (receipt["revision"], receipt["instance_id"], receipt["project_id"]) == (2, "instanceA", PROJECT)
    Native.revision = 3
    assert client.adopt(result, 2, operation_id="operation_new", actor=actor, note="采用 revision 2") == receipt
    with pytest.raises(module.OcrResultConflict):
        client.adopt(result, 2, operation_id="operation_new", actor=actor, note="different request")
    assert len(Native.operations) == 1
    Native.corrupt = True
    with pytest.raises(module.OcrUnavailable):
        client.result_snapshot(result, 2)
    Native.corrupt = False
    Native.operations["operation_new"]["payload_hash"] = "b" * 64
    with pytest.raises(module.OcrUnavailable):
        client.operation("operation_new")


@pytest.mark.parametrize("module_name", ["adapters.ocr.adapter", "workbench_platform.jobs.native_ocr"])
def test_lost_native_adoption_response_recovers_one_operation_without_resubmission(native, module_name):
    module = importlib.import_module(module_name)
    client = module.OcrAdapter(context(native), token=lambda: TOKEN, live=lambda _: True)
    result = module.ResultArtifact(RESULT, TASK_A, IMAGE, VERSION, True, {"kind": "image"})
    actor = {"id": "localUser", "display_name": "本机复核人", "source": "local"}
    Native.drop_adoption_reply = True
    with pytest.raises(module.OcrUnavailable):
        client.adopt(result, 2, operation_id="operation_lost", actor=actor)
    assert len(Native.operations) == 1
    committed = client.operation("operation_lost")
    assert committed["revision"] == 2 and committed["state"] == "committed"
    writes = [call for call in Native.calls if call[0] == "POST"]
    assert len(writes) == 1
    assert client.adopt(result, 2, operation_id="operation_lost", actor=actor) == committed
    assert len([call for call in Native.calls if call[0] == "POST"]) == 1
    viewer = module.OcrAdapter(context(native, role="viewer"), token=lambda: TOKEN, live=lambda _: True)
    with pytest.raises(module.OcrUnavailable):
        viewer.adopt(result, 2, operation_id="operation_viewer", actor=actor)


@pytest.mark.parametrize("module_name", ["adapters.ocr.adapter", "workbench_platform.jobs.native_ocr"])
def test_real_native_lost_reply_then_cold_restore_queries_the_original_instance(tmp_path, monkeypatch, module_name):
    # Optional native source is supplied explicitly by the controlled integration
    # runner. The native service remains an in-process CPU fixture, with real
    # SQLite storage, real HTTP adapter requests and no OCR worker/model launch.
    native_store = pytest.importorskip("ocr_workbench.store")
    native_service = pytest.importorskip("ocr_workbench.service")
    native_imaging = pytest.importorskip("ocr_workbench.imaging")
    from PIL import Image
    from fastapi.testclient import TestClient
    module = importlib.import_module(module_name)
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config/engines.json").write_text('{"ppocr":{"name":"Synthetic","models":[]}}', "utf-8")
    source_root = tmp_path / "nativeA"
    store = native_store.Store(source_root)
    project = store.project("cold restore")
    source_image = tmp_path / "source.png"
    Image.new("RGB", (3, 3), "white").save(source_image)
    image = native_imaging.add_image(store, project["id"], "source.png", source_image)
    task_id = store.enqueue(project["id"], [image["active_version"]], ["ppocr"])[0]
    store.claim()
    store.complete(task_id, {"text": "original", "tables": [], "blocks": [], "engine": "ppocr"})
    result_id = store.one("tasks", task_id)["result_id"]
    store.save(result_id, {"text": "revision 1", "tables": []}, 0)
    store.save(result_id, {"text": "revision 2", "tables": []}, 1)
    result = module.ResultArtifact(result_id, task_id, image["id"], image["active_version"], True, {"kind": "image"})
    actor = {"id": "localUser", "display_name": "本机复核人", "source": "local"}
    state = {"drop": True, "posts": 0, "instance": "instanceA", "requests": []}

    def service_client(root, instance):
        monkeypatch.setenv("WORKBENCH_APP_ID", "ocr")
        monkeypatch.setenv("WORKBENCH_INSTANCE_ID", instance)
        monkeypatch.setenv("WORKBENCH_NONCE", instance[0] * 32)
        app = native_service.create_app(bundle, root, TOKEN, start_queue=False, review_only=True)
        return TestClient(app), app.state.store

    class ForwardNative(BaseHTTPRequestHandler):
        def dispatch(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["requests"].append((self.command, self.path))
            if self.command == "POST":
                state["posts"] += 1
            response = state["client"].request(self.command, self.path, content=content,
                                               headers={"Authorization": self.headers.get("Authorization", ""),
                                                        "Content-Type": "application/json"})
            if state["drop"] and self.command == "POST" and response.status_code == 200:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def log_message(self, *_args):
            pass

    def http_server():
        server = ThreadingHTTPServer(("127.0.0.1", 0), ForwardNative)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def bridged(server, instance, project_id=None):
        bound = ResolvedContext("workspaceA", "ocr", instance, "linkA", project_id or project["id"],
                                "owner", "/apps/ocr", f"http://127.0.0.1:{server.server_port}/")
        return module.OcrAdapter(bound, token=lambda: TOKEN,
                                 live=lambda current: current.instance_id == state["instance"])

    state["client"], source_store = service_client(source_root, "instanceA")
    server, thread = http_server()
    try:
        client_a = bridged(server, "instanceA")
        with pytest.raises(module.OcrUnavailable):
            client_a.adopt(result, 2, operation_id="operation_restored", actor=actor)
        original_receipt = source_store.adoption_operation("operation_restored", project["id"], "instanceA")
        assert original_receipt is not None and state["posts"] == 1
    finally:
        stop_server(server, thread)
        state["client"].close()

    # The fixture has no running writers now. SQLite backup produces a coherent
    # DB; immutable source files are copied into a fresh directory with it.
    backup = tmp_path / "cold-backup"
    backup.mkdir()
    with closing(sqlite3.connect(source_root / "workbench.sqlite3")) as source_db:
        with closing(sqlite3.connect(backup / "workbench.sqlite3")) as backup_db:
            source_db.backup(backup_db)
    shutil.copytree(source_root / "projects", backup / "projects")
    restored_root = tmp_path / "fresh nativeB restored"
    shutil.copytree(backup, restored_root)
    state.update(drop=False, instance="instanceB")
    state["client"], restored_store = service_client(restored_root, "instanceB")
    server, thread = http_server()
    try:
        client_b = bridged(server, "instanceB")
        with pytest.raises(module.OcrResultConflict):
            client_b.operation("operation_restored")
        receipt = client_b.operation("operation_restored", expected_instance_id="instanceA")
        assert receipt == original_receipt and receipt["instance_id"] == "instanceA"
        assert receipt["revision"] == 2
        assert any("instance_id=instanceB&operation_instance_id=instanceA" in path for _, path in state["requests"])
        with pytest.raises(module.OcrResultConflict):
            client_b.operation("operation_restored", expected_instance_id="wrongOrigin")
        with pytest.raises(module.OcrResultConflict):
            bridged(server, "instanceB", "wrongProject").operation("operation_restored", expected_instance_id="instanceA")
        with pytest.raises(module.OcrUnavailable):
            bridged(server, "instanceA").operation("operation_restored", expected_instance_id="instanceA")
        with pytest.raises(module.OcrResultConflict):
            client_b.adopt(result, 2, operation_id="operation_restored", actor=actor)
        assert state["posts"] == 1
        assert len(restored_store.rows("SELECT * FROM platform_adoptions")) == 1
        assert restored_store.result_snapshot(result_id, 2)["edited"]["text"] == "revision 2"
        with pytest.raises(sqlite3.IntegrityError):
            with restored_store.transaction() as db:
                db.execute("UPDATE platform_adoptions SET instance_id='instanceB' WHERE operation_id='operation_restored'")
    finally:
        stop_server(server, thread)
        state["client"].close()
