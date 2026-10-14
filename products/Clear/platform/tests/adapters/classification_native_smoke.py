"""Optional real classification HTTP smoke in the isolated native runtime."""

import json
from pathlib import Path
import tempfile
import threading
import time
import urllib.request

from PIL import Image
import uvicorn

from adapters.classification.adapter import ClassificationAdapter, ClassificationUnavailable
from image_platform.api import create_app
from image_platform.datasets import register_asset
from image_platform.settings import Settings
from image_platform.store import dump
from workbench_platform.bridge.context import ResolvedContext


def main():
    with tempfile.TemporaryDirectory(prefix="classification-adapter-") as temporary:
        app = create_app(Settings(Path(temporary) / "data"))
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
            payload = json.dumps({"name": "native adapter smoke", "labels": [
                {"label_id": "a", "display_name": "A"},
                {"label_id": "b", "display_name": "B"}]}).encode()
            request = urllib.request.Request(origin + "/api/v1/projects", payload,
                                             {"X-Platform-Request": "1", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                project_id = json.load(response)["id"]
            context = ResolvedContext("workspaceA", "classification", "instanceA", "linkA", project_id,
                                      "editor", "/apps/classification", origin + "/")
            client = ClassificationAdapter(context, live=lambda _context: True)
            image_path = Path(temporary) / "source.png"
            Image.new("RGB", (32, 32), "blue").save(image_path)
            asset, created = register_asset(app.state.store, project_id, image_path)
            assert created and client.assets()[0]["id"] == asset["id"]
            assert client.annotate([asset["id"]], "b") == 1
            job_id = app.state.store.submit(project_id, "inference", {"model_id": "fixture"}, [asset])
            assert client.job(job_id).state == "queued"
            with app.state.store.connect(write=True) as con:
                con.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?",
                            (dump({"label_id": "a", "status": "needs_review", "score": 0.8}), job_id))
            item = client.results(job_id)[0]
            assert item.prediction_label_id == "a" and item.human_label_id == "b"
            assert client.action(job_id, "cancel").state == "cancelled"
            print(json.dumps({"native_http": True, "project_id": project_id, "native_job_id": job_id,
                              "prediction_separate_from_human": True, "cancelled": True}))
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive(), "Owned native service did not stop"


if __name__ == "__main__":
    main()
