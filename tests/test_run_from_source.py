"""Exercise source preview isolation and its model-free API path."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_from_source.py"
spec = importlib.util.spec_from_file_location("source_preview", SCRIPT)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class SourcePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_runtime_locations_and_child_processes_remain_on_f_drive(self):
        previous_tempdir = tempfile.tempdir
        previous_bytecode = sys.dont_write_bytecode
        self.addCleanup(setattr, tempfile, "tempdir", previous_tempdir)
        self.addCleanup(setattr, sys, "dont_write_bytecode", previous_bytecode)
        with mock.patch.dict(os.environ):
            original_user_directories = {key: os.environ.get(key) for key in ("HOME", "CODEX_HOME")}
            runtime = preview.configure_storage(self.root / "runtime")
            for name in ("TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "XDG_CACHE_HOME",
                         "PIP_CACHE_DIR", "npm_config_cache", "HF_HOME", "TORCH_HOME"):
                self.assertTrue(Path(os.environ[name]).is_relative_to(runtime))
            self.assertEqual(original_user_directories,
                             {key: os.environ.get(key) for key in original_user_directories})
            child = subprocess.run([sys.executable, "-I", "-B", "-c",
                                    "import os,tempfile;print(tempfile.gettempdir());print(os.environ['LOCALAPPDATA'])"],
                                   check=True, capture_output=True, text=True)
            for path in child.stdout.splitlines():
                self.assertTrue(Path(path).is_relative_to(runtime))

    def test_rejects_other_drive_before_creating_runtime(self):
        with self.assertRaisesRegex(ValueError, "local F drive"):
            preview.configure_storage(Path("C:/source-preview-must-not-be-created"))

    def test_source_frontend_uses_current_service_binding_in_review_mode(self):
        from fastapi.testclient import TestClient

        frontend = self.root / "frontend"
        (frontend / "dist").mkdir(parents=True)
        (frontend / "dist/index.html").write_text("source-preview-current-frontend", encoding="utf-8")
        with mock.patch.object(preview, "FRONTEND", frontend):
            app = preview.app_for(self.root / "data", "t" * 32)
        with TestClient(app) as client:
            self.assertEqual(client.get("/").text, "source-preview-current-frontend")
            health = client.get("/api/health").json()
            self.assertEqual(health["status"], "ready")
            self.assertTrue(health["review_only"])
        self.assertFalse(app.state.start_queue)
        self.assertEqual(app.state.web_root, frontend / "dist")

    def test_setup_installs_only_service_requirements_and_builds_source_frontend(self):
        python = self.root / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"test interpreter placeholder")
        frontend = self.root / "frontend"
        with mock.patch.object(preview, "ROOT", self.root), \
                mock.patch.object(preview, "FRONTEND", frontend), \
                mock.patch.object(preview.shutil, "which", return_value="npm.cmd"), \
                mock.patch.object(preview.subprocess, "run") as run:
            preview.setup()
        self.assertEqual(run.call_count, 3)
        arguments = run.call_args_list[0].args[0]
        self.assertEqual(arguments[0], str(python))
        self.assertEqual(arguments[-1], str(self.root / "config/service-requirements.txt"))
        self.assertEqual(run.call_args_list[1].args[0], ["npm.cmd", "ci"])
        self.assertEqual(run.call_args_list[2].args[0], ["npm.cmd", "run", "build"])
        self.assertTrue(all(call.kwargs.get("cwd") == frontend for call in run.call_args_list[1:]))

    def test_model_free_smoke_imports_and_transforms_image(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "--runtime-root",
             str(self.root / "runtime"), "smoke"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["mode"], "model_free")
        self.assertEqual(payload["model_inference"], "not_run")
        self.assertIn("rotate", payload["checks"])
        self.assertFalse(list((self.root / "runtime/temp").glob("ocr-source-smoke-*")))


if __name__ == "__main__":
    unittest.main()
