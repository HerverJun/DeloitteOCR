"""Native opening verifies both the actual page and the pinned generation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from workbench_platform.catalog.launch import _classification_workbench, LaunchStatus
from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.jobs import ocr_projects, classification_projects


@pytest.mark.parametrize('module,app_id,error', [
    (ocr_projects, 'ocr', ocr_projects.OcrUnavailable),
    (classification_projects, 'classification', classification_projects.ClassificationUnavailable),
])
@pytest.mark.parametrize('change', ['before_ticket', 'after_ticket'])
def test_project_ticket_cannot_cross_native_generations(module, app_id, error, change, monkeypatch):
    old = SimpleNamespace(instance_id='instanceA', base_url='http://127.0.0.1:12345/', handshake_nonce='nonceA')
    new = SimpleNamespace(instance_id='instanceB', base_url='http://127.0.0.1:54321/', handshake_nonce='nonceB')
    receipts = iter([new] if change == 'before_ticket' else [old, new])
    context = ResolvedContext('workspaceA', app_id, old.instance_id, 'linkA', 'projectA', 'owner', '/jobs', old.base_url)
    jobs = SimpleNamespace(_context=lambda *_: context, launches=SimpleNamespace(current=lambda _: next(receipts)))
    service = (module.OcrProjects if app_id == 'ocr' else module.ClassificationProjects)(jobs)
    monkeypatch.setattr(service, 'projects', lambda _: {'items':[{'project_id':'projectA'}]})
    issued = []
    monkeypatch.setattr(module, 'issue_navigation', lambda *args, **kwargs: issued.append(args[0]) or old.base_url+'#platform_ticket='+'t'*43)
    with pytest.raises(error): service.open('workspaceA', 'linkA', 'http://127.0.0.1:8792')
    assert len(issued) == (0 if change == 'before_ticket' else 1)


@pytest.mark.parametrize('mode', ['valid', 'missing_page', 'replaced_service'])
def test_classification_open_requires_an_actual_page_and_same_live_instance(mode):
    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = b'<html><body>workbench</body></html>'
            self.send_response(404 if mode == 'missing_page' else 200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Page)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    first = SimpleNamespace(instance_id='instanceA',base_url=f'http://127.0.0.1:{server.server_port}/')
    second = SimpleNamespace(instance_id='instanceB',base_url=first.base_url)
    values = iter([first, second if mode == 'replaced_service' else first])
    service = SimpleNamespace(get=lambda *_: next(values))
    try:
        if mode == 'valid': assert _classification_workbench(service,'launchA') == {'url':first.base_url}
        else:
            with pytest.raises(LaunchStatus): _classification_workbench(service,'launchA')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
