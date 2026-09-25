"""Bounded real-HTTP OCR adapter contract and adverse state projections."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

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
