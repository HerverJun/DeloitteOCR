"""Authenticated loopback application service and local static UI."""

import argparse
import logging
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from ocr_workbench import __version__
from ocr_workbench.store import Store, Conflict, uid, now, encoded
from ocr_workbench.task_queue import TaskQueue, FusionQueue, ExternalReviewQueue
from ocr_workbench.imaging import add_image, transform, thumbnail
from ocr_workbench.exporting import build_export
from ocr_workbench.editing import present_result, export_text
from ocr_workbench.platform_navigation import NavigationTickets


def create_app(bundle, data, token, *, start_queue=True, review_only=False, agent_enabled=None):
    bundle = Path(bundle).resolve()
    store = Store(data)
    from ocr_workbench.engine_packages import EnginePackages

    exports = store.root / "exports"
    if exports.exists():
        for folder in exports.glob("export-*"):
            if folder.is_dir() and not folder.is_symlink() and not folder.is_junction():
                shutil.rmtree(folder)
    registry = EnginePackages(bundle, store.root)
    queue = TaskQueue(store, bundle, registry=registry)
    fusion_queue = FusionQueue(store, bundle)
    from ocr_workbench.external_review import ExternalConnection
    external_connection = ExternalConnection(store)
    external_queue = ExternalReviewQueue(store, bundle, external_connection)
    from ocr_workbench.maintenance import ProjectMaintenance

    maintenance = ProjectMaintenance(store, queue, fusion_queue, external_queue)
    from ocr_workbench.documents import Documents
    documents = Documents(store, bundle, queue, review_only=review_only)
    agent_policy_path = bundle / "config/agent-policy.json"
    agent_policy = json.loads(agent_policy_path.read_text("utf-8")) if agent_policy_path.is_file() else {"feature_flags": {"agent_enabled": False}, "limits": {}}
    enable_agent = bool(agent_policy.get("feature_flags", {}).get("agent_enabled", False)) if agent_enabled is None else agent_enabled

    def import_one(key, name, temporary, *, on_accept=None):
        with maintenance.guard:
            return add_image(store, key, name, temporary, on_accept=on_accept)

    @asynccontextmanager
    async def lifespan(app):
        if enable_agent:
            try:
                from ocr_workbench.agent.runtime import AgentRuntime
                manager = AgentRuntime(services, policy=agent_policy, connection=app.state.agent_connection)
                await manager.open()
                app.state.agent_runtime = manager
            except (ImportError, ValueError, sqlite3.DatabaseError):
                app.state.agent_startup_error = "助手依赖或检查点不可用；请核对安装与诊断，普通工作台仍可使用"
        if start_queue and not review_only:
            queue.start()
        if start_queue:
            fusion_queue.start()
            external_queue.start()
            documents.start()
        try:
            yield
        finally:
            if app.state.agent_runtime is not None:
                await app.state.agent_runtime.close()
            if start_queue:
                documents.stop()
                external_queue.stop()
            if start_queue and not review_only:
                queue.stop()
            if start_queue:
                fusion_queue.stop()

    app = FastAPI(
        title="DeloitteOCR · 离线 OCR 工作台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.queue = queue
    app.state.fusion_queue = fusion_queue
    app.state.external_queue = external_queue
    app.state.external_connection = external_connection
    app.state.start_queue = start_queue
    app.state.documents = documents
    app.state.shutdown = lambda: None
    app.state.review_only = review_only
    platform_instance = os.environ.get("WORKBENCH_INSTANCE_ID", "")
    platform_nonce = os.environ.get("WORKBENCH_NONCE", "")
    navigation = (NavigationTickets(instance_id=platform_instance)
                  if os.environ.get("WORKBENCH_APP_ID") == "ocr" and platform_instance
                  and len(platform_nonce) >= 32 else None)
    app.state.platform_navigation = navigation

    def require_recognition():
        if review_only:
            raise ValueError("当前为仅校对与导出模式，请在完整模式下使用识别与引擎启用功能")
        from ocr_workbench.platform_resources import platform_mode, ResourceUnavailable
        try:
            platform_mode()
        except ResourceUnavailable:
            raise ValueError("平台 GPU 协调器不可用，暂不能提交识别任务") from None
        if start_queue and not queue.status().get("healthy", False):
            raise ValueError("队列正在恢复，请查看队列状态，恢复后再提交任务")

    from ocr_workbench.application_services import ApplicationServices
    services = ApplicationServices(store, documents, maintenance, queue=queue, fusion_queue=fusion_queue,
                                   registry=registry, bundle=bundle, require_recognition=require_recognition,
                                   external_connection=external_connection, external_queue=external_queue, queues_started=start_queue)
    app.state.application_services = services
    from ocr_workbench.agent.routes import register_agent_routes
    register_agent_routes(app, services, enabled=enable_agent, policy=agent_policy)
    from ocr_workbench.document_routes import register_document_routes
    register_document_routes(app, documents, maintenance, services)
    from ocr_workbench.structure_routes import register_structure_routes
    register_structure_routes(app, store)
    from ocr_workbench.multimodal_routes import register_multimodal_routes
    register_multimodal_routes(app, store, bundle, queue, maintenance, require_recognition)
    from ocr_workbench.platform_imports import register_platform_import_routes
    register_platform_import_routes(app, store, import_one)

    @app.middleware("http")
    async def local_auth(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"message": "只允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"message": "不允许跨站请求"}, status_code=403)
        issue_path = request.url.path == "/api/platform/navigation/issue" and navigation is not None
        identity_path = request.url.path == "/api/platform/v1/identity" and navigation is not None
        if (request.url.path.startswith("/api/") and request.url.path != "/api/health"
                and not issue_path and not identity_path):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse(
                    {"message": "会话已失效，请从托盘重新打开工作台"}, status_code=401
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"message": str(error).strip("'")}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse(
            {"message": str(error)},
            status_code=409 if isinstance(error, Conflict) else 400,
        )

    @app.exception_handler(Exception)
    async def failure(request, error):
        import traceback

        with (store.root / "service-errors.log").open("a", encoding="utf-8") as log:
            log.write(traceback.format_exc() + "\n")
        return JSONResponse(
            {"message": "操作未完成，请重试或在托盘查看日志"}, status_code=500
        )

    @app.get("/api/platform/v1/identity")
    def platform_identity():
        if navigation is None or not os.environ.get("WORKBENCH_LAUNCH_ID"):
            raise HTTPException(status_code=404, detail="Platform identity unavailable")
        return {"app_id": "ocr", "instance_id": platform_instance,
                "nonce": platform_nonce, "version": __version__}

    @app.get("/api/health")
    def health():
        from ocr_workbench.platform_resources import platform_mode, ResourceUnavailable
        try:
            resource_mode = "platform_shared" if platform_mode() else "legacy_single_app"
        except ResourceUnavailable:
            resource_mode = "unavailable"
        status = queue.status()
        cpu_status = fusion_queue.status()
        ready = (review_only or status.get("healthy", False)) and (not start_queue or
            (cpu_status.get("healthy", False) and external_queue.status()['healthy']))
        ready = ready and (review_only or resource_mode != "unavailable")
        return {"status": "ready" if ready else "degraded", "version": __version__,
                "review_only": review_only, "gpu_resource_mode": resource_mode,
                "queue": status, "fusion_queue": cpu_status,
                "external_queue": external_queue.status()}

    @app.post("/api/platform/navigation/issue", status_code=201)
    async def issue_platform_navigation(request: Request):
        if navigation is None or not secrets.compare_digest(
                request.headers.get("x-workbench-launch-nonce", ""), platform_nonce):
            raise HTTPException(status_code=403, detail="Navigation unavailable")
        if request.headers.get("content-type", "").split(";", 1)[0].lower().strip() != "application/json":
            raise HTTPException(status_code=400, detail="JSON required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                raise HTTPException(status_code=400, detail="Invalid navigation context")
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate field")
                    result[key] = value
                return result
            ticket = navigation.issue(json.loads(body, object_pairs_hook=unique))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid navigation context") from None
        return {"ticket": ticket}

    @app.get("/api/platform/navigation/tickets/{ticket}")
    def consume_platform_navigation(ticket: str):
        if navigation is None:
            raise HTTPException(status_code=404, detail="Navigation unavailable")
        try:
            target = navigation.consume(ticket)
        except KeyError:
            raise HTTPException(status_code=404, detail="Navigation unavailable") from None
        return {"app_id": target.app_id, "instance_id": target.instance_id,
                "link_id": target.link_id, "workspace_id": target.workspace_id,
                "return_url": target.return_url}

    @app.post('/api/multimodal/external/queue/recover')
    def recover_external_queue():
        if not start_queue:
            raise ValueError('此服务未启用外部审校队列')
        if not external_queue.status()['alive']:
            external_queue.start()
        return external_queue.recover_worker()

    @app.post("/api/fusion/queue/recover")
    def recover_fusion_queue():
        if not start_queue:
            raise ValueError("此服务未启用 CPU 融合队列")
        if not fusion_queue.status().get("alive"):
            fusion_queue.start()
        return fusion_queue.recover_worker()

    @app.post("/api/queue/recover")
    def recover_queue():
        if review_only:
            raise ValueError("仅校对模式不启动识别队列")
        if not start_queue:
            raise ValueError("此服务未启用识别队列")
        if not queue.status().get("alive"):
            queue.start()
        return queue.recover_worker()

    @app.get("/api/state")
    def state():
        return {
            "projects": store.rows("SELECT * FROM projects ORDER BY updated DESC"),
            "engines": registry.engines(),
            "queue": queue.status(),
            "fusion_queue": fusion_queue.status(),
            "external_queue": external_queue.status(),
            "document_queue": documents.status(),
            "data_directory": str(store.root),
            "review_only": review_only,
            "disk": maintenance.disk_status(),
        }

    @app.post("/api/projects")
    def create_project(body: dict):
        return store.project(body.get("name", ""))

    @app.patch("/api/projects/{key}")
    def rename_project(key: str, body: dict):
        store.one("projects", key)
        name = body.get("name", "").strip()
        if not name or len(name) > 120:
            raise ValueError("请输入有效项目名称")
        with store.transaction() as db:
            db.execute(
                "UPDATE projects SET name=?,updated=? WHERE id=?", (name, now(), key)
            )
        return store.one("projects", key)

    @app.get("/api/projects/{key}")
    def project(key: str, since_revision: int | None = None):
        snapshot = store.project_snapshot(key, since_revision)
        snapshot["queue"] = queue.status()
        snapshot["fusion_queue"] = fusion_queue.status()
        snapshot["external_queue"] = external_queue.status()
        snapshot["disk"] = maintenance.disk_status()
        return snapshot

    @app.get("/api/maintenance/orphans")
    def orphan_files():
        return maintenance.orphans()

    @app.post("/api/maintenance/orphans/quarantine")
    def quarantine_orphan_files(body: dict):
        return maintenance.quarantine_orphans(body.get("paths", []))

    @app.get("/api/projects/{key}/storage")
    def project_storage(key: str):
        return maintenance.usage(key)

    @app.delete("/api/projects/{key}")
    async def delete_project(key: str, body: dict):
        manager = app.state.agent_runtime
        if manager is not None and not manager.closed:
            return await manager.delete_project(key, body.get("confirmation"))
        return await run_in_threadpool(maintenance.delete_without_runtime, key, body.get("confirmation"))

    @app.post("/api/projects/{key}/images")
    async def import_images(key: str, files: list[UploadFile] = File(...)):
        store.one("projects", key)
        if not files or len(files) > 1000:
            raise ValueError("每次最多导入 1000 张图片")
        imported = []
        errors = []
        inbox = store.root / "inbox"
        inbox.mkdir(exist_ok=True)
        for upload in files:
            temporary = inbox / uid()
            try:
                count = 0
                with temporary.open("wb") as target:
                    while chunk := await upload.read(1024 * 1024):
                        count += len(chunk)
                        if count > 128 * 1024 * 1024:
                            raise ValueError("单张图片超过 128 MB")
                        target.write(chunk)
                name = (upload.filename or "图片.png").replace("\\", "/").split("/")[-1]
                imported.append(
                    await run_in_threadpool(import_one, key, name, temporary)
                )
            except Exception as error:
                errors.append({"name": upload.filename, "message": str(error)})
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).exception("Import inbox cleanup failed: %s", temporary.name)
                await upload.close()
        return {"images": imported, "errors": errors}

    @app.get("/api/versions/{key}/image")
    def image(key: str):
        version = store.one("versions", key)
        return FileResponse(store.file(version["path"]), media_type="image/png")

    @app.post("/api/versions/{key}/transform")
    def transform_image(key: str, body: dict):
        if body.get("kind") == "dewarp":
            require_recognition()
            task_id = store.enqueue_dewarp(key)
            queue.wake.set()
            return {"task_id": task_id, "queued": True}
        with maintenance.guard:
            return transform(store, key, body)

    @app.get("/api/versions/{key}/thumbnail")
    def image_thumbnail(key: str):
        with maintenance.guard:
            return FileResponse(thumbnail(store, key), media_type="image/jpeg")

    @app.put("/api/images/{key}/version")
    def select_version(key: str, body: dict):
        store.one("images", key)
        version = store.one("versions", body.get("version_id", ""))
        if version["image_id"] != key:
            raise ValueError("版本不属于当前图片")
        with store.transaction() as db:
            db.execute(
                "UPDATE images SET active_version=? WHERE id=?", (version["id"], key)
            )
        return {"saved": True}

    @app.post("/api/projects/{key}/tasks")
    def create_tasks(key: str, body: dict):
        return services.submit_ocr(key, body)

    @app.get("/api/fusion/policies")
    def fusion_policies():
        from ocr_workbench.fusion import load_policy
        return {kind: {mode: load_policy(bundle, kind, mode) for mode in ("conservative", "aggressive")}
                for kind in ("table", "print", "handwriting")}

    @app.post("/api/projects/{key}/fusion")
    def create_fusion(key: str, body: dict):
        from ocr_workbench.fusion import load_policy
        policy = load_policy(bundle, body.get("content_type", "table"), body.get("mode", "conservative"))
        ids = store.enqueue_fusion(key, body.get("result_ids", []), policy, body.get("request_id"), body.get("expected_engines"))
        fusion_queue.wake.set()
        return {"task_ids": ids}

    @app.get("/api/results/{key}/issues")
    def issues(key: str, state: str | None = None, category: str | None = None, offset: int = 0, limit: int = 50, resume: bool = False):
        return store.review_issues(key, state, category, offset, limit, resume)

    @app.put("/api/results/{key}/issues/position")
    def issue_position(key: str, body: dict):
        return store.set_fusion_position(key, body.get("issue_id"))

    @app.post("/api/results/{key}/issues/{issue_id}/decision")
    def decide(key: str, issue_id: str, body: dict):
        return present_result(store.decide_issue(key, issue_id, body))

    @app.get("/api/results/{key}/evidence")
    def evidence(key: str, offset: int = 0, limit: int = 50):
        store.one("results", key)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("无效证据分页")
        rows = store.rows("SELECT definition FROM fusion_evidence WHERE result_id=? ORDER BY ordinal LIMIT ? OFFSET ?", (key, limit, offset))
        return {"units": [json.loads(r["definition"]) for r in rows], "offset": offset, "limit": limit}

    @app.post("/api/projects/{key}/queue/{action}")
    def queue_action(key: str, action: str, body: dict):
        tasks = store.rows("SELECT t.id,t.kind,COALESCE(m.backend,'local') backend FROM tasks t LEFT JOIN multimodal_requests m ON m.task_id=t.id WHERE t.project_id=?", (key,))
        selected = body.get("task_ids")
        if selected is not None and not set(selected) <= {t["id"] for t in tasks}:
            raise ValueError("任务不属于该项目")
        tasks = [t for t in tasks if selected is None or t["id"] in selected]
        if action in {"retry", "resume"} and (not tasks or any(t['kind'] != 'fusion' and t['backend'] != 'external' for t in tasks)):
            require_recognition()
        ids = external_queue.action(key, action, [t['id'] for t in tasks if t['backend'] == 'external'])
        ids.extend(queue.action(key, action, [t["id"] for t in tasks if t["kind"] != "fusion" and t['backend'] != 'external']))
        ids.extend(fusion_queue.action(key, action, [t["id"] for t in tasks if t["kind"] == "fusion"]))
        return {"task_ids": ids}

    @app.get("/api/results/{key}")
    def result(key: str):
        return present_result(store.result(key))

    @app.get("/api/results/{key}/text", response_class=PlainTextResponse)
    def result_text(key: str):
        return PlainTextResponse(export_text(store.result(key)["edited"]))

    @app.post('/api/results/{key}/geometry')
    def add_geometry(key: str, body: dict):
        from ocr_workbench.geometry import enqueue_geometry, bind_manual
        if body.get('source') == 'manual':
            return bind_manual(store, key, body)
        require_recognition()
        response = enqueue_geometry(store, key, body.get('revision'), region_ids=body.get('region_ids'), force=body.get('force', False), algorithm=body.get('algorithm'), provider=body.get('provider', 'paddle'))
        queue.wake.set()
        return response

    @app.get('/api/results/{key}/geometry')
    def get_geometry(key: str):
        from ocr_workbench.geometry import geometry_view
        return geometry_view(store, key)

    @app.get('/api/results/{key}/document-conflicts')
    def document_conflicts(key: str):
        from ocr_workbench.document_conflicts import conflict_view
        return conflict_view(store, key)

    @app.post('/api/results/{key}/document-conflicts/{conflict_id}')
    def review_document_conflict(key: str, conflict_id: str, body: dict):
        from ocr_workbench.document_conflicts import acknowledge_conflict
        return acknowledge_conflict(store, key, conflict_id, body.get('revision'))

    @app.post('/api/results/{key}/geometry/location')
    def get_geometry_location(key: str, body: dict):
        from ocr_workbench.geometry import geometry_view
        return geometry_view(store, key, body.get('target'), provider=body.get('provider'), algorithm=body.get('algorithm'))

    @app.post('/api/results/{key}/review-timing')
    def review_timing(key: str, body: dict):
        result = store.result(key)
        milliseconds = body.get('active_ms')
        if type(milliseconds) is not int or not 0 <= milliseconds <= 86400000 or body.get('action') not in ('candidate','manual','keep','question','structure_accept','structure_keep','structure_reject','structure_defer'):
            raise ValueError('校对计时无效')
        if result['revision'] != body.get('revision'):
            raise Conflict('计时提交对应的修订已变化')
        event_id = body.get('id')
        if not isinstance(event_id, str) or not 8 <= len(event_id) <= 120:
            raise ValueError('计时事件标识无效')
        with store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO review_timings VALUES(?,?,?,?,?,?,?)', (event_id,key,result['revision'],encoded(body.get('target', {})),milliseconds,body['action'],now()))
        return {'saved': True, 'local_only': True}

    @app.put("/api/results/{key}")
    def save_result(key: str, body: dict):
        return present_result(store.save(key, body["edited"], body["revision"]))

    @app.post("/api/results/{key}/history")
    def history(key: str, body: dict):
        return present_result(store.history(key, body["direction"], body["revision"]))

    @app.put("/api/images/{key}/selection")
    def select_result(key: str, body: dict):
        store.one("images", key)
        result = store.one("results", body["result_id"])
        task = store.one("tasks", result["task_id"])
        if task["image_id"] != key:
            raise ValueError("结果不属于当前图片")
        with store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM results WHERE id=?", (result["id"],)).fetchone()
            if (task["kind"] == "fusion" or "revision" in body) and current["revision"] != body.get("revision"):
                raise Conflict("预览结果已变化，请重新读取后采用")
            db.execute(
                "INSERT INTO selections VALUES(?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id", (key, result["id"])
            )
        return {"saved": True}

    @app.put("/api/images/{key}/review")
    def review_image(key: str, body: dict):
        return store.set_review(key, body.get("status"), body.get("result_id"), body.get("revision"), body.get("version_id"))

    @app.post("/api/export")
    def export(body: dict):
        target = services.export(body)
        return FileResponse(
            target,
            filename=target.name,
            background=BackgroundTask(shutil.rmtree, target.parent),
        )

    @app.post('/api/export/preflight')
    def pdf_export_preflight(body: dict):
        from ocr_workbench.pdf_export import build_pdf_export
        with maintenance.guard:
            return build_pdf_export(store, documents, body, preflight=True)

    @app.get("/api/diagnostics")
    def diagnostics():
        runtime = bundle / "runtimes/control/python.exe"
        try:
            check = subprocess.run(
                [str(runtime), "-X", "utf8", "-I", "-m", "ocr_workbench.cli", "doctor"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"passed": False, "details": "", "error": str(error),
                    "free_bytes": shutil.disk_usage(store.root).free}
        return {
            "passed": check.returncode == 0,
            "details": check.stdout,
            "error": check.stderr.strip() if check.returncode else "",
            "free_bytes": shutil.disk_usage(store.root).free,
        }

    @app.get("/api/engine-packages")
    def engine_inventory():
        return registry.inventory()

    @app.post("/api/engine-packages/stage")
    async def import_engine(request: Request):
        inbox = registry.root / "incoming"
        inbox.mkdir(exist_ok=True)
        path = inbox / (uid() + ".zip")
        count = 0
        try:
            with path.open("wb") as stream:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > 128 * 1024**3 or shutil.disk_usage(inbox).free < 1024**3:
                        raise ValueError("引擎包过大或磁盘空间不足")
                    await run_in_threadpool(stream.write, chunk)
            return await run_in_threadpool(registry.stage, path)
        finally:
            path.unlink(missing_ok=True)

    @app.post("/api/engine-packages/activate")
    def activate_engine(body: dict):
        require_recognition()
        with queue.engine_maintenance():
            return registry.activate(body.get("staging_id"), body.get("sha256"))

    @app.post("/api/engine-packages/switch")
    def switch_engine(body: dict):
        require_recognition()
        with queue.engine_maintenance():
            return registry.switch(body.get("engine"), body.get("package_id"))

    @app.delete("/api/engine-packages/staging/{key}")
    def discard_engine(key: str):
        return registry.discard(key)

    @app.post("/api/shutdown")
    def shutdown():
        app.state.shutdown()
        return {"status": "stopping"}

    web = bundle / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="workbench")
    return app


def main():
    import msvcrt
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument(
        "--data",
        type=Path,
        default=None,
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument(
        "--verify-startup",
        action="store_true",
        help="Run complete offline startup gates before serving requests",
    )
    p.add_argument("--startup-check", choices=("auto", "full"), help="Reuse verified installation receipts, or force complete verification")
    p.add_argument("--review-only", action="store_true", help="Open existing projects for review and export without GPU inference")
    args = p.parse_args()
    if args.data is None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise ValueError("Standalone OCR data directory is unavailable; pass --data")
        args.data = Path(local) / "OfflineOCR/Workspace"
    token = args.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("Session token is too short")
    args.data.mkdir(parents=True, exist_ok=True)
    with (args.data / "service.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("该项目目录已由另一个工作台使用")
        if args.verify_startup or args.startup_check:
            from ocr_workbench.startup import run_startup_checks
            from ocr_workbench.engine_packages import EnginePackages

            report = run_startup_checks(
                args.bundle, args.data, EnginePackages(args.bundle, args.data), review_only=args.review_only,
                policy="full" if args.verify_startup else args.startup_check,
            )
            if report["status"] != "passed":
                raise SystemExit(2)
        app = create_app(args.bundle, args.data, token, review_only=args.review_only)
        # A reset loopback socket must not hold shutdown forever (observed under WFP).
        platform_port = os.environ.get("WORKBENCH_PORT") == "0"
        listener = None
        if platform_port:
            import socket
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="info",
                access_log=False,
                timeout_graceful_shutdown=8,
            )
        )
        app.state.shutdown = lambda: setattr(server, "should_exit", True)
        if listener is not None:
            print(f"READY {listener.getsockname()[1]}", flush=True)
            server.run(sockets=[listener])
        else:
            server.run()


if __name__ == "__main__":
    main()
