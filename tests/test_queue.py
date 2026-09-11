"""Deterministic queue race tests, complementing real GPU application acceptance."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
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
                        raise Cancelled()
                if owner.failure:
                    failure, owner.failure = owner.failure, None
                    raise RuntimeError(failure)

            def recognize(self, image, output):
                if self.cancelled.is_set():
                    raise Cancelled()
                return {"text": "actual test state", "tables": [], "blocks": []}

            def unload(self):
                owner.unloaded.append(self.engine)

        self.queue = TaskQueue(self.store, Path(self.temp.name), Fake)
        self.queue.start()

    def tearDown(self):
        self.release.set()
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
