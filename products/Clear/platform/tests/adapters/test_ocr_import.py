"""Real HTTP and process-restart proof for the OCR import handoff."""

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from adapters.ocr.adapter import OcrAdapter, OcrImportConflict, OcrUnavailable
from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.supervisor.processes import (
    process_creation_time, require_f_path, storage_environment,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "lXcAAAAASUVORK5CYII="
)
TOKEN = "i" * 40
SERVER = r"""
import hashlib
import json
import os
import sys
from pathlib import Path
bundle, platform, data = (Path(value).resolve() for value in sys.argv[1:4])
sys.path[:0] = [str(bundle / 'src'), str(platform / 'src'), str(platform)]
from workbench_platform.supervisor.processes import require_f_path, storage_environment
require_f_path(bundle, 'OCR fixture source')
runtime_environment = storage_environment(data)
os.environ.update(runtime_environment)
import uvicorn
import ocr_workbench.service as service
import ocr_workbench.store as store
from ocr_workbench.service import create_app
identity = {'pid': os.getpid(), 'executable': sys.executable,
            'sources': {name: {'path': str(Path(module.__file__).resolve()),
                             'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
                        for name, module in [('service', service), ('store', store)]},
            'environment': runtime_environment,
            'start_queue': False, 'review_only': True, 'agent_enabled': False}
(data.parent / f'native-identity-{os.getpid()}.json').write_text(
    json.dumps(identity, ensure_ascii=False, indent=2), encoding='utf-8')
app = create_app(bundle, data, sys.argv[4], start_queue=False,
                 review_only=True, agent_enabled=False)
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[5]),
            log_level='warning', access_log=False)
"""


def _http(port, method, path, payload=None, *, content_type="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode("utf-8")
        connection.request(method, path, body=payload,
                           headers={"Authorization": "Bearer " + TOKEN,
                                    "Content-Type": content_type})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _record(log, process, event):
    with log.with_name('native-processes.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps({'event': event, 'pid': process.pid,
            'creation_time': process_creation_time(process.pid),
            'returncode': process.poll()}, sort_keys=True) + '\n')


def _start(python, bundle, platform, data, port, log):
    for path in (python, bundle, platform, data, log):
        require_f_path(path, 'OCR fixture path')
    env = os.environ.copy()
    env.update(storage_environment(data))
    output = log.open("ab")
    try:
        process = subprocess.Popen([str(python), "-I", "-B", "-u", "-c", SERVER,
                                    str(bundle), str(platform), str(data), TOKEN, str(port)],
                                   cwd=data.parent, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    finally:
        output.close()
    try:
        _record(log, process, 'started')
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"OCR service exited: {log.read_text(errors='replace')}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                    identity = json.loads((data.parent / f'native-identity-{process.pid}.json').read_text('utf-8'))
                    assert Path(identity['executable']).resolve() == python.resolve()
                    for name in ('service', 'store'):
                        source = bundle / 'src' / 'ocr_workbench' / f'{name}.py'
                        assert Path(identity['sources'][name]['path']) == source.resolve()
                        assert identity['sources'][name]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
                    for key, value in identity['environment'].items():
                        if key not in {'PYTHONDONTWRITEBYTECODE', 'PYTHONUTF8',
                                       'PIP_DISABLE_PIP_VERSION_CHECK', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'}:
                            require_f_path(Path(value), key)
                    return process
            except (OSError, TimeoutError):
                time.sleep(0.1)
        raise AssertionError(f"OCR service did not start: {log.read_text(errors='replace')}")
    except BaseException:
        _stop(process, log)
        raise


def _stop(process, log):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    assert process.poll() is not None
    _record(log, process, 'exited')


def test_durable_idempotent_import_over_native_http(tmp_path):
    platform = Path(__file__).resolve().parents[2]
    bundle = Path(os.environ.get('PLATFORM_OCR_TEST_SOURCE', platform.parent / 'ocr')).resolve()
    python = Path(os.environ.get('PLATFORM_OCR_TEST_PYTHON', sys.executable)).resolve()
    assert python.is_file() and (bundle / "src" / "ocr_workbench" / "service.py").is_file()
    data, log = tmp_path / "ocr-data", tmp_path / "native.log"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = _start(python, bundle, platform, data, port, log)
    try:
        code, project = _http(port, "POST", "/api/projects", {"name": "handoff"})
        assert code == 200
        project_id = project["id"]
        context = ResolvedContext("workspaceA", "ocr", "instanceA", "linkA", project_id,
                                  "editor", "/apps/ocr", f"http://127.0.0.1:{port}/")
        adapter = OcrAdapter(context, token=lambda: TOKEN, live=lambda _context: True, timeout=8)
        image = tmp_path / "image.png"
        image.write_bytes(PNG)
        blob_hash = hashlib.sha256(PNG).hexdigest()
        items = [{"item_id": f"item{i}", "asset_id": f"asset{i}",
                  "blob_hash": blob_hash, "size_bytes": len(PNG), "name": f"source{i}.png"}
                 for i in (1, 2)]
        manifest = {"schema_version": "1.0", "asset_set_id": "setA", "asset_set_revision": 1,
                    "manifest_hash": "a" * 64, "import_mode": "copy", "items": items}
        request_id = "request_0001"
        first = adapter.begin_import(request_id, manifest)
        assert first["state"] == "prepared" and first == adapter.begin_import(request_id, manifest)
        unauthorized = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            unauthorized.request("GET", f"/api/platform/imports/{project_id}/{request_id}")
            assert unauthorized.getresponse().status == 401
        finally:
            unauthorized.close()
        changed = {**manifest, "manifest_hash": "b" * 64}
        try:
            adapter.begin_import(request_id, changed)
        except OcrImportConflict:
            pass
        else:
            raise AssertionError("Changed manifest reused an existing request ID")
        code, _ = _http(port, "PUT", f"/api/platform/imports/{project_id}/{request_id}/items/item1",
                        b"bad", content_type="application/octet-stream")
        assert code == 400

        # Send item 1, deliberately discard its reply, then kill the accepting
        # process. The platform only learns its durable outcome after restart.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        connection.request("PUT", f"/api/platform/imports/{project_id}/{request_id}/items/item1",
                           body=PNG, headers={"Authorization": "Bearer " + TOKEN,
                                              "Content-Type": "application/octet-stream"})
        connection.getresponse().close()
        connection.close()
        _stop(process, log)
        process = _start(python, bundle, platform, data, port, log)
        status = adapter.import_status(request_id)
        assert status["state"] == "accepted"
        first_image = status["items"][0]["target_asset_id"]
        assert first_image
        assert status["items"][1]["status"] == "pending"
        replay = adapter.import_item(request_id, "item1", image,
                                     blob_hash=blob_hash, size_bytes=len(PNG))
        assert replay["items"][0]["target_asset_id"] == first_image
        completed = adapter.import_item(request_id, "item2", image,
                                        blob_hash=blob_hash, size_bytes=len(PNG))
        assert completed["state"] == "completed"
        assert completed == adapter.begin_import(request_id, manifest)
        assert len(adapter.snapshot()["images"]) == 2
        viewer = OcrAdapter(ResolvedContext("workspaceA", "ocr", "instanceA", "linkA",
                                            project_id, "viewer", "/apps/ocr",
                                            f"http://127.0.0.1:{port}/"),
                            token=lambda: TOKEN, live=lambda _context: True)
        assert viewer.import_status(request_id)["state"] == "completed"
        try:
            viewer.begin_import("request_0002", manifest)
        except OcrUnavailable:
            pass
        else:
            raise AssertionError("Viewer started a native import")
    finally:
        _stop(process, log)
