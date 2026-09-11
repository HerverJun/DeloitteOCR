"""Authenticated loopback application service and local static UI."""

import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from ocr_workbench import __version__
from ocr_workbench.store import Store, Conflict, uid, now
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.imaging import add_image, transform, thumbnail
from ocr_workbench.exporting import build_export


def create_app(bundle, data, token, *, start_queue=True):
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
    from ocr_workbench.maintenance import ProjectMaintenance

    maintenance = ProjectMaintenance(store, queue)

    def import_one(key, name, temporary):
        with maintenance.guard:
            return add_image(store, key, name, temporary)

    @asynccontextmanager
    async def lifespan(app):
        if start_queue:
            queue.start()
        try:
            yield
        finally:
            if start_queue:
                queue.stop()

    app = FastAPI(
        title="纸页 · 离线 OCR",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.queue = queue
    app.state.shutdown = lambda: None

    @app.middleware("http")
    async def local_auth(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"message": "只允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"message": "不允许跨站请求"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
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

    @app.get("/api/health")
    def health():
        return {"status": "ready", "version": __version__}

    @app.get("/api/state")
    def state():
        return {
            "projects": store.rows("SELECT * FROM projects ORDER BY updated DESC"),
            "engines": registry.engines(),
            "queue": queue.status(),
            "data_directory": str(store.root),
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
    def project(key: str):
        return {
            "project": store.one("projects", key),
            "images": store.rows(
                "SELECT i.*,s.result_id selected_result FROM images i LEFT JOIN selections s ON s.image_id=i.id WHERE i.project_id=? ORDER BY i.created",
                (key,),
            ),
            "versions": store.rows(
                "SELECT v.* FROM versions v JOIN images i ON i.id=v.image_id WHERE i.project_id=? ORDER BY v.created",
                (key,),
            ),
            "tasks": store.rows(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY created", (key,)
            ),
            "queue": queue.status(),
        }

    @app.get("/api/projects/{key}/storage")
    def project_storage(key: str):
        return maintenance.usage(key)

    @app.delete("/api/projects/{key}")
    def delete_project(key: str, body: dict):
        return maintenance.delete(key, body.get("confirmation"))

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
                temporary.unlink(missing_ok=True)
                await upload.close()
        return {"images": imported, "errors": errors}

    @app.get("/api/versions/{key}/image")
    def image(key: str):
        version = store.one("versions", key)
        return FileResponse(store.file(version["path"]), media_type="image/png")

    @app.post("/api/versions/{key}/transform")
    def transform_image(key: str, body: dict):
        if body.get("kind") == "dewarp":
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
        ids = store.enqueue(
            key,
            body.get("version_ids", []),
            body.get("engines", []),
            body.get("preprocess", []),
            {name: spec["package_id"] for name, spec in registry.engines().items()},
        )
        queue.wake.set()
        return {"task_ids": ids}

    @app.post("/api/projects/{key}/queue/{action}")
    def queue_action(key: str, action: str, body: dict):
        return {"task_ids": queue.action(key, action, body.get("task_ids"))}

    @app.get("/api/results/{key}")
    def result(key: str):
        return store.result(key)

    @app.put("/api/results/{key}")
    def save_result(key: str, body: dict):
        return store.save(key, body["edited"], body["revision"])

    @app.post("/api/results/{key}/history")
    def history(key: str, body: dict):
        return store.history(key, body["direction"], body["revision"])

    @app.put("/api/images/{key}/selection")
    def select_result(key: str, body: dict):
        store.one("images", key)
        result = store.one("results", body["result_id"])
        task = store.one("tasks", result["task_id"])
        if task["image_id"] != key:
            raise ValueError("结果不属于当前图片")
        with store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO selections VALUES(?,?)", (key, result["id"])
            )
        return {"saved": True}

    @app.post("/api/export")
    def export(body: dict):
        with maintenance.guard:
            target = build_export(
                store,
                body.get("result_ids", []),
                body.get("format"),
                body.get("aggregate", False),
            )
        return FileResponse(
            target,
            filename=target.name,
            background=BackgroundTask(shutil.rmtree, target.parent),
        )

    @app.get("/api/diagnostics")
    def diagnostics():
        runtime = bundle / "runtimes/control/python.exe"
        check = subprocess.run(
            [str(runtime), "-X", "utf8", "-I", "-m", "ocr_workbench.cli", "doctor"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return {
            "passed": check.returncode == 0,
            "details": check.stdout,
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
        with queue.engine_maintenance():
            return registry.activate(body.get("staging_id"), body.get("sha256"))

    @app.post("/api/engine-packages/switch")
    def switch_engine(body: dict):
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
        default=Path(os.environ["LOCALAPPDATA"]) / "OfflineOCR/Workspace",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument(
        "--verify-startup",
        action="store_true",
        help="Run complete offline startup gates before serving requests",
    )
    args = p.parse_args()
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
        if args.verify_startup:
            from ocr_workbench.startup import run_checks
            from ocr_workbench.engine_packages import EnginePackages

            report = run_checks(
                args.bundle, args.data, EnginePackages(args.bundle, args.data)
            )
            if report["status"] != "passed":
                raise SystemExit(2)
        app = create_app(args.bundle, args.data, token)
        # A reset loopback socket must not hold shutdown forever (observed under WFP).
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
        server.run()


if __name__ == "__main__":
    main()
