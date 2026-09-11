import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile
from ocr_workbench.engine_packages import EnginePackages, member_path


class EnginePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bundle = self.root / "bundle"
        (bundle / "config").mkdir(parents=True)
        self.spec = {
            "name": "Test OCR",
            "models": ["model"],
            "capabilities": {"text": True, "tables": False},
        }
        (bundle / "config/engines.json").write_text(json.dumps({"ppocr": self.spec}))
        self.registry = EnginePackages(bundle, self.root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def package(self, package_id="ppocr-test-v1", change=None, extra=None):
        files = {
            "runtimes/ppocr/python.exe": b"unit-test-only",
            "runtimes/ppocr/python312.dll": b"dll",
            "runtimes/ppocr/python312.zip": b"zip",
            "runtimes/ppocr/python312._pth": b"python312.zip\n.\nLib/site-packages\n../../app\nimport site\n",
            "app/ocr_workbench/engine_host.py": b'raise RuntimeError("must not execute while staging")',
            "app/ocr_workbench/offline.py": b"",
            "config/engines.json": json.dumps({"ppocr": self.spec}).encode(),
            "locks/ppocr.json": b"[]",
            "models/model/source-manifest.json": b'{"revision":"test-revision"}',
            "models/model/weights.bin": b"weights",
        }
        if change:
            files.update(change)
        manifest = {
            "schema_version": 1,
            "protocol": "file-ipc-v1",
            "id": package_id,
            "engine": "ppocr",
            "name": "Test OCR",
            "version": "test",
            "files": [
                {
                    "path": name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for name, data in files.items()
            ],
        }
        path = self.root / (package_id + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr("engine-package.json", json.dumps(manifest))
            if extra:
                archive.writestr(*extra)
        return path

    def test_stage_does_not_execute_and_activation_is_atomic(self):
        with patch.object(self.registry, "probe") as probe, patch.object(
            self.registry, "smoke"
        ) as smoke:
            receipt = self.registry.stage(self.package())
            probe.assert_not_called()
            smoke.assert_not_called()
            pending = self.registry.inventory()["staged"]
            self.assertEqual(pending[0]["staging_id"], receipt["staging_id"])
            self.assertTrue(pending[0]["ready"])
            self.assertEqual(self.registry.engines()["ppocr"]["package_id"], "builtin")
            self.registry.activate(receipt["staging_id"], receipt["sha256"])
            probe.assert_called_once()
            smoke.assert_called_once()
        self.assertEqual(
            self.registry.engines()["ppocr"]["package_id"], "ppocr-test-v1"
        )
        self.registry.switch("ppocr", "builtin")
        self.assertEqual(self.registry.engines()["ppocr"]["package_id"], "builtin")
        self.assertTrue((self.registry.packages / "ppocr-test-v1").exists())

    def test_failed_probe_keeps_current_and_staged_package(self):
        receipt = self.registry.stage(self.package())
        with patch.object(
            self.registry, "probe", side_effect=ValueError("dependency failure")
        ):
            with self.assertRaises(ValueError):
                self.registry.activate(receipt["staging_id"], receipt["sha256"])
        self.assertEqual(self.registry.active(), {})
        self.assertTrue((self.registry.staging / receipt["staging_id"]).exists())

    def test_failed_real_inference_keeps_current(self):
        receipt = self.registry.stage(self.package())
        with patch.object(self.registry, "probe"), patch.object(
            self.registry, "smoke", side_effect=RuntimeError("model failure")
        ):
            with self.assertRaises(RuntimeError):
                self.registry.activate(receipt["staging_id"], receipt["sha256"])
        self.assertEqual(self.registry.active(), {})

    def test_modified_staged_file_and_extra_code_rejected(self):
        receipt = self.registry.stage(self.package())
        (self.registry.staging / receipt["staging_id"] / "app/extra.py").write_text(
            "unlisted"
        )
        with self.assertRaises(ValueError):
            self.registry.activate(receipt["staging_id"], receipt["sha256"])

        self.registry.discard(receipt["staging_id"])
        receipt = self.registry.stage(self.package())
        (
            self.registry.staging / receipt["staging_id"] / "models/model/weights.bin"
        ).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.registry.activate(receipt["staging_id"], receipt["sha256"])

    def test_staged_junction_rejected_before_execution(self):
        receipt = self.registry.stage(self.package())
        root = self.registry.staging / receipt["staging_id"]
        link = root / "linked"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(root / "models")],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        try:
            with patch.object(self.registry, "probe") as probe:
                with self.assertRaisesRegex(ValueError, "重解析"):
                    self.registry.activate(receipt["staging_id"], receipt["sha256"])
                probe.assert_not_called()
        finally:
            link.rmdir()

    def test_unsafe_paths_and_extra_archive_members_rejected(self):
        for value in [
            "../escape",
            "C:/escape",
            "a\\b",
            "NUL.txt",
            "folder/file:stream",
            "a./b",
            "a//b",
            "/absolute",
        ]:
            with self.assertRaises(ValueError):
                member_path(value)
        with self.assertRaises(ValueError):
            self.registry.stage(self.package(extra=("extra.py", b"code")))
        with self.assertRaises(ValueError):
            self.registry.stage(self.package(extra=("CONFIG/engines.json", b"{}")))
        self.assertEqual(list(self.registry.staging.iterdir()), [])

    def test_nonportable_runtime_and_missing_weights_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.stage(
                self.package(
                    change={"runtimes/ppocr/python312._pth": b"C:/Developer/Lib\n"}
                )
            )

    def test_restart_retains_active_and_old_version(self):
        receipts = [
            self.registry.stage(self.package("ppocr-test-v" + str(i))) for i in [1, 2]
        ]
        with patch.object(self.registry, "probe"), patch.object(self.registry, "smoke"):
            for receipt in receipts:
                self.registry.activate(receipt["staging_id"], receipt["sha256"])
            self.registry.switch("ppocr", "ppocr-test-v1")
        reopened = EnginePackages(self.registry.bundle, self.root / "data")
        self.assertEqual(reopened.engines()["ppocr"]["package_id"], "ppocr-test-v1")
        self.assertEqual(len(reopened.inventory()["installed"]), 2)


if __name__ == "__main__":
    unittest.main()
