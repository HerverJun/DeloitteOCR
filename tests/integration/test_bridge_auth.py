"""Real loopback HTTP and supervised fixture; no legacy application is launched."""

import hashlib
import http.client
import json
import shutil
import socket
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from packages.contracts.models import ContextIssueRequest, OpenContext
from workbench_platform.bridge import (
    BridgeDenied, ContextBridge, NativeAdapter, NativeRoute, bridge_router,
)
from workbench_platform.catalog.registry import AppRegistry, DEFAULT_CONFIG
from workbench_platform.store import WorkspaceStore
from workbench_platform.supervisor import Supervisor


SERVER = '''
import http.server, json, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/platform/v1/identity':
            data = {'app_id': os.environ['WORKBENCH_APP_ID'],
                    'instance_id': os.environ['WORKBENCH_INSTANCE_ID'],
                    'nonce': os.environ['WORKBENCH_NONCE'], 'version': 'fixture-1'}
        elif self.path == '/native/echo':
            data = {'authenticated': self.headers.get('Authorization') == 'Bearer private-test-token',
                    'origin': self.headers.get('Origin')}
        elif self.path == '/native/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://example.invalid/escape')
            self.end_headers()
            return
        elif self.path == '/native/bad':
            self.wfile.write(b'private-transport-sentinel\\r\\n')
            self.wfile.flush()
            self.close_connection = True
            return
        else:
            self.send_error(404)
            return
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        if self.path != '/native/write':
            self.send_error(404)
            return
        with open('write-count.txt', 'a') as output:
            output.write('1')
        self.send_response(204)
        self.end_headers()
    def log_message(self, *args): pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
print('READY ' + str(server.server_address[1]), flush=True)
server.serve_forever()
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def channel(tmp_path):
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
    names = ["Scripts/python.exe", "pyvenv.cfg", "server.py"]
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
    registry = AppRegistry(config, install_root=tmp_path / "bundles")
    store = WorkspaceStore(tmp_path / "workspaces.sqlite3")
    one = store.create_workspace("One")
    two = store.create_workspace("Two")
    link = store.add_link(one.workspace_id, "ocr", "native-one", role="editor",
                          expected_revision=one.revision)
    other = store.add_link(two.workspace_id, "ocr", "native-two", role="owner",
                           expected_revision=two.revision)
    with Supervisor(registry) as supervisor:
        receipt = supervisor.start("ocr", "launchOne")
        adapter = NativeAdapter({"echo": NativeRoute("GET", "/native/echo"),
                                 "write": NativeRoute("POST", "/native/write"),
                                 "bad": NativeRoute("GET", "/native/bad"),
                                 "redirect": NativeRoute("GET", "/native/redirect")},
                                lambda resolved: {"Authorization": "Bearer private-test-token",
                                                  "Origin": resolved.base_url.rstrip("/")})
        bridge = ContextBridge(store, supervisor, adapters={"ocr": adapter})
        yield store, supervisor, receipt, bridge, one.workspace_id, two.workspace_id, link, other


def request(receipt, link, **values):
    return ContextIssueRequest(schema_version="1.0", app_id="ocr", instance_id=receipt.instance_id,
                               link_id=link.link_id, return_route="/workspaces", **values)


def issued(channel):
    _, _, receipt, bridge, workspace, _, link, _ = channel
    return bridge.issue(request(receipt, link), workspace_id=workspace)


def test_issue_consume_replay_and_workspace_restoration(channel):
    _, _, _, bridge, workspace, other_workspace, _, _ = channel
    context = issued(channel)
    with pytest.raises(BridgeDenied):
        bridge.consume(context, workspace_id=other_workspace)
    resolved = bridge.consume(context, workspace_id=workspace)
    assert (resolved.workspace_id, resolved.module_project_id, resolved.return_route) == (
        workspace, "native-one", "/workspaces")
    with pytest.raises(BridgeDenied):
        bridge.consume(context, workspace_id=workspace)


def test_concurrent_consume_is_exactly_once(channel):
    _, _, _, bridge, workspace, _, _, _ = channel
    context = issued(channel)
    def consume(_):
        try:
            bridge.consume(context, workspace_id=workspace)
            return True
        except BridgeDenied:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(8))) == 1


def test_tamper_expiry_and_unknown_objects(channel):
    _, _, receipt, bridge, workspace, _, link, _ = channel
    context = issued(channel)
    tampered = context.model_copy(update={"return_route": "/elsewhere"})
    with pytest.raises(BridgeDenied):
        bridge.consume(tampered, workspace_id=workspace)
    assert bridge.consume(context, workspace_id=workspace)
    mutable = issued(channel)
    mutable.return_route = "/elsewhere"
    with pytest.raises(BridgeDenied):
        bridge.consume(mutable, workspace_id=workspace)
    bridge.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=10)
    stale = bridge.issue(request(receipt, link), workspace_id=workspace)
    bridge.clock = lambda: datetime.fromisoformat(stale.expires_at.replace("Z", "+00:00"))
    with pytest.raises(BridgeDenied) as error:
        bridge.consume(stale, workspace_id=workspace)
    assert error.value.code == "CONTEXT_EXPIRED"
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, link, asset_set_id="unknownAsset"), workspace_id=workspace)
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, link, artifact_id="unknownArtifact"), workspace_id=workspace)


def test_link_mutation_and_wrong_project_are_rejected(channel):
    store, _, receipt, bridge, workspace, other_workspace, link, other = channel
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, other), workspace_id=workspace)
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, link), workspace_id=other_workspace)
    context = issued(channel)
    current = store.get_workspace(workspace)
    store.update_link_role(workspace, link.link_id, "viewer",
                           expected_revision=current.revision, expected_link_revision=link.revision)
    with pytest.raises(BridgeDenied):
        bridge.consume(context, workspace_id=workspace)
    changed = issued(channel)
    current = store.get_workspace(workspace)
    store.remove_link(workspace, link.link_id, expected_revision=current.revision)
    with pytest.raises(BridgeDenied):
        bridge.consume(changed, workspace_id=workspace)


def test_historical_receipt_and_instance_replacement(channel):
    _, supervisor, receipt, bridge, workspace, _, link, _ = channel
    context = issued(channel)
    supervisor.stop(receipt.launch_id)
    assert supervisor.start("ocr", receipt.launch_id) == receipt  # Historical idempotency result.
    with pytest.raises(BridgeDenied) as error:
        bridge.consume(context, workspace_id=workspace)
    assert error.value.code == "APP_UNAVAILABLE"
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, link), workspace_id=workspace)
    replacement = supervisor.start("ocr", "launchTwo")
    assert replacement.instance_id != receipt.instance_id
    with pytest.raises(BridgeDenied):
        bridge.issue(request(receipt, link), workspace_id=workspace)
    assert bridge.issue(request(replacement, link), workspace_id=workspace)


def test_listener_ownership_and_handshake_are_rechecked(channel, monkeypatch):
    from workbench_platform.bridge import context as module
    _, _, receipt, bridge, workspace, _, link, _ = channel
    context = issued(channel)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_listener_pid", lambda _: receipt.pid + 1)
        with pytest.raises(BridgeDenied) as error:
            bridge.consume(context, workspace_id=workspace)
        assert error.value.code == "APP_UNAVAILABLE"
    with monkeypatch.context() as patch:
        patch.setattr(module, "_identity", lambda *args: "wrong-version")
        with pytest.raises(BridgeDenied):
            bridge.issue(request(receipt, link), workspace_id=workspace)
    assert bridge.consume(context, workspace_id=workspace)


def test_native_forward_uses_registered_route_and_server_credentials(channel):
    _, _, _, bridge, workspace, _, _, _ = channel
    context = issued(channel)
    with pytest.raises(BridgeDenied):
        bridge.forward(context, workspace_id=workspace, route_name="http://example.invalid/")
    response = bridge.forward(context, workspace_id=workspace, route_name="echo")
    assert response.status == 200
    assert json.loads(response.body)["authenticated"] is True
    assert json.loads(response.body)["origin"].startswith("http://127.0.0.1:")
    with pytest.raises(BridgeDenied):
        bridge.forward(context, workspace_id=workspace, route_name="echo")
    redirect = bridge.forward(issued(channel), workspace_id=workspace, route_name="redirect")
    assert redirect.status == 302 and redirect.body == b""


def test_native_write_role_and_callback_failure(channel):
    store, _, _, bridge, workspace, _, link, _ = channel
    current = store.get_workspace(workspace)
    store.update_link_role(workspace, link.link_id, "viewer",
                           expected_revision=current.revision, expected_link_revision=link.revision)
    with pytest.raises(BridgeDenied):
        bridge.forward(issued(channel), workspace_id=workspace, route_name="write", body=b"data")
    assert not (bridge.supervisor.registry.installations["ocr"].working_directory / "write-count.txt").exists()
    assert bridge.forward(issued(channel), workspace_id=workspace, route_name="echo").status == 200
    bridge.adapters["ocr"] = NativeAdapter({"echo": NativeRoute("GET", "/native/echo")},
                                            lambda _: (_ for _ in ()).throw(RuntimeError("private-test-token")))
    with pytest.raises(BridgeDenied) as error:
        bridge.forward(issued(channel), workspace_id=workspace, route_name="echo")
    assert "private-test-token" not in str(error.value)


def test_native_transport_and_header_encoding_do_not_expose_text(channel):
    _, _, _, bridge, workspace, _, _, _ = channel
    with pytest.raises(BridgeDenied) as error:
        bridge.forward(issued(channel), workspace_id=workspace, route_name="bad")
    assert "private-transport-sentinel" not in "".join(traceback.format_exception(error.value))
    bridge.adapters["ocr"] = NativeAdapter({"echo": NativeRoute("GET", "/native/echo")},
                                            lambda _: {"Authorization": "Bearer private-encoding-🙂"})
    with pytest.raises(BridgeDenied) as error:
        bridge.forward(issued(channel), workspace_id=workspace, route_name="echo")
    assert "private-encoding" not in "".join(traceback.format_exception(error.value))


@pytest.fixture
def http_channel(channel):
    *_, bridge, workspace, other_workspace, link, other = channel
    app = FastAPI()
    app.include_router(bridge_router(bridge))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", log_level="error",
                                           access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        yield port, channel
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def post(port, workspace, payload, *, host=None, origin=None, content_type="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    host = host or f"127.0.0.1:{port}"
    headers = {"Host": host, "Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request("POST", f"/api/platform/v1/workspaces/{workspace}/open-contexts",
                           body=json.dumps(payload).encode(), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_real_http_host_origin_and_payload_boundaries(http_channel):
    port, channel = http_channel
    _, _, receipt, bridge, workspace, _, link, _ = channel
    payload = request(receipt, link).model_dump()
    good = f"http://127.0.0.1:{port}"
    status, body = post(port, workspace, payload, origin=good)
    assert status == 201
    context = OpenContext.model_validate(body)
    assert bridge.consume(context, workspace_id=workspace).module_project_id == "native-one"
    for host, origin in [(None, None), ("evil.test", "http://evil.test"),
                         (f"127.0.0.1:{port}", f"http://127.0.0.1:{port + 1}"),
                         (f"localhost:{port}", good),
                         ("rebind.test", good)]:
        status, _ = post(port, workspace, payload, host=host, origin=origin)
        assert status == 403
    for change in ({"instance_id": "fakeInstance"}, {"link_id": "unknownLink"},
                   {"asset_set_id": "unknownAsset"}, {"return_route": "//evil.test"},
                   {"headers": {"Authorization": "Bearer attacker"}},
                   {"base_url": "http://127.0.0.1:1234/"}):
        status, _ = post(port, workspace, payload | change, origin=good)
        assert status in (400, 503)


def test_real_http_rejects_wrong_workspace_and_json_type(http_channel):
    port, channel = http_channel
    _, _, receipt, _, workspace, other_workspace, link, _ = channel
    payload = request(receipt, link).model_dump()
    good = f"http://127.0.0.1:{port}"
    assert post(port, other_workspace, payload, origin=good)[0] == 400
    assert post(port, workspace, payload, origin=good, content_type="text/plain")[0] == 400
