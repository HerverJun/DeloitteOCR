"""Native OCR export must pin the result revision and source version."""

import json

import pytest

from workbench_platform.bridge.context import ResolvedContext
from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable, ResultArtifact


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
