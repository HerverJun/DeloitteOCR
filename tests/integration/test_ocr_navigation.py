"""OCR adapter navigation uses only a trusted resolved context and loopback."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from workbench_platform.bridge.context import ResolvedContext
from adapters.ocr.navigation import NavigationUnavailable, issue_navigation


class NativeFixture(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.received.append((self.path, dict(self.headers), body))
        if self.path != "/api/platform/navigation/issue" or self.headers.get("X-Workbench-Launch-Nonce") != "n" + "a" * 32:
            self.send_error(403)
            return
        value = json.loads(body)
        if value["app_id"] != "ocr" or value["platform_origin"] != "http://127.0.0.1:8765":
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
    value = dict(workspace_id="workspaceA", app_id="ocr", instance_id="instanceA",
                 link_id="linkA", module_project_id="nativeProjectA", role="editor",
                 return_route="/apps/ocr", base_url=f"http://127.0.0.1:{native.server_port}/")
    return ResolvedContext(**(value | overrides))


def test_trusted_context_issues_native_ticket_without_credential_in_url(native):
    entry = issue_navigation(context(native), platform_origin="http://127.0.0.1:8765",
                             launch_nonce="n" + "a" * 32)
    assert entry == f"http://127.0.0.1:{native.server_port}/#platform_ticket=" + "t" * 43
    path, headers, raw = NativeFixture.received[0]
    assert path == "/api/platform/navigation/issue"
    assert headers["X-Workbench-Launch-Nonce"] == "n" + "a" * 32
    assert json.loads(raw) == {"app_id": "ocr", "instance_id": "instanceA", "link_id": "linkA",
                               "workspace_id": "workspaceA", "return_route": "/apps/ocr",
                               "platform_origin": "http://127.0.0.1:8765"}
    assert "nativeProjectA" not in entry and "workspaceA" not in entry and "n" + "a" * 32 not in entry


@pytest.mark.parametrize("change", [
    {"app_id": "classification"}, {"return_route": "//remote.invalid"},
    {"return_route": "/apps/ocr?workspace=other"}, {"workspace_id": "../other"},
    {"base_url": "http://127.0.0.1:8765@remote.invalid"},
])
def test_untrusted_context_rejected_before_network(native, change):
    with pytest.raises(NavigationUnavailable, match="unavailable"):
        issue_navigation(context(native, **change), platform_origin="http://127.0.0.1:8765",
                         launch_nonce="n" + "a" * 32)
    assert NativeFixture.received == []


def test_external_platform_target_and_bad_nonce_rejected(native):
    for target, nonce in (("https://remote.invalid", "n" + "a" * 32),
                          ("http://127.0.0.1:8765", "line\r\ninjection")):
        with pytest.raises(NavigationUnavailable):
            issue_navigation(context(native), platform_origin=target, launch_nonce=nonce)
    assert NativeFixture.received == []
