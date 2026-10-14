"""Real loopback adapter exchange, including rejected identities and targets."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from adapters.classification.navigation import NavigationUnavailable, issue_navigation
from workbench_platform.bridge.context import ResolvedContext


class NativeFixture(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.received.append((self.path, dict(self.headers), raw))
        if (self.path != "/api/v1/platform/navigation/issue"
                or self.headers.get("X-Workbench-Launch-Nonce") != "n" + "a" * 32
                or self.headers.get("X-Platform-Request") != "1"):
            self.send_error(403)
            return
        value = json.loads(raw)
        if (value["app_id"] != "classification" or value["platform_origin"] != "http://127.0.0.1:8765"
                or value["module_project_id"] != "nativeProjectA"):
            self.send_error(400)
            return
        payload = json.dumps({"ticket": "t" * 43}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def native():
    NativeFixture.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeFixture)
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
                 link_id="linkA", module_project_id="nativeProjectA", role="editor",
                 return_route="/apps/classification", base_url=f"http://127.0.0.1:{native.server_port}/")
    return ResolvedContext(**(value | overrides))


def test_native_project_mapping_and_optional_asset_are_server_side(native):
    entry = issue_navigation(context(native), platform_origin="http://127.0.0.1:8765",
                             launch_nonce="n" + "a" * 32, asset_id="assetA")
    assert entry == f"http://127.0.0.1:{native.server_port}/#platform_ticket=" + "t" * 43
    path, headers, raw = NativeFixture.received[0]
    assert path == "/api/v1/platform/navigation/issue"
    assert headers["X-Platform-Request"] == "1"
    assert json.loads(raw) == {"app_id": "classification", "instance_id": "instanceA",
                               "link_id": "linkA", "workspace_id": "workspaceA",
                               "module_project_id": "nativeProjectA", "asset_id": "assetA",
                               "return_route": "/apps/classification",
                               "platform_origin": "http://127.0.0.1:8765"}
    assert "nativeProjectA" not in entry and "workspaceA" not in entry and "assetA" not in entry


@pytest.mark.parametrize("change", [
    {"app_id": "ocr"}, {"return_route": "//remote.invalid"},
    {"return_route": "/apps/classification?workspace=other"},
    {"workspace_id": "../other"}, {"module_project_id": "../other"},
    {"base_url": "http://127.0.0.1:8765@remote.invalid"},
])
def test_untrusted_context_rejected_before_network(native, change):
    with pytest.raises(NavigationUnavailable):
        issue_navigation(context(native, **change), platform_origin="http://127.0.0.1:8765",
                         launch_nonce="n" + "a" * 32)
    assert NativeFixture.received == []


def test_external_target_bad_nonce_or_asset_rejected(native):
    for target, nonce, asset in (("https://remote.invalid", "n" + "a" * 32, None),
                                 ("http://127.0.0.1:8765", "line\r\ninjection", None),
                                 ("http://127.0.0.1:8765", "n" + "a" * 32, "../other")):
        with pytest.raises(NavigationUnavailable):
            issue_navigation(context(native), platform_origin=target, launch_nonce=nonce, asset_id=asset)
    assert NativeFixture.received == []
