"""Real local platform HTTP, owned child process and persistent recent ledger."""

import hashlib
import http.client
import json
import shutil
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from workbench_platform.app import app
from workbench_platform.catalog import routes as catalog_routes
from workbench_platform.catalog.registry import AppRegistry, DEFAULT_CONFIG


SERVER = '''
import http.server, json, os, pathlib, sys, time
root = pathlib.Path.cwd()
with (root / 'starts.txt').open('a') as output: output.write(str(os.getpid()) + '\\n')
mode = (root / 'mode.txt').read_text().strip()
if mode == 'exit': sys.exit(7)
if mode == 'silent': time.sleep(30); sys.exit(0)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/api/platform/v1/identity':
            self.send_error(404); return
        body = json.dumps({'app_id': os.environ['WORKBENCH_APP_ID'],
                           'instance_id': os.environ['WORKBENCH_INSTANCE_ID'],
                           'nonce': os.environ['WORKBENCH_NONCE'], 'version': 'fixture-1'}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print('READY ' + str(server.server_address[1]), flush=True)
server.serve_forever()
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    config = tmp_path / "config" / "apps"
    config.mkdir(parents=True)
    for item in DEFAULT_CONFIG.glob("*.json"):
        shutil.copy2(item, config / item.name)
    root = tmp_path / "bundles" / "ocr"
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(sys.executable, scripts / "python.exe")
    shutil.copy2(Path(sys.executable).parent.parent / "pyvenv.cfg", root / "pyvenv.cfg")
    (root / "server.py").write_text(SERVER, encoding="utf-8")
    (root / "mode.txt").write_text("normal", encoding="utf-8")
    names = ["Scripts/python.exe", "pyvenv.cfg", "server.py", "mode.txt"]
    def install():
        (root / "bundle.manifest.json").write_text(json.dumps({
            "app_id": "ocr", "files": [{"path": name, "sha256": digest(root / name)} for name in names]
        }), encoding="utf-8")
        (config / "ocr.install.json").write_text(json.dumps({
            "app_id": "ocr", "installed": True, "manifest_sha256": digest(config / "ocr.json"),
            "launch": {"bundle_root": str(root), "executable": str(scripts / "python.exe"),
                       "working_directory": str(root), "allowed_arguments": ["-u", "server.py"],
                       "executable_sha256": digest(scripts / "python.exe"),
                       "bundle_manifest_sha256": digest(root / "bundle.manifest.json")}
        }), encoding="utf-8")
        return AppRegistry(config, install_root=tmp_path / "bundles")
    monkeypatch.setattr(catalog_routes, "_registry", install())
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    return tmp_path, root, install


@contextmanager
def live_http():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    count = len(app.routes)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=6)
        listener.close()
        assert not thread.is_alive() and len(app.routes) == count


def call(port, method, path, *, host=None, origin=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request(method, path, body=body, headers=headers)
        result = connection.getresponse()
        raw = result.read()
        return result.status, json.loads(raw) if raw else None
    finally:
        connection.close()


def start(port, launch_id="launchOne", **kwargs):
    return call(port, "POST", f"/api/platform/v1/apps/ocr/launches/{launch_id}",
                origin=f"http://127.0.0.1:{port}", **kwargs)


def test_idempotent_dynamic_launch_lost_response_and_current_route(configured):
    _, root, _ = configured
    with live_http() as port:
        with ThreadPoolExecutor(max_workers=5) as pool:
            replies = list(pool.map(lambda _: start(port), range(5)))
        assert sorted(status for status, _ in replies) == [200, 200, 200, 200, 201]
        receipts = [body for _, body in replies]
        assert all(body == receipts[0] for body in receipts)
        receipt = receipts[0]
        assert receipt["launch_id"] == "launchOne"
        assert receipt["base_url"].startswith("http://127.0.0.1:") and ":8765/" not in receipt["base_url"]
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current") == (200, receipt)
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/launchOne") == (200, receipt)
        assert start(port, "launchOther")[0] == 409
        assert len((root / "starts.txt").read_text().splitlines()) == 1
        recent = call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]
        assert len(recent["items"]) == 1
        assert recent["items"][0]["source"] == "platform_launch"
        assert recent["items"][0]["launch_id"] == receipt["launch_id"]
        assert recent["items"][0]["workspace_id"] is None
        assert call(port, "GET", "/api/platform/v1/recent-work?limit=200&workspace_id=unknownWorkspace")[0] == 404


def test_historical_receipt_is_not_a_live_address_and_new_id_required(configured):
    _, root, _ = configured
    with live_http() as port:
        first = start(port)[1]
        app.state.supervisor.stop("launchOne")
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/launchOne")[0] == 410
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 404
        assert start(port)[0] == 410
        second = start(port, "launchTwo")[1]
        assert second["launch_id"] == "launchTwo" and second["instance_id"] != first["instance_id"]
        assert len((root / "starts.txt").read_text().splitlines()) == 2


def test_missing_bundle_failed_start_and_same_origin_boundary(configured):
    _, root, install = configured
    with live_http() as port:
        installed = call(port, "GET", "/api/platform/v1/apps/classification/installation")
        assert installed == (200, {"schema_version": "1.0", "app_id": "classification",
                                   "installed": False, "launch_ready": False, "state": "not_registered"})
        assert call(port, "POST", "/api/platform/v1/apps/classification/launches/classOne",
                    origin=f"http://127.0.0.1:{port}")[0] == 503
        assert call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]["items"] == []
        assert call(port, "POST", "/api/platform/v1/apps/ocr/launches/launchOne")[0] == 403
        assert start(port, host="rebind.test")[0] == 403
        assert call(port, "POST", "/api/platform/v1/apps/ocr/launches/launchOne",
                    origin=f"http://127.0.0.1:{port + 1}")[0] == 403
        assert start(port, body=b"unexpected")[0] == 400
        assert start(port)[0] == 201
        assert not (root / "starts.txt").read_text().strip() == ""


def test_recent_snapshot_persistence_and_no_duplicate_on_retry(configured):
    _, root, _ = configured
    with live_http() as port:
        first = start(port)[1]
        assert start(port)[0] == 200
        app.state.supervisor.stop(first["launch_id"])
        second = start(port, "launchTwo")[1]
        page = call(port, "GET", "/api/platform/v1/recent-work?limit=1")[1]
        assert len(page["items"]) == 1 and page["next_cursor"] == "1"
        query = f"/api/platform/v1/recent-work?limit=1&cursor=1&snapshot_revision={page['snapshot_revision']}"
        assert call(port, "GET", query)[1]["items"][0]["launch_id"] == first["launch_id"]
        app.state.supervisor.stop(second["launch_id"])
        start(port, "launchThree")
        assert call(port, "GET", query)[0] == 409
    with live_http() as port:
        persisted = call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]
        assert len(persisted["items"]) == 3
        assert all(item["source"] == "platform_launch" for item in persisted["items"])
        assert call(port, "POST", "/api/platform/v1/apps/classification/launches/launchOne",
                    origin=f"http://127.0.0.1:{port}")[0] == 409
        assert start(port, "launchOne")[0] == 410  # A prior owned Job cannot be recreated after restart.
        assert len((root / "starts.txt").read_text().splitlines()) == 3
        assert len(app.routes) > 0  # Router count also asserted by live_http on each shutdown.


def test_lifespan_recreates_supervisor_and_missing_store_is_honest(configured, monkeypatch):
    first = None
    with live_http() as port:
        first = app.state.supervisor
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 404
    assert first._closed
    monkeypatch.delenv("WORKBENCH_DATA_DIR")
    with live_http() as port:
        assert app.state.supervisor is not first
        assert call(port, "GET", "/api/platform/v1/apps/ocr/installation") == (200, {
            "schema_version": "1.0", "app_id": "ocr", "installed": True,
            "launch_ready": False, "state": "storage_unavailable"})
        assert call(port, "GET", "/api/platform/v1/recent-work?limit=200")[0] == 503
        assert call(port, "POST", "/api/platform/v1/workspaces/workspaceA/open-contexts",
                    origin=f"http://127.0.0.1:{port}", body=b"{}")[0] == 503
        assert start(port)[0] == 503  # Durable request IDs require a trusted data directory.


def test_uncertain_live_identity_blocks_a_second_process(configured, monkeypatch):
    from workbench_platform.catalog import launch as module
    _, root, _ = configured
    with live_http() as port:
        assert start(port)[0] == 201
        with monkeypatch.context() as patch:
            patch.setattr(module, "_identity", lambda *args: (_ for _ in ()).throw(OSError("temporary identity probe")))
            assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 503
            assert start(port, "launchTwo")[0] == 503
        assert len((root / "starts.txt").read_text().splitlines()) == 1


def test_current_is_reserved_from_launch_identity(configured):
    with live_http() as port:
        assert start(port, "current")[0] == 400
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 404


def test_live_revision_or_current_mismatch_blocks_a_second_process(configured):
    _, root, _ = configured
    with live_http() as port:
        assert start(port)[0] == 201
        supervisor = app.state.supervisor
        original = supervisor._current["ocr"]
        try:
            supervisor._current["ocr"] = (original[0] + 1, original[1])
            assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 503
            assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/launchOne")[0] == 503
            assert start(port, "launchTwo")[0] == 503
        finally:
            supervisor._current["ocr"] = original
        supervisor.registry.reload()
        assert call(port, "GET", "/api/platform/v1/apps/ocr/launches/current")[0] == 503
        assert start(port, "launchThree")[0] == 503
        assert len((root / "starts.txt").read_text().splitlines()) == 1


def test_recent_write_failure_is_recoverable_by_same_id(configured, monkeypatch):
    _, root, _ = configured
    with live_http() as port:
        store = app.state.recent_store
        original = store.record_launch
        def fail_once(receipt):
            monkeypatch.setattr(store, "record_launch", original)
            raise OSError("write failed")
        monkeypatch.setattr(store, "record_launch", fail_once)
        assert start(port)[0] == 503
        assert call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]["items"] == []
        assert start(port)[0] == 200
        assert len(call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]["items"]) == 1
        assert len((root / "starts.txt").read_text().splitlines()) == 1


def test_startup_exit_or_timeout_never_records_recent(configured):
    _, root, install = configured
    for mode in ("exit", "silent"):
        (root / "mode.txt").write_text(mode, encoding="utf-8")
        catalog_routes._registry = install()
        with live_http() as port:
            app.state.supervisor.handshake_timeout = 0.6
            assert start(port, "launch" + mode.capitalize())[0] == 503
            assert call(port, "GET", "/api/platform/v1/recent-work?limit=200")[1]["items"] == []
