"""Native OCR export must pin the result revision and source version."""

import json

import pytest

from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable, OcrResultConflict, ResultArtifact


def test_export_pins_revision_and_rejects_native_conflict(monkeypatch):
    context = ResolvedContext("workspaceA", "ocr", "instanceA", "linkA",
                              "projectA", "owner", "/jobs", "http://127.0.0.1:8765")
    adapter = OcrAdapter(context, token=lambda: "a" * 32, live=lambda _: True)
    result = ResultArtifact("resultA", "taskA", "imageA", "versionA", True,
                            {"kind": "image"})
    requests = []
    response = {"status": 200, "body": b'{"id":"resultA","revision":0}'}

    class FakeResponse:
        @property
        def status(self):
            return response["status"]

        def getheader(self, name):
            return str(len(response["body"])) if name == "Content-Length" else None

        def read(self, _limit):
            return response["body"]

    class FakeConnection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 8765, 30)

        def request(self, method, path, *, body, headers):
            requests.append((method, path, json.loads(body), headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("workbench_platform.jobs.native_ocr.http.client.HTTPConnection",
                        FakeConnection)
    data, media = adapter.export_results([(result, 0)])
    assert media == "application/json" and json.loads(data)["revision"] == 0
    assert requests[0][2]["expected_results"] == {
        "resultA": {"revision": 0, "version_id": "versionA"}}
    response["status"] = 409
    with pytest.raises(OcrUnavailable):
        adapter.export_results([(result, 0)])
    response.update(status=200, body=b'{"id":"resultA","revision":1}')
    with pytest.raises(OcrUnavailable):
        adapter.export_results([(result, 0)])


@pytest.mark.parametrize('status,expected', [(409, OcrResultConflict), (503, OcrUnavailable)])
def test_screen_export_preserves_native_revision_conflict(monkeypatch, status, expected):
    context = ResolvedContext('workspaceA', 'ocr', 'instanceA', 'linkA',
                              'projectA', 'owner', '/jobs', 'http://127.0.0.1:8765')
    adapter = OcrAdapter(context, token=lambda: 'a' * 32, live=lambda _: True)
    closed = []

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ('127.0.0.1', 8765, 120)

        def request(self, method, path, *, body, headers):
            assert method == 'POST' and path == '/api/projects/projectA/screen-batches/batchA/export'

        def getresponse(self):
            return type('Response', (), {'status': status})()

        def close(self):
            closed.append(True)

    monkeypatch.setattr('workbench_platform.jobs.native_ocr.http.client.HTTPConnection', Connection)
    with pytest.raises(expected) as error:
        adapter.screen_export('projectA', 'batchA', {'include_pending_review': True})
    assert type(error.value) is expected
    assert closed == [True]
