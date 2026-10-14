import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from ocr_workbench.startup import verify_integrity, run_checks


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.file = self.bundle / "model.bin"
        self.file.write_bytes(b"weights")
        self.records = [
            {
                "path": "model.bin",
                "bytes": 7,
                "sha256": hashlib.sha256(b"weights").hexdigest(),
            }
        ]
        self.manifest()

    def manifest(self):
        (self.bundle / "manifest.json").write_text(
            json.dumps({"files": self.records}), "utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_changed_and_unlisted_files(self):
        self.assertEqual(verify_integrity(self.bundle), [])
        self.file.write_bytes(b"corrupt")
        self.assertTrue(any("内容损坏" in v for v in verify_integrity(self.bundle)))
        self.file.unlink()
        self.assertTrue(any("缺少文件" in v for v in verify_integrity(self.bundle)))
        (self.bundle / "extra.dll").write_bytes(b"extra")
        self.assertTrue(any("清单外" in v for v in verify_integrity(self.bundle)))

    def test_manifest_traversal_and_case_duplicate_rejected(self):
        self.records.append({**self.records[0], "path": "MODEL.BIN"})
        self.manifest()
        with self.assertRaises(ValueError):
            verify_integrity(self.bundle)
        self.records[0]["path"] = "../outside.bin"
        self.manifest()
        with self.assertRaises(ValueError):
            verify_integrity(self.bundle)

    def test_junction_rejected_without_hashing_target(self):
        external = self.root / "external"
        external.mkdir()
        link = self.bundle / "linked"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True
        )
        self.assertEqual(result.returncode, 0)
        try:
            self.assertTrue(any("重解析" in v for v in verify_integrity(self.bundle)))
        finally:
            link.rmdir()

    def test_corruption_prevents_dependency_code_execution_and_publishes_error(self):
        self.file.write_bytes(b"corrupt")
        with patch(
            "ocr_workbench.startup.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0, stdout="GPU-test, RTX A4000, 16384, 14500, 595.97\n"
            ),
        ) as run:
            report = run_checks(self.bundle, self.root / "data")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["status"], "failed")
        saved = json.loads(
            (self.root / "data/launcher/startup-state.json").read_text("utf-8")
        )
        self.assertIn("内容损坏", saved["message"])

    def test_missing_driver_has_actionable_error(self):
        with patch(
            "ocr_workbench.startup.subprocess.run",
            side_effect=FileNotFoundError("nvidia-smi"),
        ):
            report = run_checks(self.bundle, self.root / "data")
        self.assertEqual(report["status"], "failed")
        self.assertIn("NVIDIA 驱动", report["message"])

    def test_transient_windows_reader_lock_does_not_abort_check(self):
        replace = Path.replace
        attempts = []

        def transient(source, target):
            attempts.append(source)
            if len(attempts) <= 3:
                raise PermissionError("Windows sharing violation")
            return replace(source, target)

        self.file.write_bytes(b"corrupt")
        with patch.object(Path, "replace", transient), patch(
            "ocr_workbench.startup.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0, stdout="GPU-test, RTX A4000, 16384, 14500, 595.97\n"
            ),
        ):
            report = run_checks(self.bundle, self.root / "data")
        self.assertGreater(len(attempts), 3)
        self.assertTrue(any("内容损坏" in e for e in report["errors"]))
        self.assertFalse(any("sharing" in e for e in report["errors"]))

    def test_review_mode_skips_gpu_and_engine_code_but_probes_service(self):
        with patch(
            "ocr_workbench.startup.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="imports-ok", stderr=""),
        ) as run:
            report = run_checks(self.bundle, self.root / "data", review_only=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["mode"], "review_only")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            Path(run.call_args.args[0][0]), self.bundle / "runtimes/service/python.exe"
        )
        self.assertIn("core-file-sha256", report["checks"])

    def test_review_mode_still_rejects_corrupt_core_before_any_probe(self):
        self.file.write_bytes(b"corrupt")
        with patch("ocr_workbench.startup.subprocess.run") as run:
            report = run_checks(
                self.bundle, self.root / "data", integrity=False, review_only=True
            )
        self.assertEqual(report["status"], "failed")
        self.assertIn("内容损坏", report["message"])
        run.assert_not_called()

    def test_review_mode_allows_damaged_disabled_engine_files(self):
        path = self.bundle / "runtimes/glm/library.dll"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"corrupt")
        self.records.append(
            {
                "path": "runtimes/glm/library.dll",
                "bytes": 7,
                "sha256": hashlib.sha256(b"correct").hexdigest(),
            }
        )
        self.manifest()
        self.assertTrue(verify_integrity(self.bundle))
        self.assertEqual(verify_integrity(self.bundle, core_only=True), [])
        with patch(
            "ocr_workbench.startup.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="imports-ok", stderr=""),
        ) as run:
            report = run_checks(self.bundle, self.root / "data", review_only=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
