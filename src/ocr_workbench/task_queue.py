"""Durable queue, one owning thread and one GPU model session at a time."""

import threading
import time
from ocr_workbench.adapter import EngineAdapter, Cancelled
from ocr_workbench.store import now


class TaskQueue:
    def __init__(self, store, bundle, adapter_factory=EngineAdapter):
        self.store, self.bundle, self.factory = store, bundle, adapter_factory
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.RLock()
        self.adapter = None
        self.current = None
        self.cancel_event = threading.Event()
        self.thread = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(
            target=self.run, name="OCR GPU queue", daemon=False
        )
        self.thread.start()

    def status(self):
        with self.guard:
            return {
                "task_id": self.current,
                "engine": self.adapter.engine if self.adapter else None,
                "loaded": bool(
                    self.adapter
                    and self.adapter.ready
                    and self.adapter.process
                    and self.adapter.process.poll() is None
                ),
            }

    def unload(self):
        with self.guard:
            adapter = self.adapter
            self.adapter = None
        if adapter:
            adapter.unload()

    def run(self):
        try:
            while not self.stopping.is_set():
                task = self.store.claim()
                if task is None:
                    self.unload()
                    self.wake.wait(0.3)
                    self.wake.clear()
                    continue
                with self.guard:
                    self.current = task["id"]
                    self.cancel_event = threading.Event()
                    if self.stopping.is_set():
                        self.cancel_event.set()
                    if self.store.one("tasks", task["id"])["status"] != "running":
                        self.current = None
                        continue
                try:
                    if self.adapter and self.adapter.engine != task["engine"]:
                        self.unload()
                    if self.adapter is None:
                        with self.guard:
                            self.adapter = self.factory(
                                self.bundle,
                                task["engine"],
                                self.store.root / "sessions",
                            )
                            self.adapter.cancelled = self.cancel_event
                        self.adapter.load()
                    else:
                        self.adapter.cancelled = self.cancel_event
                    if self.cancel_event.is_set() or self.stopping.is_set():
                        raise Cancelled()
                    with self.store.transaction() as db:
                        if (
                            db.execute(
                                "UPDATE tasks SET phase=? WHERE id=? AND status='running'",
                                (
                                    (
                                        "去弯曲中"
                                        if task.get("kind") == "dewarp"
                                        else "识别中"
                                    ),
                                    task["id"],
                                ),
                            ).rowcount
                            != 1
                        ):
                            raise Cancelled()
                    version = self.store.one("versions", task["version_id"])
                    output = (
                        self.store.root
                        / "task-results"
                        / task["id"]
                        / str(time.time_ns())
                    )
                    output.mkdir(parents=True)
                    data = self.adapter.recognize(
                        self.store.file(version["path"]), output
                    )
                    if task.get("kind") == "dewarp":
                        from ocr_workbench.imaging import save_version
                        from PIL import Image

                        with Image.open(data["image"]) as image:
                            save_version(
                                self.store,
                                version,
                                image,
                                {
                                    "kind": "dewarp",
                                    "model": data["model"],
                                    "revision": data["revision"],
                                },
                                task_id=task["id"],
                            )
                    else:
                        data["project_image_version"] = version["id"]
                        data["version_operations"] = version["operations"]
                        self.store.complete(task["id"], data)
                except Cancelled:
                    with self.store.transaction() as db:
                        db.execute(
                            "UPDATE tasks SET status=?,phase=?,finished=? WHERE id=? AND status='running'",
                            (
                                (
                                    "interrupted"
                                    if self.stopping.is_set()
                                    else "cancelled"
                                ),
                                "等待继续" if self.stopping.is_set() else "已取消",
                                now(),
                                task["id"],
                            ),
                        )
                    self.unload()
                except Exception as error:
                    with self.store.transaction() as db:
                        db.execute(
                            "UPDATE tasks SET status='failed',phase='识别失败',error=?,finished=? WHERE id=? AND status='running'",
                            (str(error), now(), task["id"]),
                        )
                    self.unload()
                finally:
                    with self.guard:
                        self.current = None
        finally:
            self.unload()

    def action(self, project_id, action, task_ids=None):
        self.store.one("projects", project_id)
        transitions = {
            "pause": ("paused", ("queued",)),
            "resume": ("queued", ("paused", "interrupted")),
            "retry": ("queued", ("failed", "cancelled")),
            "cancel": ("cancelled", ("queued", "paused", "interrupted", "running")),
        }
        if action not in transitions:
            raise ValueError("未知队列操作")
        target, sources = transitions[action]
        with self.store.transaction() as db:
            tasks = db.execute(
                "SELECT id,status FROM tasks WHERE project_id=?", (project_id,)
            ).fetchall()
            owned = {t["id"] for t in tasks}
            if task_ids is not None and not set(task_ids) <= owned:
                raise ValueError("任务不属于该项目")
            affected = [
                t["id"]
                for t in tasks
                if t["status"] in sources and (task_ids is None or t["id"] in task_ids)
            ]
            for key in affected:
                db.execute(
                    "UPDATE tasks SET status=?,phase=?,error=NULL WHERE id=?",
                    (
                        target,
                        {
                            "paused": "已暂停",
                            "queued": "等待识别",
                            "cancelled": "已取消",
                        }[target],
                        key,
                    ),
                )
        with self.guard:
            if action == "cancel" and self.current in affected:
                self.cancel_event.set()
        self.wake.set()
        return affected

    def stop(self):
        self.stopping.set()
        with self.guard:
            self.cancel_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=35)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("GPU 工作线程未能退出")
