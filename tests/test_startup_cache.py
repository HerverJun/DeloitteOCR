import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from ocr_workbench import startup
from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.verification_cache import VerificationCache


class StartupCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.data = self.root / "data"
        self.cache_directory = self.root / "receipts"
        for name in (*startup.REQUIRED_ENTRIES, "models/test/weights.bin"):
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test payload")
        self.manifest()
        self.probes = patch("ocr_workbench.startup.subprocess.run", side_effect=self.probe).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def probe(command, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            "GPU-test, RTX A4000, 16384, 14500, 595.97\n" if command[0] == "nvidia-smi" else "ok"
        ))

    def manifest(self):
        records = []
        for file in self.bundle.rglob("*"):
            if file.is_file() and file.name != "manifest.json":
                data = file.read_bytes()
                records.append({"path": file.relative_to(self.bundle).as_posix(),
                                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        (self.bundle / "manifest.json").write_text(json.dumps({"files": records}))

    def cache(self):
        return VerificationCache(self.bundle, directory=self.cache_directory)

    def run_check(self, **kwargs):
        return startup.run_startup_checks(self.bundle, kwargs.pop("data", self.data),
                                          cache_directory=self.cache_directory, **kwargs)

    def test_first_run_notice_precedes_probes_and_persists_through_progress(self):
        states = []
        def capture(path, value, **kwargs):
            states.append(json.loads(json.dumps(value)))
            write_json(path, value, **kwargs)
        def probe(*args, **kwargs):
            current = read_json(self.data / "launcher/startup-state.json")
            self.assertEqual(current["notice"], startup.FIRST_START_NOTICE)
            return self.probe(*args, **kwargs)
        with patch("ocr_workbench.startup.write_json", side_effect=capture), patch(
            "ocr_workbench.startup.subprocess.run", side_effect=probe
        ):
            result = self.run_check()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["reason"], "first_start")
        self.assertIn("full-file-sha256", result["checks"])
        progress = [s for s in states if "正在校验离线文件：" in s.get("message", "")]
        self.assertTrue(progress)
        self.assertTrue(all(s["notice"] == startup.FIRST_START_NOTICE for s in progress))
        self.assertTrue(result["last_verified_at"])

    def test_repeat_and_other_project_do_not_scan_hash_payloads_or_probe(self):
        self.assertEqual(self.run_check()["status"], "passed")
        with patch("os.scandir", side_effect=AssertionError("payload scan")), patch.object(
            Path, "rglob", side_effect=AssertionError("recursive scan")
        ), patch("hashlib.file_digest", side_effect=AssertionError("payload hash")), patch(
            "ocr_workbench.startup.subprocess.run", side_effect=AssertionError("dependency probe")
        ):
            for data in (self.data, self.root / "another project"):
                result = self.run_check(data=data)
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(result["verification"], "cached")
                self.assertNotIn("full-file-sha256", result["checks"])
                self.assertEqual(result["notice"], "")

    def test_upgrade_and_relocation_require_full_verification(self):
        self.run_check()
        (self.bundle / "web/index.html").write_bytes(b"new version")
        self.manifest()
        upgraded = self.run_check()
        self.assertEqual(upgraded["reason"], "version_changed")
        self.assertEqual(upgraded["notice"], startup.UPGRADE_NOTICE)
        moved = self.root / "moved"
        shutil.copytree(self.bundle, moved)
        self.bundle = moved
        self.assertEqual(self.run_check()["verification"], "full")

    def test_missing_malformed_incomplete_and_old_policy_receipts_fall_back(self):
        for invalid in (None, [], {"status": "passed"}, "broken"):
            self.run_check()
            cache = self.cache()
            if invalid is None:
                cache.path.unlink()
            else:
                cache.path.write_text(invalid if isinstance(invalid, str) else json.dumps(invalid))
            self.assertEqual(self.run_check()["verification"], "full")
        cache.invalidate(cache.identity())
        self.assertEqual(self.run_check()["reason"], "unfinished_check")
        record = read_json(cache.path)
        record["policy_version"] = 0
        write_json(cache.path, record)
        self.assertEqual(self.run_check()["reason"], "policy_changed")

    def test_core_receipt_cannot_enable_recognition_but_full_covers_core(self):
        self.assertEqual(self.run_check(review_only=True)["verification"], "core")
        self.assertEqual(self.run_check(review_only=True)["verification"], "cached")
        self.assertEqual(self.run_check()["reason"], "recognition_enabled")
        self.probes.reset_mock()
        self.assertEqual(self.run_check(review_only=True)["verification"], "cached")
        self.probes.assert_not_called()

    def test_force_detects_damage_and_failure_does_not_leave_old_success(self):
        self.run_check()
        (self.bundle / "models/test/weights.bin").write_bytes(b"corrupt")
        # The selected fast policy intentionally does not scan unchanged manifests.
        self.assertEqual(self.run_check()["verification"], "cached")
        failed = self.run_check(policy="full")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(read_json(self.cache().path)["status"], "checking")
        self.assertEqual(self.run_check()["status"], "failed")

    def test_forced_core_still_checks_core_files(self):
        self.run_check()
        (self.bundle / "web/index.html").write_bytes(b"corrupt")
        result = self.run_check(policy="full", review_only=True)
        self.assertEqual(result["status"], "failed")

    def test_missing_required_entry_invalidates_receipt(self):
        self.run_check()
        (self.bundle / "web/index.html").unlink()
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertEqual(read_json(self.cache().path)["status"], "checking")

    def test_low_disk_does_not_invalidate_verified_files(self):
        self.run_check()
        with patch("ocr_workbench.startup.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            self.assertEqual(self.run_check()["status"], "failed")
        self.assertEqual(self.run_check()["verification"], "cached")

    def test_manifest_change_during_check_cannot_be_saved(self):
        original = startup.run_checks
        def changing(*args, **kwargs):
            report = original(*args, **kwargs)
            path = self.bundle / "manifest.json"
            path.write_text(path.read_text() + " ")
            return report
        with patch("ocr_workbench.startup.run_checks", side_effect=changing):
            self.assertEqual(self.run_check()["status"], "failed")
        self.assertNotEqual(read_json(self.cache().path)["status"], "passed")

    def test_receipt_save_failure_warns_and_next_start_checks_again(self):
        with patch.object(VerificationCache, "save", side_effect=PermissionError("locked")):
            result = self.run_check()
        self.assertEqual(result["status"], "passed")
        self.assertIn("下次启动会重新", result["message"])
        self.assertEqual(self.run_check()["verification"], "full")

    def test_unavailable_cache_checks_fully_without_blocking_startup(self):
        with patch.object(VerificationCache, "locked", side_effect=PermissionError("directory")):
            result = self.run_check()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["warnings"])

    def test_interrupted_check_invalidates_success_and_releases_lock(self):
        self.run_check()
        with patch("ocr_workbench.startup.run_checks", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check(policy="full")
        self.assertEqual(self.run_check()["reason"], "unfinished_check")

    def test_concurrent_startups_only_run_one_full_check(self):
        original = startup.run_checks
        def slow(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)
        with patch("ocr_workbench.startup.run_checks", side_effect=slow) as checked, ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.run_check, data=self.root / f"data-{n}") for n in range(2)]
            results = [f.result(20) for f in futures]
        self.assertEqual(checked.call_count, 1)
        self.assertEqual({r["verification"] for r in results}, {"full", "cached"})
        self.assertTrue(all(r["status"] == "passed" for r in results))

    def test_lock_serializes_separate_processes(self):
        code = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from ocr_workbench.verification_cache import VerificationCache
c=VerificationCache(Path(sys.argv[2]), directory=Path(sys.argv[3]))
counter=Path(sys.argv[4])
with c.locked():
    n=int(counter.read_text()) if counter.exists() else 0
    time.sleep(.2)
    counter.write_text(str(n+1))
"""
        command = [sys.executable, "-B", "-c", code, str(Path(startup.__file__).parents[1]),
                   str(self.bundle), str(self.cache_directory), str(self.root / "counter")]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            _, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
        self.assertEqual((self.root / "counter").read_text(), "2")

    def test_service_cli_preserves_explicit_full_and_development_skip(self):
        from ocr_workbench import service
        token = self.root / "token"
        token.write_text("t" * 40)
        argv = ["service", "--bundle", str(self.bundle), "--data", str(self.data), "--token-file", str(token)]
        for flags, expected in (([], None), (["--startup-check", "auto"], "auto"),
                                (["--startup-check", "full"], "full"),
                                (["--verify-startup", "--startup-check", "auto"], "full")):
            with self.subTest(flags=flags), patch.object(sys, "argv", argv + flags), patch(
                "ocr_workbench.startup.run_startup_checks", return_value={"status": "passed"}
            ) as checks, patch("ocr_workbench.engine_packages.EnginePackages"), patch.object(
                service, "create_app"
            ), patch("uvicorn.Server") as server:
                service.main()
                server.return_value.run.assert_called_once()
                if expected is None:
                    checks.assert_not_called()
                else:
                    self.assertEqual(checks.call_args.kwargs["policy"], expected)

    def test_engine_failure_does_not_rehash_already_verified_base(self):
        from unittest.mock import MagicMock
        registry = MagicMock()
        registry.check_active.side_effect = ValueError("engine package damaged")
        self.assertEqual(self.run_check(registry=registry)["status"], "failed")
        registry.check_active.side_effect = None
        registry.check_active.return_value = []
        result = self.run_check(registry=registry)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["verification"], "cached")


if __name__ == "__main__":
    unittest.main()
