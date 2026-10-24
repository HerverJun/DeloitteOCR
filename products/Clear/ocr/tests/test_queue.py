"""Deterministic queue race tests, complementing real GPU application acceptance."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
import sqlite3
from unittest.mock import patch
from contextlib import contextmanager
from PIL import Image
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.adapter import Cancelled


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data")
        self.project = self.store.project("Queue test")["id"]
        file = Path(self.temp.name) / "image.png"
        Image.new("RGB", (20, 20), "white").save(file)
        self.version = add_image(self.store, self.project, "image.png", file)[
            "active_version"
        ]
        self.loaded = threading.Event()
        self.release = threading.Event()
        self.unloaded = []
        self.factories = []
        self.failure = None
        self.cancel_exception = None
        self.stop_on_cancel = False
        self.block_inference = False
        self.inference_started = threading.Event()
        self.inference_release = threading.Event()
        owner = self

        class Fake:
            def __init__(self, bundle, engine, root):
                self.engine = engine
                self.ready = False
                self.process = None
                owner.factories.append(engine)

            def load(self):
                self.ready = True
                owner.loaded.set()
                while not owner.release.wait(0.01):
                    if self.cancelled.is_set():
                        self.cancel_failure()
                if owner.failure:
                    failure, owner.failure = owner.failure, None
                    raise RuntimeError(failure)

            def recognize(self, image, output):
                owner.inference_started.set()
                while owner.block_inference and not owner.inference_release.wait(0.01):
                    if self.cancelled.is_set():
                        self.cancel_failure()
                if self.cancelled.is_set():
                    self.cancel_failure()
                return {"text": "actual test state", "tables": [], "blocks": []}

            def cancel_failure(self):
                if owner.stop_on_cancel:
                    owner.queue.stopping.set()
                raise owner.cancel_exception or Cancelled()

            def unload(self):
                owner.unloaded.append(self.engine)

        self.queue = TaskQueue(self.store, Path(self.temp.name), Fake)
        self.queue.start()

    def tearDown(self):
        self.release.set()
        self.inference_release.set()
        self.queue.stop()
        self.temp.cleanup()

    def enqueue(self, engines):
        ids = self.store.enqueue(self.project, [self.version], engines)
        self.queue.wake.set()
        return ids

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("queue condition timed out")

    def test_cancel_while_loading_cannot_publish_result(self):
        key = self.enqueue(["ppocr"])[0]
        self.assertTrue(self.loaded.wait(3))
        self.queue.action(self.project, "cancel", [key])
        self.wait(lambda: bool(self.unloaded))
        self.assertEqual(self.store.one("tasks", key)["status"], "cancelled")
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])
        self.release.set()
        self.queue.action(self.project, "retry", [key])
        self.wait(lambda: self.store.one("tasks", key)["status"] == "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 1)

    def test_auto_fallback_preserves_selected_engine_and_actual_result_identity(self):
        self.failure = 'Pro is unavailable'
        self.release.set()
        key = self.store.enqueue(self.project, [self.version], ['auto'], fallback_engine='glm')[0]
        self.queue.wake.set()
        self.wait(lambda: self.store.one('tasks', key)['status'] == 'succeeded')
        task = self.store.one('tasks', key)
        result = self.store.result(task['result_id'])['original']
        self.assertEqual(self.factories, ['mineru_pro', 'glm'])
        self.assertEqual(task['engine'], 'auto')
        self.assertEqual(result['engine'], 'glm')
        self.assertEqual(result['engine_route']['fallback_engine'], 'glm')
        self.assertTrue(result['engine_route']['fallback_used'])
        self.assertIn('Pro is unavailable', result['engine_route']['fallback_reason'])

    def test_auto_inference_failure_does_not_fallback_after_acceptance(self):
        with patch.object(self.queue.factory, 'recognize', side_effect=RuntimeError('accepted inference failed')):
            key = self.store.enqueue(self.project, [self.version], ['auto'], fallback_engine='glm')[0]
            self.release.set()
            self.queue.wake.set()
            self.wait(lambda: self.store.one('tasks', key)['status'] == 'failed')
        self.assertEqual(self.factories, ['mineru_pro'])
        self.assertEqual(self.store.rows('SELECT * FROM results'), [])

    def test_auto_route_is_frozen_in_idempotent_submission(self):
        from ocr_workbench.store import Conflict
        key = self.store.enqueue(self.project, [self.version], ['auto'], request_id='frozen-route', fallback_engine='glm')[0]
        self.assertEqual(self.store.enqueue(self.project, [self.version], ['auto'], request_id='frozen-route', fallback_engine='glm'), [key])
        with self.assertRaises(Conflict):
            self.store.enqueue(self.project, [self.version], ['auto'], request_id='frozen-route', fallback_engine='ppocr')
        with self.assertRaises(ValueError):
            self.store.enqueue(self.project, [self.version], ['glm'], fallback_engine='ppocr')
        with self.assertRaises(ValueError):
            self.store.enqueue(self.project, [self.version], ['auto'], fallback_engine='mineru_pro')

    def test_stop_interrupts_loading_and_recovery_pauses_queued(self):
        ids = self.enqueue(["glm", "ppocr"])
        self.assertTrue(self.loaded.wait(3))
        self.queue.stop()
        self.assertFalse(self.queue.thread.is_alive())
        self.store.recover()
        self.assertEqual(
            {self.store.one("tasks", key)["status"] for key in ids},
            {"interrupted", "paused"},
        )
        self.assertEqual(self.unloaded, ["glm"])

    def test_stop_process_exit_while_loading_is_interrupted(self):
        self.cancel_exception = RuntimeError("引擎异常退出")
        key = self.enqueue(["hunyuan"])[0]
        self.assertTrue(self.loaded.wait(3))
        self.queue.stop()
        task = self.store.one("tasks", key)
        self.assertEqual(task["status"], "interrupted")
        self.assertIsNone(task["error"])
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])
        self.assertIsNone(self.queue.adapter)

    def test_stop_process_exit_during_inference_is_interrupted(self):
        self.cancel_exception = RuntimeError("引擎异常退出")
        self.block_inference = True
        self.release.set()
        key = self.enqueue(["hunyuan"])[0]
        self.assertTrue(self.inference_started.wait(3))
        self.queue.stop()
        task = self.store.one("tasks", key)
        self.assertEqual(task["status"], "interrupted")
        self.assertIsNone(task["error"])
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])
        self.assertEqual(self.unloaded, ["hunyuan"])
        self.assertIsNone(self.queue.adapter)

    def test_user_cancel_process_exit_is_cancelled(self):
        self.cancel_exception = RuntimeError("引擎异常退出")
        self.block_inference = True
        self.release.set()
        key = self.enqueue(["hunyuan"])[0]
        self.assertTrue(self.inference_started.wait(3))
        self.queue.action(self.project, "cancel", [key])
        self.wait(lambda: self.queue.status()["task_id"] is None)
        task = self.store.one("tasks", key)
        self.assertEqual(task["status"], "cancelled")
        self.assertIsNone(task["error"])
        self.assertFalse(self.queue.stopping.is_set())
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])
        self.assertIsNone(self.queue.adapter)

    def test_process_exit_without_cancel_remains_failed(self):
        self.failure = "引擎异常退出"
        self.release.set()
        key = self.enqueue(["hunyuan"])[0]
        self.wait(lambda: self.store.one("tasks", key)["status"] == "failed")
        self.wait(lambda: self.queue.adapter is None)
        self.assertIn("引擎异常退出", self.store.one("tasks", key)["error"])
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])

    def test_request_timeout_during_stop_remains_failed(self):
        self.cancel_exception = TimeoutError("识别超时")
        self.block_inference = True
        self.release.set()
        key = self.enqueue(["hunyuan"])[0]
        self.assertTrue(self.inference_started.wait(3))
        self.queue.stop()
        task = self.store.one("tasks", key)
        self.assertEqual(task["status"], "failed")
        self.assertIn("识别超时", task["error"])
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])
        self.assertIsNone(self.queue.adapter)

    def test_budget_expiry_process_exit_takes_priority_over_stop(self):
        self.cancel_exception = RuntimeError("引擎异常退出")
        self.stop_on_cancel = True
        with patch.object(self.queue, "_screen_budget_remaining", return_value=.03):
            key = self.enqueue(["hunyuan"])[0]
            self.wait(lambda: self.store.one("tasks", key)["status"] == "failed")
            self.wait(lambda: self.queue.adapter is None)
        task = self.store.one("tasks", key)
        self.assertTrue(self.queue.stopping.is_set())
        self.assertEqual(task["phase"], "预算已用尽")
        self.assertIn("预算已用尽", task["error"])
        self.assertEqual(self.store.rows("SELECT * FROM results"), [])

    def test_pause_resume_and_engine_switch_release_old_session(self):
        ids = self.enqueue(["glm", "ppocr"])
        self.assertTrue(self.loaded.wait(3))
        ppocr = next(k for k in ids if self.store.one("tasks", k)["engine"] == "ppocr")
        self.queue.action(self.project, "pause", [ppocr])
        self.release.set()
        self.wait(lambda: bool(self.unloaded))
        self.assertEqual(self.factories, ["glm"])
        self.assertEqual(self.store.one("tasks", ppocr)["status"], "paused")
        self.queue.action(self.project, "resume", [ppocr])
        self.wait(lambda: self.store.one("tasks", ppocr)["status"] == "succeeded")
        self.wait(lambda: len(self.unloaded) == 2)
        self.assertEqual(self.unloaded, ["glm", "ppocr"])

    def test_package_checks_cannot_overlap_queue_gpu_owner(self):
        key = self.enqueue(["ppocr"])[0]
        self.assertTrue(self.loaded.wait(3))
        with self.assertRaisesRegex(ValueError, "识别任务正在运行"):
            with self.queue.engine_maintenance():
                self.fail("second GPU owner admitted")
        self.release.set()
        self.wait(lambda: self.store.one("tasks", key)["status"] == "succeeded")
        self.wait(lambda: self.queue.status()["task_id"] is None)
        with self.queue.engine_maintenance():
            self.assertIsNone(self.queue.adapter)
            self.loaded.clear()
            next_key = self.enqueue(["glm"])[0]
            self.assertFalse(self.loaded.wait(0.1))
            self.assertEqual(self.store.one("tasks", next_key)["status"], "queued")
        self.wait(lambda: self.store.one("tasks", next_key)["status"] == "succeeded")

    def test_simulated_oom_releases_engine_continues_batch_and_retries_same_engine(
        self,
    ):
        self.failure = "CUDA out of memory: allocating test tensor"
        self.release.set()
        ids = self.enqueue(["glm", "ppocr"])
        self.wait(
            lambda: all(
                self.store.one("tasks", k)["status"] in {"failed", "succeeded"}
                for k in ids
            )
        )
        first, second = [self.store.one("tasks", k) for k in ids]
        self.assertEqual(first["status"], "failed")
        self.assertIn("显存", first["error"])
        self.assertIn("glm", self.unloaded)
        self.assertEqual(second["status"], "succeeded")
        preserved = second["result_id"]
        self.queue.action(self.project, "retry", [first["id"]])
        self.wait(lambda: self.store.one("tasks", first["id"])["status"] == "succeeded")
        self.assertEqual(self.store.one("tasks", first["id"])["engine"], "glm")
        self.assertEqual(self.store.one("tasks", second["id"])["result_id"], preserved)
        self.assertEqual(self.factories, ["glm", "ppocr", "glm"])

    def test_transient_claim_failure_recovers_and_reports_last_error(self):
        self.queue.stop()
        claim = self.store.claim
        attempts = []

        def fail_once():
            attempts.append(True)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            return claim()

        self.release.set()
        with patch.object(self.store, "claim", fail_once):
            self.queue.start()
            key = self.enqueue(["ppocr"])[0]
            self.wait(lambda: self.store.one("tasks", key)["status"] == "succeeded")
        self.wait(lambda: self.queue.status()["healthy"])
        state = self.queue.status()
        self.assertTrue(state["alive"])
        self.assertEqual(state["last_error"]["type"], "OperationalError")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 1)

    def test_persistent_claim_failure_suspends_until_explicit_recovery(self):
        self.queue.stop()
        self.queue.retry_delays = (0.01, 0.01)
        with patch.object(
            self.store,
            "claim",
            side_effect=sqlite3.OperationalError("database is locked"),
        ) as claim:
            self.queue.start()
            self.wait(lambda: self.queue.status()["state"] == "faulted")
            self.assertTrue(self.queue.status()["alive"])
            self.assertFalse(self.queue.status()["healthy"])
            self.assertEqual(claim.call_count, 3)
            self.assertEqual(self.queue.status()["consecutive_failures"], 3)
        self.release.set()
        key = self.enqueue(["ppocr"])[0]
        self.queue.recover_worker()
        self.wait(lambda: self.store.one("tasks", key)["status"] == "succeeded")
        self.assertTrue(self.queue.status()["healthy"])

    def test_claim_commit_then_error_interrupts_owned_task_without_reexecuting(self):
        self.queue.stop()
        claim = self.store.claim
        claimed = []

        def fail_after_commit():
            task = claim()
            if task and not claimed:
                claimed.append(task["id"])
                raise sqlite3.OperationalError("database is locked")
            return task

        self.release.set()
        with patch.object(self.store, "claim", fail_after_commit):
            self.queue.start()
            key = self.enqueue(["ppocr"])[0]
            self.wait(lambda: self.store.one("tasks", key)["status"] == "interrupted")
        self.assertEqual(self.factories, [])
        self.queue.action(self.project, "resume", [key])
        self.wait(lambda: self.store.one("tasks", key)["status"] == "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 1)

    def test_unload_failure_retains_owner_and_does_not_duplicate_completed_result(self):
        unload = self.queue.factory.unload
        attempts = []

        def fail_once(adapter):
            attempts.append(adapter)
            if len(attempts) == 1:
                raise OSError("temporary unload error")
            return unload(adapter)

        self.release.set()
        with patch.object(self.queue.factory, "unload", fail_once):
            key = self.enqueue(["ppocr"])[0]
            self.wait(lambda: len(attempts) >= 2)
            self.wait(
                lambda: self.queue.status()["healthy"] and self.queue.adapter is None
            )
        self.assertIs(attempts[0], attempts[1])
        self.assertEqual(self.store.one("tasks", key)["status"], "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 1)
        self.assertEqual(self.factories, ["ppocr"])

    def test_failure_status_write_error_is_reconciled_and_batch_can_continue(self):
        transaction = self.store.transaction
        failed = []

        class FaultyWrite:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, args=()):
                is_failure = "status='failed'" in sql or (
                    "UPDATE tasks SET status=?" in sql and args and args[0] == "failed"
                )
                if is_failure and not failed:
                    failed.append(True)
                    raise sqlite3.OperationalError("database is locked")
                return self.db.execute(sql, args)

        @contextmanager
        def fault():
            with transaction() as db:
                yield FaultyWrite(db)

        self.failure = "simulated model error"
        self.release.set()
        with patch.object(self.store, "transaction", fault):
            first, second = self.enqueue(["glm", "ppocr"])
            self.wait(lambda: self.store.one("tasks", first)["status"] == "interrupted")
            self.wait(lambda: self.store.one("tasks", second)["status"] == "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 1)
        self.assertTrue(self.queue.status()["healthy"])
        self.queue.action(self.project, "resume", [first])
        self.wait(lambda: self.store.one("tasks", first)["status"] == "succeeded")
        self.assertEqual(len(self.store.rows("SELECT * FROM results")), 2)
