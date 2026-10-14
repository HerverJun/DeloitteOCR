"""The candidate frontend is served independently of shared OCR model assets."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from ocr_workbench.service import create_app


class StaticFrontendBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        (self.bundle / "config").mkdir(parents=True)
        (self.bundle / "config/engines.json").write_text("{}", encoding="utf-8")
        (self.bundle / "web").mkdir()
        (self.bundle / "web/index.html").write_text("shared-old-page", encoding="utf-8")

    def test_candidate_index_and_asset_are_served_without_overwriting_bundle(self):
        web = self.root / "candidate/frontend/dist"
        (web / "assets").mkdir(parents=True)
        (web / "index.html").write_text("candidate-new-page", encoding="utf-8")
        (web / "assets/app.js").write_text("console.log('candidate');", encoding="utf-8")
        app = create_app(self.bundle, self.root / "data", "t" * 32,
                         start_queue=False, agent_enabled=False, web_root=web)
        with TestClient(app) as client:
            self.assertEqual(client.get("/?view=project").text, "candidate-new-page")
            self.assertEqual(client.get("/assets/app.js").text, "console.log('candidate');")
        self.assertEqual((self.bundle / "web/index.html").read_text(encoding="utf-8"),
                         "shared-old-page")
        self.assertEqual(app.state.web_root, web.resolve())

    def test_missing_explicit_frontend_fails_before_workspace_creation(self):
        data = self.root / "uncreated-data"
        with self.assertRaisesRegex(ValueError, "missing index.html"):
            create_app(self.bundle, data, "t" * 32, start_queue=False,
                       web_root=self.root / "missing-dist")
        self.assertFalse(data.exists())

    def test_legacy_default_still_serves_bundle_web(self):
        app = create_app(self.bundle, self.root / "legacy-data", "t" * 32,
                         start_queue=False, agent_enabled=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/").text, "shared-old-page")


if __name__ == "__main__":
    unittest.main()
