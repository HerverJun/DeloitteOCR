"""Durable queue, one owning thread and one GPU model session at a time."""

from contextlib import contextmanager
import threading
import time
import logging
import sqlite3
from ocr_workbench.adapter import EngineAdapter, Cancelled
from ocr_workbench.store import now


class TaskQueue:
    def __init__(self, store, bundle, adapter_factory=EngineAdapter, registry=None):
        self.store, self.bundle, self.factory = store, bundle, adapter_factory
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.RLock()
        self.gpu_guard = threading.RLock()
        self.adapter = None
        self.current = None
        self.cancel_event = threading.Event()
        self.thread = None
        self.registry = registry
        self.started = False
        self.state = "stopped"
        self.last_error = None
        self.consecutive_failures = 0
        self.retry_at = None
        self.recovery_task = None
        self.needs_reconcile = False
        self.recovery_requested = threading.Event()
        self.retry_delays = (0.3, 0.6, 1.2, 2.4, 5.0)
        self.fusion_only = False
        self.external_only = False
        self.review_factory = None

    def start(self):
        with self.guard:
            if self.thread and self.thread.is_alive():
                return
            self.unload()
            self.store.recover(fusion=self.fusion_only, external=self.external_only)
            self.stopping.clear()
            self.recovery_requested.clear()
            self.consecutive_failures = 0
            self.retry_at = None
            self.started = True
            self.state = "running"
            self.thread = threading.Thread(
                target=self.run, name="External review queue" if self.external_only else "Fusion CPU queue" if self.fusion_only else "OCR GPU queue", daemon=False
            )
            self.thread.start()

    def status(self):
        with self.guard:
            alive = bool(self.thread and self.thread.is_alive())
            return {
                "alive": alive,
                "healthy": alive and self.state == "running",
                "state": self.state if alive or not self.started else "stopped",
                "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures,
                "retry_at": self.retry_at,
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
        if adapter:
            adapter.unload()
            with self.guard:
                if self.adapter is adapter:
                    self.adapter = None

    @contextmanager
    def engine_maintenance(self):
        if not self.gpu_guard.acquire(timeout=0.1):
            raise ValueError(
                "识别任务正在运行，请暂停队列并等待当前项完成后再启用引擎包"
            )
        try:
            self.unload()
            yield
        finally:
            self.gpu_guard.release()
            self.wake.set()

    def run(self):
        try:
            while not self.stopping.is_set():
                if self.state == "faulted":
                    if not self.recovery_requested.wait(0.3):
                        continue
                    self.recovery_requested.clear()
                    self.consecutive_failures = 0
                try:
                    with self.gpu_guard:
                        if self.stopping.is_set():
                            break
                        if self.consecutive_failures or self.state == "faulted":
                            self.unload()
                        if self.needs_reconcile:
                            # Never rerun a possibly committed result. An unfinished
                            # claimed task requires explicit resume after recovery.
                            with self.store.transaction() as db:
                                db.execute(
                                    "UPDATE tasks SET status='interrupted',phase='等待继续',error=?,finished=? WHERE status='running' AND " + self.store.queue_scope(self.fusion_only, self.external_only),
                                    ("队列存储异常中断，请检查后继续", now()),
                                )
                            self.recovery_task = None
                            self.needs_reconcile = False
                        worked = self.step()
                    with self.guard:
                        self.state = "running"
                        self.consecutive_failures = 0
                        self.retry_at = None
                except Exception as error:
                    logging.getLogger(__name__).exception("OCR queue failed")
                    recoverable = isinstance(error, OSError) or (
                        isinstance(error, sqlite3.OperationalError)
                        and any(
                            word in str(error).lower()
                            for word in ("locked", "busy", "disk", "i/o")
                        )
                    )
                    with self.guard:
                        self.current = None
                        self.needs_reconcile = True
                        self.consecutive_failures += 1
                        self.last_error = {
                            "message": str(error),
                            "type": type(error).__name__,
                            "time": now(),
                            "recoverable": recoverable,
                        }
                        retry = recoverable and self.consecutive_failures <= len(
                            self.retry_delays
                        )
                        self.state = "recovering" if retry else "faulted"
                        delay = (
                            self.retry_delays[self.consecutive_failures - 1]
                            if retry
                            else 0
                        )
                        self.retry_at = time.time() + delay if retry else None
                    if retry:
                        self.stopping.wait(delay)
                    continue
                if not worked:
                    self.wake.wait(0.3)
                    self.wake.clear()
        finally:
            try:
                self.unload()
            except Exception as error:
                logging.getLogger(__name__).exception("OCR queue unload failed")
                self.last_error = {
                    "message": str(error),
                    "type": type(error).__name__,
                    "time": now(),
                    "recoverable": False,
                }
            self.state = "stopped"

    def recover_worker(self):
        """Explicitly retry the suspended worker after its cause is addressed."""
        with self.guard:
            if self.state == "faulted":
                self.recovery_requested.set()
        self.wake.set()
        return self.status()

    def step(self):
        task = self.store.claim(external=True) if self.external_only else self.store.claim()
        if task is None:
            self.unload()
            return False
        with self.guard:
            self.current = task["id"]
            self.recovery_task = task["id"]
            self.cancel_event = threading.Event()
            if self.stopping.is_set():
                self.cancel_event.set()
            if self.store.one("tasks", task["id"])["status"] != "running":
                self.current = None
                self.recovery_task = None
                return True
        try:
            if task.get("kind") == "multimodal":
                self._run_multimodal(task)
                return True
            from ocr_workbench.imaging import prepare_task
            geometry_provider, shared_context = 'paddle', None

            prepared = prepare_task(self.store, task)
            if prepared is None:
                raise Cancelled()
            task = prepared
            if task.get('kind') == 'geometry':
                import json
                import hashlib
                from ocr_workbench.geometry import complete_geometry
                request_row = self.store.rows('SELECT snapshot FROM geometry_requests WHERE task_id=?', (task['id'],))[0]
                geometry_snapshot = json.loads(request_row['snapshot'])
                geometry_provider = geometry_snapshot.get('geometry_provider', 'paddle')
                candidate = geometry_snapshot.get('candidate_artifact')
                if candidate:
                    artifact = self.store.file(candidate['path'])
                    if artifact.exists() and hashlib.sha256(artifact.read_bytes()).hexdigest() == candidate['sha256']:
                        if self.cancel_event.is_set() or self.stopping.is_set():
                            raise Cancelled()
                        complete_geometry(self.store, task, json.loads(artifact.read_text('utf-8')), artifact, candidate_cache_hit=True)
                        return True
            resolved = (
                self.registry.resolve(
                    task["engine"], task.get("engine_package", "builtin")
                )
                if self.registry and task.get('kind') != 'geometry'
                else self.bundle
            )
            if task.get('kind') == 'geometry' and geometry_provider != 'paddle':
                regions = json.loads(self.store.rows('SELECT regions FROM geometry_requests WHERE task_id=?', (task['id'],))[0]['regions'])
                if not geometry_snapshot.get('ocr_blocks') or not regions:
                    self.unload()
                    with self.guard:
                        self.adapter = self.factory(resolved, 'geometry', self.store.root / 'sessions')
                        self.adapter.configure_geometry('context')
                        self.adapter.cancelled = self.cancel_event
                    self.adapter.load()
                    context_output = self.store.root / 'task-results' / task['id'] / ('context-' + str(time.time_ns()))
                    context_output.mkdir(parents=True)
                    version = self.store.one('versions', task['version_id'])
                    shared_context = self.adapter.recognize(self.store.file(version['path']), context_output,
                        {**geometry_snapshot, 'regions': regions})
                    self.unload()
            if self.adapter and (
                self.adapter.engine != task["engine"]
                or getattr(self.adapter, "bundle", self.bundle) != resolved
                or task.get('kind') == 'geometry' and getattr(self.adapter, 'geometry_provider', 'paddle') != geometry_provider
            ):
                self.unload()
            if self.adapter is None:
                with self.guard:
                    self.adapter = self.factory(
                        resolved,
                        task["engine"],
                        self.store.root / "sessions",
                    )
                    self.adapter.cancelled = self.cancel_event
                    if task.get('kind') == 'geometry' and hasattr(self.adapter, 'configure_geometry'):
                        self.adapter.configure_geometry(geometry_provider)
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
                            ("去弯曲中" if task.get("kind") == "dewarp" else "补充表格定位" if task.get('kind') == 'geometry' else "识别中"),
                            task["id"],
                        ),
                    ).rowcount
                    != 1
                ):
                    raise Cancelled()
            version = self.store.one("versions", task["version_id"])
            output = self.store.root / "task-results" / task["id"] / str(time.time_ns())
            output.mkdir(parents=True)
            input_image = self.store.file(version["path"])
            if task.get('kind') == 'region_ocr':
                import json
                from PIL import Image
                crop = self.store.rows('SELECT crop_box FROM page_ocr_inputs WHERE task_id=?', (task['id'],))[0]
                with Image.open(input_image) as source:
                    cropped = source.crop(json.loads(crop['crop_box']))
                    input_image = output / 'region-input.png'
                    cropped.save(input_image)
            if task.get('kind') == 'geometry':
                import json
                geometry_request = self.store.rows('SELECT snapshot,regions FROM geometry_requests WHERE task_id=?', (task['id'],))[0]
                snapshot = json.loads(geometry_request['snapshot'])
                request = {**snapshot, 'regions': json.loads(geometry_request['regions'])}
                if shared_context:
                    request.update({key: shared_context[key] for key in ('regions', 'ocr_blocks', 'ocr_source')})
                data = self.adapter.recognize(input_image, output, request)
                if shared_context:
                    data['context_seconds'] = shared_context['inference_seconds']
                    from ocr_workbench.atomic_files import write_json
                    write_json(output / 'geometry.json', data, durable=True)
            elif task.get('kind') == 'region_ocr':
                data = self.adapter.recognize(input_image, output, allow_empty=True)
            else:
                data = self.adapter.recognize(input_image, output)
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
            elif task.get('kind') == 'geometry':
                from ocr_workbench.geometry import complete_geometry
                complete_geometry(self.store, task, data, output / 'geometry.json')
            elif task.get('kind') == 'region_ocr':
                from ocr_workbench.page_processing import complete_region_task
                complete_region_task(self.store, task, data)
            else:
                data["project_image_version"] = version["id"]
                data["version_operations"] = version["operations"]
                data["engine_package"] = {
                    "id": task.get("engine_package", "builtin"),
                    "bundle": str(resolved),
                }
                self.store.complete(task["id"], data)
        except Cancelled:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE tasks SET status=?,phase=?,finished=? WHERE id=? AND status='running'",
                    (
                        ("interrupted" if self.stopping.is_set() else "cancelled"),
                        "等待继续" if self.stopping.is_set() else "已取消",
                        now(),
                        task["id"],
                    ),
                )
            self.unload()
        except Exception as error:
            from ocr_workbench.errors import friendly_engine_error

            with self.store.transaction() as db:
                db.execute(
                    "UPDATE tasks SET status='failed',phase=?,error=?,finished=? WHERE id=? AND status='running'",
                    ("审校失败" if task.get("kind") == "multimodal" else "识别失败",
                     friendly_engine_error(error), now(), task["id"]),
                )
            self.unload()
        finally:
            with self.guard:
                self.current = None
                self.recovery_task = None
        return True

    def _run_multimodal(self, task):
        """Run shared review logic under the selected worker's ownership."""
        from ocr_workbench.multimodal_store import prepare_review, complete_review
        from ocr_workbench.multimodal_runtime import ReviewSession, ReviewCancelled
        from ocr_workbench.atomic_files import write_json

        self.unload()
        snapshot = prepare_review(self.store, task["id"])
        output = self.store.root / "task-results" / task["id"] / str(time.time_ns())
        output.mkdir(parents=True)

        def progress(done, total):
            if self.cancel_event.is_set() or self.stopping.is_set():
                raise Cancelled()
            with self.store.transaction() as db:
                changed = db.execute(
                    "UPDATE tasks SET phase=? WHERE id=? AND status='running'",
                    (f"视觉审校 {done}/{total}", task["id"]),
                ).rowcount
            if changed != 1:
                raise Cancelled()

        progress(0, len(snapshot["targets"]))
        if snapshot.get('review_kind') == 'structure':
            from ocr_workbench.structure_arbitration import begin_dispatch
            begin_dispatch(self.store, task['id'])
        factory = self.review_factory or ReviewSession
        if snapshot['config'].get('backend') == 'external':
            from ocr_workbench.agent.visual import check_external_job_authorization
            check_external_job_authorization(self.store, task['id'], snapshot)
        try:
            with factory(self.bundle, snapshot["config"], output, self.cancel_event) as session:
                if snapshot['config'].get('backend') == 'external' and hasattr(session, 'check_connection'):
                    original_check = session.check_connection
                    def check_dispatch():
                        if original_check:
                            original_check()
                        check_external_job_authorization(self.store, task['id'], snapshot)
                    session.check_connection = check_dispatch
                response = session.review(
                    self.store.file(snapshot["image_relative_path"]), snapshot,
                    progress_callback=progress,
                )
        except ReviewCancelled as error:
            raise Cancelled() from error
        if self.cancel_event.is_set() or self.stopping.is_set():
            raise Cancelled()
        write_json(output / "review.json", response, durable=True)
        response["artifact"] = str((output / "review.json").relative_to(self.store.root))
        complete_review(self.store, task["id"], response)

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
                "SELECT id,status FROM tasks WHERE project_id=? AND " + self.store.queue_scope(self.fusion_only, self.external_only), (project_id,)
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
        if action in {"resume", "retry"}:
            self.recover_worker()
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


class ExternalReviewQueue(TaskQueue):
    """One network task at a time, with no engine process or GPU ownership."""
    def __init__(self, store, bundle, connection):
        super().__init__(store, bundle)
        self.external_only = True
        self.connection = connection
        self.review_factory = connection.session

    def unload(self):
        return None

    def action(self, project_id, action, task_ids=None):
        if action in {'retry', 'resume'}:
            from ocr_workbench.multimodal_store import prepare_review
            rows = self.store.rows("SELECT t.id,t.status FROM tasks t JOIN multimodal_requests m ON m.task_id=t.id WHERE t.project_id=? AND m.backend='external'", (project_id,))
            wanted = {'failed', 'cancelled'} if action == 'retry' else {'paused', 'interrupted'}
            for row in rows:
                if row['status'] in wanted and (task_ids is None or row['id'] in task_ids):
                    snapshot = prepare_review(self.store, row['id'], require_running=False)
                    config, _ = self.connection.resolve(snapshot['model_id'])
                    if config != snapshot['config']:
                        raise ValueError('外部审校配置已变化，请重新提交')
        return super().action(project_id, action, task_ids)


class FusionQueue(TaskQueue):
    """A separate CPU worker; no preprocessing, adapter factory or GPU lock."""

    def __init__(self, store, bundle):
        super().__init__(store, bundle)
        self.fusion_only = True

    def unload(self):
        return None

    def step(self):
        task = self.store.claim(fusion=True)
        if task is None:
            return False
        with self.guard:
            self.current = self.recovery_task = task["id"]
            self.cancel_event = threading.Event()
        last_check = 0.0
        cancelled_state = False

        def cancelled():
            nonlocal last_check, cancelled_state
            if self.cancel_event.is_set() or self.stopping.is_set():
                return True
            if time.monotonic()-last_check >= .1:
                last_check = time.monotonic()
                cancelled_state = self.store.one("tasks", task["id"])["status"] != "running"
            return cancelled_state

        try:
            if cancelled():
                raise Cancelled()
            self.store.run_fusion(task, cancelled)
        except Cancelled:
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status=?,phase=?,finished=? WHERE id=? AND status='running'",
                           ("interrupted" if self.stopping.is_set() else "cancelled",
                            "等待继续" if self.stopping.is_set() else "已取消", now(), task["id"]))
        except (OSError, sqlite3.OperationalError):
            raise
        except Exception as error:
            logging.getLogger(__name__).exception("CPU fusion failed")
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status='failed',phase='融合失败',error=?,finished=? WHERE id=? AND status='running'",
                           (str(error), now(), task["id"]))
        finally:
            with self.guard:
                self.current = None
                self.recovery_task = None
        return True
