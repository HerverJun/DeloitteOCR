"""Durable platform asset-set handoff to native classification projects."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from workbench_platform.assets.registry import AssetChanged, AssetUnavailable
from workbench_platform.assets.sets import AssetSetConflict, AssetSetMissing
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.jobs.native_classification import (
    ClassificationAdapter, ClassificationImportConflict, ClassificationUnavailable)
from workbench_platform.jobs.projection import ProjectionConflict, ProjectionUnavailable
from workbench_platform.workspaces.routes import ID, _loopback_host, _same_loopback_origin

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/classification/handoffs",
                   tags=["classification-handoff"])
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class ClassificationHandoff:
    def __init__(self, db_path: Path, jobs, registry, sets):
        self.db_path, self.jobs, self.registry, self.sets = db_path, jobs, registry, sets
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS classification_handoff (
                    handoff_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    link_id TEXT NOT NULL, asset_set_id TEXT NOT NULL,
                    asset_set_revision INTEGER NOT NULL, manifest_digest TEXT NOT NULL,
                    request_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    last_status_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, link_id, asset_set_id, asset_set_revision));
                CREATE TABLE IF NOT EXISTS classification_submission (
                    handoff_id TEXT PRIMARY KEY REFERENCES classification_handoff(handoff_id),
                    model_id TEXT NOT NULL, device TEXT NOT NULL, request_id TEXT NOT NULL,
                    native_job_id TEXT, platform_job_id TEXT, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS classification_handoff_workspace
                    ON classification_handoff(workspace_id, created_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _context(self, workspace_id, link_id):
        context = self.jobs._context(workspace_id, "classification", link_id)
        if context.role == "viewer":
            raise PermissionError()
        return context

    def _adapter(self, context):
        return ClassificationAdapter(context, live=self.jobs._live, timeout=10)

    def _manifest(self, workspace_id, asset_set_id, revision, labels, adapter):
        asset_set = self.sets.get(workspace_id, asset_set_id, revision)
        if not 1 <= len(asset_set.items) <= 1000 or not isinstance(labels, dict):
            raise ValueError("Invalid classification input selection")
        ids = {item.item_id for item in asset_set.items}
        if not set(labels) <= ids or any(not isinstance(key, str) for key in labels):
            raise ValueError("Invalid classification label targets")
        project = adapter.project()
        native_labels = {item.get("label_id") for item in project["labels"]
                         if isinstance(item, dict)}
        items = []
        for member in asset_set.items:
            label_id = labels.get(member.item_id)
            if label_id is not None and (not isinstance(label_id, str) or label_id not in native_labels):
                raise ValueError("Category does not belong to the selected native project")
            asset = self.registry.get(workspace_id, member.asset_id)
            if (asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash
                    or Path(asset.original_name).suffix.lower() not in IMAGE_EXTENSIONS
                    or not 1 <= asset.size_bytes <= 100 * 1024 * 1024):
                raise AssetChanged("Classification input is unavailable")
            with self.registry.stage_verified(workspace_id, asset.asset_id,
                                              revision=member.asset_revision, blob_hash=member.blob_hash):
                pass
            items.append({"item_id": member.item_id, "asset_id": asset.asset_id,
                          "blob_hash": asset.blob_hash, "size_bytes": asset.size_bytes,
                          "name": asset.original_name, "label_id": label_id})
        return {"schema_version": "1.0", "asset_set_id": asset_set_id,
                "asset_set_revision": revision, "manifest_hash": asset_set.manifest_hash,
                "import_mode": "copy", "items": items}

    def _row(self, workspace_id, handoff_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM classification_handoff WHERE workspace_id=? "
                             "AND handoff_id=?", (workspace_id, handoff_id)).fetchone()
        if row is None:
            raise KeyError("Classification handoff not found")
        return row

    def _save(self, row, status):
        with closing(self._connect()) as db:
            db.execute("UPDATE classification_handoff SET last_status_json=?,updated_at=? "
                       "WHERE handoff_id=?", (_json(status), _utc(), row["handoff_id"]))
        return self._row(row["workspace_id"], row["handoff_id"])

    def _format(self, row, status, *, reachable):
        manifest = json.loads(row["manifest_json"])
        observed = {item["item_id"]: item for item in status["items"]} if status else {}
        items = [{"item_id": item["item_id"], "asset_id": item["asset_id"],
                  "name": item["name"], "label_id": item["label_id"],
                  "status": observed.get(item["item_id"], {}).get("status", "unknown"),
                  "target_asset_id": observed.get(item["item_id"], {}).get("target_asset_id")}
                 for item in manifest["items"]]
        with closing(self._connect()) as db:
            processing = db.execute("SELECT model_id,device,native_job_id,platform_job_id "
                                    "FROM classification_submission WHERE handoff_id=?",
                                    (row["handoff_id"],)).fetchone()
        return {"handoff_id": row["handoff_id"], "workspace_id": row["workspace_id"],
                "link_id": row["link_id"], "asset_set_id": row["asset_set_id"],
                "asset_set_revision": row["asset_set_revision"], "request_id": row["request_id"],
                "state": status["state"] if reachable and status else "unknown",
                "availability": "ready" if reachable else "unreachable", "items": items,
                "processing": ({"model_id": processing["model_id"], "device": processing["device"],
                                "platform_job_id": processing["platform_job_id"],
                                "request_state": "registered" if processing["platform_job_id"]
                                    else "sent" if processing["native_job_id"] else "unknown"}
                               if processing else None),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _observe(self, adapter, row):
        try:
            status = adapter.import_status(row["request_id"])
            return self._save(row, status), status, True
        except ClassificationUnavailable:
            previous = json.loads(row["last_status_json"]) if row["last_status_json"] else None
            return row, previous, False

    def begin(self, workspace_id, link_id, asset_set_id, revision, labels):
        with self.lock:
            context = self._context(workspace_id, link_id)
            adapter = self._adapter(context)
            manifest = self._manifest(workspace_id, asset_set_id, revision, labels, adapter)
            digest = hashlib.sha256(_json(manifest).encode()).hexdigest()
            key = hashlib.sha256(_json([workspace_id, link_id, asset_set_id, revision]).encode()).hexdigest()
            handoff_id, request_id = "h" + key[:32], "r" + key[:32]
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM classification_handoff WHERE handoff_id=?",
                                 (handoff_id,)).fetchone()
                if row is None:
                    stamp = _utc()
                    db.execute("INSERT INTO classification_handoff VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (handoff_id, workspace_id, link_id, asset_set_id, revision,
                                digest, request_id, _json(manifest), None, stamp, stamp))
                    row = db.execute("SELECT * FROM classification_handoff WHERE handoff_id=?",
                                     (handoff_id,)).fetchone()
            if row["manifest_digest"] != digest:
                raise AssetSetConflict("This classification handoff is bound to other labels")
            return self._continue(context, row)

    def _continue(self, context, row):
        adapter = self._adapter(context)
        manifest = json.loads(row["manifest_json"])
        asset_set = self.sets.get(row["workspace_id"], row["asset_set_id"],
                                  row["asset_set_revision"])
        revisions = {item.item_id: item.asset_revision for item in asset_set.items}
        try:
            status = adapter.import_status(row["request_id"])
        except ClassificationUnavailable:
            try:
                status = adapter.begin_import(row["request_id"], manifest)
            except ClassificationImportConflict:
                raise AssetSetConflict("Native import request conflicts") from None
            except ClassificationUnavailable:
                row, status, reachable = self._observe(adapter, row)
                return self._format(row, status, reachable=reachable)
        if (status["payload_hash"] != hashlib.sha256(_json(manifest).encode()).hexdigest()
                or [item["item_id"] for item in status["items"]]
                != [item["item_id"] for item in manifest["items"]]):
            raise AssetSetConflict("Native classification import manifest differs")
        row = self._save(row, status)
        for source in manifest["items"]:
            native = next(item for item in status["items"] if item["item_id"] == source["item_id"])
            if native["status"] != "pending":
                continue
            try:
                with self.registry.stage_verified(row["workspace_id"], source["asset_id"],
                                                  revision=revisions[source["item_id"]],
                                                  blob_hash=source["blob_hash"]) as path:
                    status = adapter.import_item(row["request_id"], source["item_id"], path,
                                                 blob_hash=source["blob_hash"],
                                                 size_bytes=source["size_bytes"])
            except ClassificationUnavailable:
                row, status, reachable = self._observe(adapter, row)
                if not reachable or status["state"] == "unknown":
                    return self._format(row, status, reachable=reachable)
                if next(item for item in status["items"] if item["item_id"] == source["item_id"])["status"] == "pending":
                    return self._format(row, status, reachable=False)
            row = self._save(row, status)
        row, status, reachable = self._observe(adapter, row)
        return self._format(row, status, reachable=reachable)

    def resume(self, workspace_id, handoff_id):
        with self.lock:
            row = self._row(workspace_id, handoff_id)
            return self._continue(self._context(workspace_id, row["link_id"]), row)

    def status(self, workspace_id, handoff_id):
        row = self._row(workspace_id, handoff_id)
        try:
            adapter = self._adapter(self._context(workspace_id, row["link_id"]))
        except (LaunchStatus, ClassificationUnavailable, ValueError):
            previous = json.loads(row["last_status_json"]) if row["last_status_json"] else None
            return self._format(row, previous, reachable=False)
        row, native, reachable = self._observe(adapter, row)
        return self._format(row, native, reachable=reachable)

    def models(self, workspace_id, handoff_id):
        row = self._row(workspace_id, handoff_id)
        adapter = self._adapter(self._context(workspace_id, row["link_id"]))
        return {"items": [{"model_id": item["id"], "name": item.get("name") or item["id"]}
                          for item in adapter.models()]}

    def result_snapshot(self, workspace_id, handoff_id):
        row = self._row(workspace_id, handoff_id)
        with closing(self._connect()) as db:
            submission = db.execute("SELECT * FROM classification_submission WHERE handoff_id=?",
                                    (handoff_id,)).fetchone()
        if submission is None or not submission["native_job_id"] or not submission["platform_job_id"]:
            raise AssetSetConflict("Classification task is not registered")
        context = self._context(workspace_id, row["link_id"])
        adapter = self._adapter(context)
        job = adapter.job(submission["native_job_id"])
        if (job.kind != "inference" or job.state not in {
                "succeeded", "completed_with_errors", "failed", "cancelled"}
                or job.total > 5000):
            raise AssetSetConflict("Classification result is not final")
        imported = adapter.import_status(row["request_id"])
        asset_set = self.sets.get(workspace_id, row["asset_set_id"], row["asset_set_revision"])
        if imported["state"] != "completed" or len(imported["items"]) != len(asset_set.items):
            raise AssetSetConflict("Classification input mapping is incomplete")
        sources = {}
        inputs = []
        for member, native in zip(asset_set.items, imported["items"]):
            if member.item_id != native["item_id"] or native["status"] != "imported":
                raise AssetSetConflict("Classification input mapping changed")
            asset = self.registry.get(workspace_id, member.asset_id)
            if (asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash):
                raise AssetSetConflict("Classification source version changed")
            with self.registry.stage_verified(workspace_id, asset.asset_id,
                                              revision=member.asset_revision, blob_hash=member.blob_hash):
                pass
            native_asset_id = native["target_asset_id"]
            if native_asset_id in sources:
                raise AssetSetConflict("Native source identity was repeated")
            sources[native_asset_id] = (asset, member)
            inputs.append({"role": "image", "name": asset.original_name,
                           "asset_id": asset.asset_id, "revision": asset.revision,
                           "blob_hash": asset.blob_hash,
                           "source_locator": member.source_locator.model_dump(mode="json")})
        results = []
        for offset in range(0, job.total, 200):
            results.extend(adapter.results(job.native_job_id,
                                           limit=min(200, job.total - offset), offset=offset))
        if len(results) != job.total or len({item.native_item_id for item in results}) != len(results):
            raise AssetSetConflict("Classification result coverage is incomplete")
        items = []
        for result in results:
            if result.asset_id not in sources:
                raise AssetSetConflict("Result refers to another input")
            asset, _ = sources[result.asset_id]
            items.append({"native_item_id": result.native_item_id,
                          "native_asset_id": result.asset_id, "asset_id": asset.asset_id,
                          "source_name": asset.original_name, "state": result.state,
                          "prediction_label_id": result.prediction_label_id,
                          "prediction_status": result.prediction_status,
                          "prediction_score": result.prediction_score,
                          "human_label_id": result.human_label_id,
                          "disposition_state": result.disposition_state,
                          "disposition_revision": result.disposition_revision,
                          "disposition_note": result.disposition_note,
                          "current_annotation_label_id": result.current_annotation_label_id,
                          "error": result.error})
        projection = self.jobs.refresh(workspace_id, self.jobs.ledger.get(
            workspace_id, submission["platform_job_id"]))
        if projection["status"] not in {"succeeded", "partial", "failed", "cancelled"}:
            raise AssetSetConflict("Platform task is not final")
        project = adapter.project()
        return {"workspace_id": workspace_id, "handoff_id": handoff_id,
                "project_id": context.module_project_id, "project_name": project["name"],
                "platform_job_id": submission["platform_job_id"],
                "native_job_id": submission["native_job_id"],
                "model_id": submission["model_id"], "input_set_id": asset_set.asset_set_id,
                "input_set_revision": asset_set.revision, "input_manifest_hash": asset_set.manifest_hash,
                "inputs": inputs, "items": items, "native_state": job.state,
                "platform_job": projection}

    def submit(self, workspace_id, handoff_id, model_id, device, *, allow_new=True):
        if (not isinstance(model_id, str) or not ID.fullmatch(model_id)
                or device not in {"cpu", "cuda"}):
            raise ValueError("Invalid model selection")
        with self.lock:
            row = self._row(workspace_id, handoff_id)
            context = self._context(workspace_id, row["link_id"])
            adapter = self._adapter(context)
            native = adapter.import_status(row["request_id"])
            if native["state"] != "completed":
                raise AssetSetConflict("Classification import has not completed")
            asset_ids = [item["target_asset_id"] for item in native["items"]]
            if any(not isinstance(item, str) for item in asset_ids):
                raise AssetSetConflict("Imported classification asset is missing")
            digest = hashlib.sha256(_json([handoff_id, model_id, device]).encode()).hexdigest()
            request_id = "r" + digest[:32]
            with closing(self._connect()) as db:
                submission = db.execute("SELECT * FROM classification_submission WHERE handoff_id=?",
                                        (handoff_id,)).fetchone()
                if submission is None:
                    db.execute("INSERT INTO classification_submission VALUES(?,?,?,?,?,?,?)",
                               (handoff_id, model_id, device, request_id, None, None, _utc()))
                    submission = db.execute("SELECT * FROM classification_submission WHERE handoff_id=?",
                                            (handoff_id,)).fetchone()
            if (submission["model_id"] != model_id or submission["device"] != device
                    or submission["request_id"] != request_id):
                raise AssetSetConflict("Processing request is already bound to another model")
            native_job = submission["native_job_id"]
            if native_job is None:
                lookup = getattr(adapter, "lookup_inference", None)
                native_job = lookup(request_id) if callable(lookup) else None
                if native_job is None:
                    if not allow_new:
                        # Only a successful strict native inference-request GET
                        # can authorize the same original keyed submission.
                        return {"not_submitted": True, "authoritative_negative": callable(lookup),
                            "native_request_id": request_id, "native_project_id": context.module_project_id,
                            "input_fingerprint": hashlib.sha256(_json({"handoff_id": handoff_id,
                                "manifest_digest": row["manifest_digest"], "model_id": model_id,
                                "device": device, "asset_ids": asset_ids}).encode()).hexdigest()}
                    native_job = adapter.infer(model_id, asset_ids, key=request_id, device=device)
                with closing(self._connect()) as db:
                    db.execute("UPDATE classification_submission SET native_job_id=? "
                               "WHERE handoff_id=?", (native_job, handoff_id))
            job = self.jobs.register(workspace_id, row["link_id"], "classification", [native_job])
            with closing(self._connect()) as db:
                db.execute("UPDATE classification_submission SET platform_job_id=? "
                           "WHERE handoff_id=?", (job["platform_job_id"], handoff_id))
            return {"handoff": self.status(workspace_id, handoff_id), "job": job}


def configure_classification_handoff(app):
    root = os.environ.get("WORKBENCH_DATA_DIR")
    assets = getattr(app.state, "asset_library", None)
    jobs = getattr(app.state, "job_service", None)
    app.state.classification_handoff = (ClassificationHandoff(
        Path(root).resolve() / "classification-handoffs.sqlite3", jobs, assets[0], assets[1])
        if root and assets and jobs else None)


def _service(request: Request, workspace_id: str, *, write: bool = False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, "请从本机平台页面操作")
    if not ID.fullmatch(workspace_id) or request.query_params:
        raise HTTPException(400, "工作空间请求无效")
    value = getattr(request.app.state, "classification_handoff", None)
    if value is None:
        raise HTTPException(503, "分类交接尚未配置")
    return value


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


async def _body(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(400, "请提交 JSON 参数")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 131072:
            raise HTTPException(413, "交接参数过大")
        raw.extend(chunk)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "交接参数无效") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "交接参数无效")
    return value


def _failed(error: Exception):
    if isinstance(error, (KeyError, AssetSetMissing, ProjectionUnavailable)):
        raise HTTPException(404, "资料集、项目或交接不存在") from None
    if isinstance(error, PermissionError):
        raise HTTPException(403, "当前项目不可导入") from None
    if isinstance(error, (LaunchStatus, ClassificationUnavailable)):
        raise HTTPException(503, "分类应用尚未就绪") from None
    if isinstance(error, (AssetUnavailable, AssetSetConflict, ProjectionConflict)):
        raise HTTPException(409, "资料或原生任务状态已变化") from None
    raise HTTPException(400, "交接请求无效") from None


@router.post("", status_code=201)
async def begin_handoff(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"link_id", "asset_set_id", "asset_set_revision", "labels"}:
        raise HTTPException(400, "请选择项目、资料集和类别")
    try:
        return await run_in_threadpool(service.begin, workspace_id, payload["link_id"],
                                       payload["asset_set_id"], payload["asset_set_revision"],
                                       payload["labels"])
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict,
            ProjectionUnavailable, PermissionError, LaunchStatus,
            ClassificationUnavailable, ValueError) as error:
        _failed(error)


@router.get("/{handoff_id}")
async def get_handoff(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.status, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict,
            ProjectionUnavailable, PermissionError, LaunchStatus,
            ClassificationUnavailable, ValueError) as error:
        _failed(error)


@router.post("/{handoff_id}/resume")
async def resume_handoff(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "续传请求不接受参数")
    try:
        return await run_in_threadpool(service.resume, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict,
            ProjectionUnavailable, PermissionError, LaunchStatus,
            ClassificationUnavailable, ValueError) as error:
        _failed(error)


@router.get("/{handoff_id}/models")
async def list_models(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.models, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict,
            ProjectionUnavailable, PermissionError, LaunchStatus,
            ClassificationUnavailable, ValueError) as error:
        _failed(error)


@router.post("/{handoff_id}/jobs", status_code=201)
async def submit_inference(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"model_id", "device"}:
        raise HTTPException(400, "请选择分类模型和运行设备")
    try:
        return await run_in_threadpool(service.submit, workspace_id, handoff_id,
                                       payload["model_id"], payload["device"])
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict,
            ProjectionConflict, ProjectionUnavailable, PermissionError, LaunchStatus,
            ClassificationUnavailable, ValueError) as error:
        _failed(error)
