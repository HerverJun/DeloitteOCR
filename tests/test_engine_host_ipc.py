"""CPU regressions for Windows request ownership and shutdown observation."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ocr_workbench.adapter import Cancelled, EngineAdapter
from ocr_workbench.engine_host import consume_request


def windows_permission(code):
    error = PermissionError("simulated Windows file lock")
    error.winerror = code
    return error


class EngineHostIpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["TEMP"])
        self.root = Path(self.temp.name)
        self.request = self.root / "request.json"
        self.stop = self.root / "stop.json"
        self.data = {"id": "request-1", "image": "image.png"}
        self.request.write_text(json.dumps(self.data), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_transient_sharing_locks_claim_the_request_once(self):
        real_unlink = Path.unlink
        for code in (32, 33):
            with self.subTest(winerror=code):
                self.request.write_text(json.dumps(self.data), encoding="utf-8")
                calls = []

                def locked_once(path, *args, **kwargs):
                    calls.append(path)
                    if len(calls) == 1:
                        raise windows_permission(code)
                    return real_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", autospec=True, side_effect=locked_once), patch(
                    "ocr_workbench.engine_host.time.sleep"
                ) as sleep:
                    self.assertEqual(consume_request(self.request, self.stop), self.data)
                self.assertEqual(calls, [self.request, self.request])
                sleep.assert_called_once_with(.025)
                self.assertFalse(self.request.exists())

    def test_stop_during_sharing_retry_preserves_unclaimed_request(self):
        def stop_while_locked(path):
            self.stop.write_text("{}", encoding="utf-8")
            raise windows_permission(32)

        with patch.object(Path, "unlink", autospec=True, side_effect=stop_while_locked) as unlink, patch(
            "ocr_workbench.engine_host.time.sleep"
        ):
            self.assertIsNone(consume_request(self.request, self.stop))
        self.assertEqual(unlink.call_count, 1)
        self.assertEqual(json.loads(self.request.read_text(encoding="utf-8")), self.data)

    def test_persistent_sharing_lock_fails_at_bounded_deadline(self):
        with patch.object(Path, "unlink", side_effect=windows_permission(32)) as unlink, patch(
            "ocr_workbench.engine_host.time.monotonic", side_effect=[0, 0, 2]
        ), patch("ocr_workbench.engine_host.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                consume_request(self.request, self.stop)
        self.assertEqual(unlink.call_count, 2)
        sleep.assert_called_once_with(.025)
        self.assertTrue(self.request.exists())

    def test_nonsharing_permission_failure_is_immediate(self):
        with patch.object(Path, "unlink", side_effect=windows_permission(5)) as unlink, patch(
            "ocr_workbench.engine_host.time.sleep"
        ) as sleep:
            with self.assertRaises(PermissionError):
                consume_request(self.request, self.stop)
        unlink.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(self.request.exists())

    def adapter(self):
        adapter = EngineAdapter.__new__(EngineAdapter)
        adapter.ipc = self.root
        adapter.cancelled = threading.Event()
        adapter.process = Mock()
        return adapter

    def test_process_exit_that_sets_cancel_is_cancelled(self):
        adapter = self.adapter()

        def cancel_on_poll():
            adapter.cancelled.set()
            return 1

        adapter.process.poll.side_effect = cancel_on_poll
        with self.assertRaises(Cancelled):
            adapter.wait_for(self.root / "response.json", 1)

    def test_process_exit_without_cancel_remains_failure(self):
        adapter = self.adapter()
        adapter.process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "引擎异常退出"):
            adapter.wait_for(self.root / "response.json", 1)

    def test_expired_wait_without_cancel_remains_timeout(self):
        adapter = self.adapter()
        with patch("ocr_workbench.adapter.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                adapter.wait_for(self.root / "response.json", 1)
