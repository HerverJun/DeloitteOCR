"""Platform handoff tests use synthetic IDs and never start OCR engines."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ocr_workbench.platform_navigation import NavigationTickets
from ocr_workbench.service import create_app


class PlatformNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bundle = root / "bundle"
        (bundle / "config").mkdir(parents=True)
        (bundle / "config/engines.json").write_text(json.dumps({
            "glm": {"name": "Synthetic", "models": [], "capabilities": {"tables": True}}
        }), encoding="utf-8")
        with patch.dict(os.environ, {"WORKBENCH_APP_ID": "ocr", "WORKBENCH_INSTANCE_ID": "instanceA",
                                  "WORKBENCH_NONCE": "n" + "a" * 32}):
            self.app = create_app(bundle, root / "data", "t" * 32, start_queue=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.value = {"app_id": "ocr", "instance_id": "instanceA", "link_id": "linkA",
                      "workspace_id": "workspaceA", "project_id": "projectA",
                      "return_route": "/apps/ocr",
                      "platform_origin": "http://127.0.0.1:8765"}
        self.headers = {"X-Workbench-Launch-Nonce": "n" + "a" * 32}

    def test_issued_ticket_establishes_native_session_and_is_one_use(self):
        issue = self.client.post("/api/platform/navigation/issue", json=self.value, headers=self.headers)
        self.assertEqual(issue.status_code, 201)
        ticket = issue.json()["ticket"]
        path = f"/api/platform/navigation/tickets/{ticket}"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["return_url"], "http://127.0.0.1:8765/apps/ocr?workspace=workspaceA")
        self.assertEqual(response.json()["project_id"], "projectA")
        self.assertEqual(response.json()["session_token"], "t" * 32)
        self.assertEqual(response.headers["Content-Security-Policy"].split("frame-ancestors ")[1], "'none'")
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_untrusted_or_malformed_handoff_is_rejected(self):
        self.assertEqual(self.client.post("/api/platform/navigation/issue", json=self.value).status_code, 403)
        invalid = [
            {**self.value, "platform_origin": "https://example.com"},
            {**self.value, "platform_origin": "http://127.0.0.1:8765@evil.test"},
            {**self.value, "return_route": "//evil.test"},
            {**self.value, "return_route": "/apps/ocr?workspace=other"},
            {**self.value, "instance_id": "instanceB"},
            {**self.value, "workspace_id": "../other"},
            {**self.value, "project_id": "../other"},
            {**self.value, "credential": "secret"},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.client.post("/api/platform/navigation/issue", json=value,
                                                  headers=self.headers).status_code, 400)
        duplicate = json.dumps(self.value).replace('"app_id": "ocr"', '"app_id": "ocr", "app_id": "ocr"')
        self.assertEqual(self.client.post("/api/platform/navigation/issue", content=duplicate,
                                          headers=self.headers | {"Content-Type": "application/json"}).status_code, 400)

    def test_ticket_expiration_and_independent_start(self):
        tickets = NavigationTickets(instance_id="instanceA", ttl_seconds=1)
        ticket = tickets.issue(self.value)
        with patch("ocr_workbench.platform_navigation.time.monotonic", return_value=float("inf")):
            with self.assertRaises(KeyError):
                tickets.consume(ticket)
        root = Path(self.temp.name)
        with patch.dict(os.environ, {"WORKBENCH_APP_ID": "", "WORKBENCH_INSTANCE_ID": "", "WORKBENCH_NONCE": ""}):
            app = create_app(root / "bundle", root / "standalone", "t" * 32, start_queue=False)
        with TestClient(app) as standalone:
            self.assertEqual(standalone.get("/api/health").status_code, 200)
            self.assertEqual(standalone.post("/api/platform/navigation/issue", json=self.value,
                                             headers=self.headers).status_code, 401)
