from contextlib import asynccontextmanager
import csv
from importlib import metadata as package_metadata
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal
import zipfile

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .datasets import load_dataset, publish_dataset, register_asset
from .images import SUPPORTED, file_sha, thumbnail
from .runtime import validate_package
from .settings import REPO, Settings
from .store import Store, dump, now, uid


class Label(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    label_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    display_name: str = Field(min_length=1, max_length=60)
    definition: str = Field(default="", max_length=1000)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    labels: list[Label] = Field(min_length=2, max_length=1000)

    @model_validator(mode="after")
    def unique_labels(self):
        if len({x.label_id for x in self.labels}) != len(self.labels):
            raise ValueError("类别 ID 不能重复")
        if len({x.display_name for x in self.labels}) != len(self.labels):
            raise ValueError("类别显示名不能重复")
        return self


class ImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    infer_labels: bool = True
    source_id: str = Field(default="", max_length=200)


class DatasetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class TrainingRequest(BaseModel):
    dataset_id: str
    encoder: Literal["resnet18", "dinov2", "mobilenetv4", "repvit"] = "resnet18"
    head_method: Literal["adamw", "logistic_cv"] = "adamw"
    input_size: Literal[224, 336] = 224
    epochs: int = Field(default=100, ge=1, le=500)
    batch_size: int = Field(default=16, ge=1, le=64)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    device: Literal["cuda", "cpu"] = "cuda"
    minimum_precision: float = Field(default=0.95, ge=0.5, le=1)


class InferenceRequest(BaseModel):
    model_id: str
    batch_size: int = Field(default=32, ge=1, le=128)
    device: Literal["cuda", "cpu"] = "cuda"
    auto_accept: bool = False
    scope: Literal["all", "unlabeled"] = "unlabeled"
    asset_ids: list[str] | None = Field(default=None, max_length=5000)


class AnnotationRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=500)
    label_id: str | None
    group_id: str | None = Field(default=None, min_length=1, max_length=200)


class JobAction(BaseModel):
    action: Literal["pause", "resume", "cancel", "retry_failed"]


def public_job(store, row):
    item = dict(row)
    for key in ("params", "result"):
        if isinstance(item.get(key), str):
            item[key] = json.loads(item[key])
    for key in ("owner", "lease_token", "lease_until"):
        item.pop(key, None)
    return item


def create_app(settings=None):
    settings = settings or Settings.from_env()
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(title="图像分类工作台", version="0.2.0", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    origins = [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (8765, 5173)]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "PATCH"],
                       allow_headers=["Content-Type", "X-Platform-Request", "Idempotency-Key"])

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            same_origin = str(request.base_url).rstrip("/")
            if origin and origin not in origins and origin != same_origin:
                return JSONResponse({"detail": "拒绝外部网站发起的本机写入请求"}, status_code=403)
            if request.headers.get("x-platform-request") != "1":
                return JSONResponse({"detail": "缺少 X-Platform-Request 请求头"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(LookupError)
    async def not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "version": "0.2.0", "mode": "local", "time": now()}

    @app.get("/api/v1/runtime")
    def runtime():
        versions = {}
        for name in ("torch", "timm", "onnxruntime-gpu", "onnxruntime"):
            try:
                versions[name] = package_metadata.version(name)
            except package_metadata.PackageNotFoundError:
                versions[name] = None
        gpu = None
        executable = shutil.which("nvidia-smi")
        if executable:
            try:
                output = subprocess.run([executable, "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                if output.returncode == 0:
                    values = [part.strip() for part in output.stdout.splitlines()[0].split(",")]
                    gpu = {"name": values[0], "memory_total_mib": int(values[1]), "memory_used_mib": int(values[2]), "utilization": int(values[3])}
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                pass
        disk = shutil.disk_usage(settings.data_dir)
        return {"python": sys.version.split()[0], "packages": versions, "gpu": gpu,
                "data_dir": str(settings.data_dir), "free_bytes": disk.free,
                "worker": store.one("SELECT resource,expires FROM leases WHERE resource='worker'"),
                "note": "空闲时工作进程不占用任务租约；GPU 实际执行由训练导出验证记录确认。"}

    @app.get("/api/v1/projects")
    def projects():
        rows = store.rows("SELECT p.*, (SELECT COUNT(*) FROM assets a WHERE a.project_id=p.id AND archived=0) asset_count, (SELECT COUNT(*) FROM models m WHERE m.project_id=p.id) model_count FROM projects p ORDER BY created_at DESC")
        for row in rows:
            row["labels"] = json.loads(row["labels"])
        return rows

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: ProjectCreate):
        project_id = uid("project")
        labels = [label.model_dump() for label in body.labels]
        for label in labels:
            if not label["definition"]:
                label["definition"] = label["display_name"]
        with store.connect(write=True) as con:
            con.execute("INSERT INTO projects(id,name,description,labels,created_at) VALUES(?,?,?,?,?)", (project_id, body.name.strip(), body.description, dump(labels), now()))
        return store.project(project_id)

    @app.get("/api/v1/projects/{project_id}")
    def project_detail(project_id: str):
        project = store.project(project_id)
        project["asset_counts"] = store.rows("SELECT label_id,COUNT(*) count FROM assets WHERE project_id=? AND archived=0 GROUP BY label_id", (project_id,))
        project["dataset_count"] = store.one("SELECT COUNT(*) count FROM datasets WHERE project_id=?", (project_id,))["count"]
        project["model_count"] = store.one("SELECT COUNT(*) count FROM models WHERE project_id=?", (project_id,))["count"]
        project["job_counts"] = store.rows("SELECT state,COUNT(*) count FROM jobs WHERE project_id=? GROUP BY state", (project_id,))
        return project

    @app.post("/api/v1/projects/{project_id}/imports", status_code=202)
    def import_images(project_id: str, body: ImportRequest, idempotency_key: str | None = Header(default=None)):
        path = Path(body.path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("目录不存在，请填写此服务器上的完整路径")
        params = body.model_dump()
        params["path"] = str(path)
        job_id = store.submit(project_id, "import", params, key=idempotency_key)
        return {"job_id": job_id}

    @app.post("/api/v1/projects/{project_id}/uploads")
    def uploads(project_id: str, files: list[UploadFile] = File(...), label_id: str | None = Form(default=None)):
        project = store.project(project_id)
        label_id = label_id or None
        if label_id and label_id not in {label["label_id"] for label in project["labels"]}:
            raise ValueError("类别不属于此项目")
        if len(files) > 100:
            raise ValueError("单次最多上传 100 张；大量图片请使用目录导入")
        results, errors = [], []
        folder = settings.data_dir / "uploads" / project_id
        folder.mkdir(exist_ok=True)
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in SUPPORTED:
                errors.append({"name": upload.filename, "error": "不支持的格式"})
                continue
            destination = folder / f"{uid('upload')}{suffix}"
            try:
                size = 0
                with destination.open("wb") as stream:
                    for block in iter(lambda: upload.file.read(1024 * 1024), b""):
                        size += len(block)
                        if size > settings.max_upload_bytes:
                            raise ValueError("文件超过 100 MiB")
                        stream.write(block)
                asset, _ = register_asset(store, project_id, destination, label_id)
                with store.connect(write=True) as con:
                    con.execute("UPDATE assets SET name=? WHERE id=?", (Path(upload.filename or destination.name).name, asset["id"]))
                results.append(asset["id"])
            except (ValueError, OSError) as exc:
                destination.unlink(missing_ok=True)
                errors.append({"name": upload.filename, "error": str(exc)})
        return {"asset_ids": results, "errors": errors}

    @app.get("/api/v1/projects/{project_id}/assets")
    def assets(project_id: str, limit: int = Query(48, ge=1, le=200), offset: int = Query(0, ge=0), label: str | None = None, search: str = ""):
        store.project(project_id)
        conditions, params = ["project_id=?", "archived=0"], [project_id]
        if label == "unlabeled":
            conditions.append("label_id IS NULL")
        elif label:
            conditions.append("label_id=?")
            params.append(label)
        if search:
            conditions.append("name LIKE ?")
            params.append("%" + search + "%")
        where = " AND ".join(conditions)
        total = store.one(f"SELECT COUNT(*) count FROM assets WHERE {where}", params)["count"]
        rows = store.rows(f"SELECT * FROM assets WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*params, limit, offset))
        return {"items": rows, "total": total}

    @app.get("/api/v1/assets/{asset_id}/thumbnail")
    def asset_thumbnail(asset_id: str):
        asset = store.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not asset:
            raise LookupError("图片不存在")
        destination = settings.data_dir / "thumbnails" / f"{asset['sha256']}.jpg"
        if not destination.exists():
            try:
                thumbnail(asset["path"], destination, asset["sha256"])
            except (ValueError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc
        return FileResponse(destination, media_type="image/jpeg")

    @app.post("/api/v1/projects/{project_id}/annotations")
    def annotate(project_id: str, body: AnnotationRequest):
        project = store.project(project_id)
        if body.label_id and body.label_id not in {label["label_id"] for label in project["labels"]}:
            raise ValueError("类别不属于此项目")
        with store.connect(write=True) as con:
            for asset_id in dict.fromkeys(body.asset_ids):
                item = con.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
                if not item:
                    raise LookupError("图片不存在或不属于当前项目")
                con.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?)", (uid("annotation"), asset_id, item["label_id"], body.label_id, "human_confirmed", now()))
                con.execute("UPDATE assets SET label_id=?,group_id=COALESCE(?,group_id) WHERE id=?", (body.label_id, body.group_id, asset_id))
        return {"updated": len(set(body.asset_ids)), "note": "旧数据版本保持不变，请发布新版本以用于下一轮训练。"}

    @app.get("/api/v1/assets/{asset_id}/image")
    def original_image(asset_id: str):
        asset = store.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not asset:
            raise LookupError("图片不存在")
        try:
            if file_sha(asset["path"]) != asset["sha256"]:
                raise ValueError("input_changed: 图片内容已变化")
        except OSError as exc:
            raise HTTPException(404, "源图片已无法读取") from exc
        return FileResponse(asset["path"], filename=asset["name"], content_disposition_type="inline")

    @app.get("/api/v1/projects/{project_id}/dataset-versions")
    def datasets(project_id: str):
        store.project(project_id)
        rows = store.rows("SELECT id,name,stats,created_at,manifest_sha256 FROM datasets WHERE project_id=? ORDER BY created_at DESC", (project_id,))
        for row in rows:
            row["stats"] = json.loads(row["stats"])
        return rows

    @app.post("/api/v1/projects/{project_id}/dataset-versions", status_code=201)
    def dataset_create(project_id: str, body: DatasetRequest):
        return publish_dataset(store, project_id, body.name, body.seed)

    @app.post("/api/v1/projects/{project_id}/experiments", status_code=202)
    def train(project_id: str, body: TrainingRequest, idempotency_key: str | None = Header(default=None)):
        dataset, _ = load_dataset(store, body.dataset_id)
        if dataset["project_id"] != project_id:
            raise ValueError("数据集不属于此项目")
        job_id = store.submit(project_id, "train", body.model_dump(), key=idempotency_key)
        return {"job_id": job_id}

    @app.get("/api/v1/projects/{project_id}/models")
    def models(project_id: str):
        store.project(project_id)
        rows = store.rows("SELECT * FROM models WHERE project_id=? ORDER BY created_at DESC", (project_id,))
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
            row["metrics"] = json.loads(row["metrics"])
        return rows

    @app.put("/api/v1/projects/{project_id}/active-model")
    def activate_model(project_id: str, body: dict):
        row = store.one("SELECT * FROM models WHERE id=? AND project_id=?", (body.get("model_id"), project_id))
        if not row:
            raise LookupError("模型不存在")
        validate_package(row["package_path"])
        with store.connect(write=True) as con:
            con.execute("UPDATE projects SET active_model_id=? WHERE id=?", (row["id"], project_id))
        return {"active_model_id": row["id"]}

    @app.get("/api/v1/models/{model_id}/download")
    def download_model(model_id: str):
        row = store.one("SELECT * FROM models WHERE id=?", (model_id,))
        if not row:
            raise LookupError("模型不存在")
        validate_package(row["package_path"])
        destination = settings.data_dir / "exports" / f"{model_id}.zip"
        if not destination.exists():
            temp = destination.with_suffix(".tmp")
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED) as archive:
                for path in sorted(Path(row["package_path"]).rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(row["package_path"]))
            temp.replace(destination)
        return FileResponse(destination, media_type="application/zip", filename=destination.name)

    @app.post("/api/v1/projects/{project_id}/batch-jobs", status_code=202)
    def batch_job(project_id: str, body: InferenceRequest, idempotency_key: str | None = Header(default=None)):
        row = store.one("SELECT * FROM models WHERE id=? AND project_id=?", (body.model_id, project_id))
        if not row:
            raise LookupError("模型不存在或不属于此项目")
        model_meta = validate_package(row["package_path"])
        where, values = "project_id=? AND archived=0", [project_id]
        if body.asset_ids is not None:
            if not body.asset_ids:
                raise ValueError("请至少选择一张图片")
            where += " AND id IN (" + ",".join("?" for _ in body.asset_ids) + ")"
            values.extend(body.asset_ids)
        elif body.scope == "unlabeled":
            where += " AND label_id IS NULL"
        with store.connect() as con:
            count = con.execute(f"SELECT COUNT(*) FROM assets WHERE {where}", values).fetchone()[0]
            if not count:
                raise ValueError("选择范围内没有图片")
            if body.asset_ids is not None and count != len(set(body.asset_ids)):
                raise ValueError("选中的图片包含不存在或其他项目的资产")
            params = {**body.model_dump(), "model_sha256": model_meta["provenance"]["model_onnx_sha256"]}
            # The cursor streams input identity rows into the job transaction.
            items = con.execute(f"SELECT id,path,sha256 FROM assets WHERE {where} ORDER BY id", values)
            job_id = store.submit(project_id, "inference", params, items, idempotency_key)
        return {"job_id": job_id}

    @app.get("/api/v1/projects/{project_id}/jobs")
    def jobs(project_id: str, limit: int = Query(50, ge=1, le=200)):
        store.project(project_id)
        return [public_job(store, row) for row in store.rows("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))]

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str):
        return public_job(store, store.job(job_id))

    @app.get("/api/v1/jobs/{job_id}/logs")
    def job_logs(job_id: str):
        store.job(job_id)
        path = settings.data_dir / "logs" / f"{job_id}.log"
        if not path.exists():
            return {"text": "任务尚未开始"}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 40_000))
            content = stream.read().decode("utf-8", errors="replace")
        return {"text": content}

    @app.post("/api/v1/jobs/{job_id}/actions")
    def job_action(job_id: str, body: JobAction):
        if body.action != "retry_failed":
            return public_job(store, store.action(job_id, body.action))
        old = store.job(job_id)
        if old["kind"] != "inference" or old["state"] not in {"failed", "completed_with_errors", "cancelled"}:
            raise ValueError("仅终止的批量分类任务可重试未完成项")
        params = {**old["params"], "retry_of": job_id}
        with store.connect() as con:
            items = con.execute("SELECT asset_id id,path,sha256 FROM job_items WHERE job_id=? AND state IN ('failed','cancelled','pending')", (job_id,))
            first = items.fetchone()
            if not first:
                raise ValueError("没有可重试项")
            import itertools
            new_id = store.submit(old["project_id"], "inference", params, itertools.chain([first], items))
        return {"job_id": new_id}

    @app.get("/api/v1/jobs/{job_id}/results")
    def results(job_id: str, limit: int = Query(48, ge=1, le=200), offset: int = Query(0, ge=0), status: str | None = None):
        store.job(job_id)
        where, params = "j.job_id=?", [job_id]
        if status in {"accepted", "needs_review"}:
            where += " AND json_extract(j.prediction,'$.status')=?"
            params.append(status)
        elif status in {"failed", "pending", "cancelled"}:
            where += " AND j.state=?"
            params.append(status)
        total = store.one(f"SELECT COUNT(*) count FROM job_items j WHERE {where}", params)["count"]
        rows = store.rows(f"SELECT j.*,a.name,a.label_id confirmed_label FROM job_items j LEFT JOIN assets a ON a.id=j.asset_id WHERE {where} ORDER BY j.id LIMIT ? OFFSET ?", (*params, limit, offset))
        for row in rows:
            row["prediction"] = json.loads(row["prediction"]) if row["prediction"] else None
        return {"items": rows, "total": total}

    @app.get("/api/v1/jobs/{job_id}/export")
    def export_results(job_id: str, format: Literal["csv", "jsonl"] = "csv"):
        store.job(job_id)
        fields = ["job_id", "item_id", "asset_id", "path", "content_sha256", "model_version_id", "label_id", "score", "status", "error", "processed_at", "current_annotation_label_id", "effective_label_id"]

        def records():
            with store.connect() as con:
                for row in con.execute("SELECT j.*,a.label_id current_annotation_label_id FROM job_items j LEFT JOIN assets a ON a.id=j.asset_id WHERE j.job_id=? ORDER BY j.id", (job_id,)):
                    prediction = json.loads(row["prediction"]) if row["prediction"] else {}
                    yield {"job_id": job_id, "item_id": row["id"], "asset_id": row["asset_id"], "path": row["path"], "content_sha256": row["sha256"],
                           "model_version_id": prediction.get("model_version_id"), "label_id": prediction.get("label_id"), "score": prediction.get("score"),
                           "status": prediction.get("status", row["state"]), "error": row["error"], "processed_at": prediction.get("processed_at"),
                           "current_annotation_label_id": row["current_annotation_label_id"],
                           "effective_label_id": row["current_annotation_label_id"] or prediction.get("label_id")}

        def stream():
            if format == "jsonl":
                for record in records():
                    yield dump(record) + "\n"
            else:
                buffer = io.StringIO(newline="")
                writer = csv.DictWriter(buffer, fieldnames=fields)
                yield "\ufeff"
                writer.writeheader()
                yield buffer.getvalue()
                for record in records():
                    buffer.seek(0)
                    buffer.truncate(0)
                    safe = {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value for key, value in record.items()}
                    writer.writerow(safe)
                    yield buffer.getvalue()
        return StreamingResponse(stream(), media_type="text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="{job_id}.{format}"'})

    web = REPO / "apps" / "web" / "dist"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
