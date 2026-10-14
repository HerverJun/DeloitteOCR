"""Optional smoke against the real OCR ASGI service, run with OCR's isolated venv.

This is not a substitute for the formal platform adapter check or a packaged
launch. It exercises the native HTTP response shape without a mocked route.
"""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.request

from PIL import Image
import uvicorn

from adapters.ocr.adapter import OcrAdapter, OcrUnavailable
from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from workbench_platform.bridge.context import ResolvedContext


def main():
    bundle = Path(sys.argv[1]).resolve()
    token = "s" * 40
    with tempfile.TemporaryDirectory(prefix="ocr-adapter-") as temporary:
        app = create_app(bundle, Path(temporary), token, start_queue=False, review_only=True,
                         agent_enabled=False)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                               log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started and server.servers
            port = server.servers[0].sockets[0].getsockname()[1]
            origin = f"http://127.0.0.1:{port}"

            def request(path, payload):
                encoded = json.dumps(payload).encode()
                req = urllib.request.Request(origin + path, encoded,
                                             {"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            project = request("/api/projects", {"name": "adapter-native-smoke"})
            project_id = project["id"]
            context = ResolvedContext("workspaceA", "ocr", "instanceA", "linkA", project_id,
                                      "editor", "/apps/ocr", origin + "/")
            adapter = OcrAdapter(context, token=lambda: token, live=lambda _context: True)
            assert adapter.capabilities().review and not adapter.capabilities().recognition
            snapshot = adapter.snapshot()
            assert snapshot["project"]["id"] == project_id and snapshot["tasks"] == []
            image_path = Path(temporary) / "source.png"
            Image.new("RGB", (32, 32), "white").save(image_path)
            image = add_image(app.state.store, project_id, "source.png", image_path)
            native_ids = app.state.store.enqueue(project_id, [image["active_version"]], ["ppocr"])
            assert len(native_ids) == 1
            queued = adapter.batch(native_ids)
            assert queued.status == "running" and queued.unfinished_ids == tuple(native_ids)
            assert adapter.results(native_ids) == ()
            assert adapter.cancel(native_ids) == tuple(native_ids)
            cancelled = adapter.batch(native_ids)
            assert cancelled.status == "cancelled" and cancelled.unfinished_ids == ()
            try:
                adapter.submit(["unknown"], ["ppocr"], request_id="request_123")
            except OcrUnavailable:
                pass
            else:
                raise AssertionError("Review-only service accepted recognition")
            print(json.dumps({"native_http": True, "project_id": project_id,
                              "native_task_ids": native_ids, "cancelled": True,
                              "review_only": True, "snapshot_revision": snapshot["revision"]}))
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "Owned native service did not stop"


if __name__ == "__main__":
    main()
