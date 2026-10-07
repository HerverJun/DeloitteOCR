"""Durable, resumable handoff from platform asset sets to native OCR imports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from workbench_platform.assets.registry import AssetChanged, AssetUnavailable
from workbench_platform.assets.sets import AssetSetConflict, AssetSetMissing
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.jobs.native_ocr import OcrAdapter, OcrImportConflict, OcrUnavailable
from workbench_platform.jobs.projection import ProjectionConflict, ProjectionUnavailable
from workbench_platform.workspaces.routes import ID, _loopback_host, _same_loopback_origin

router = APIRouter(prefix="/api/platform/v1/workspaces/{workspace_id}/ocr/handoffs", tags=["ocr-handoff"])
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
                              ".webp", ".heic", ".heif", ".pdf"})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class OcrHandoff:
    def __init__(self, db_path: Path, jobs, registry, sets):
        self.db_path, self.jobs, self.registry, self.sets = db_path, jobs, registry, sets
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ocr_handoff (
                    handoff_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    link_id TEXT NOT NULL, asset_set_id TEXT NOT NULL,
                    asset_set_revision INTEGER NOT NULL, manifest_hash TEXT NOT NULL,
                    request_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    last_status_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, link_id, asset_set_id, asset_set_revision));
                CREATE INDEX IF NOT EXISTS ocr_handoff_workspace
                    ON ocr_handoff(workspace_id, created_at);
                CREATE TABLE IF NOT EXISTS ocr_submission (
                    handoff_id TEXT PRIMARY KEY REFERENCES ocr_handoff(handoff_id),
                    engines_json TEXT NOT NULL, request_id TEXT NOT NULL,
                    version_ids_json TEXT NOT NULL, native_task_ids_json TEXT,
                    platform_job_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ocr_page_submission (
                    handoff_id TEXT PRIMARY KEY REFERENCES ocr_handoff(handoff_id),
                    document_id TEXT NOT NULL,request_id TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,created_at TEXT NOT NULL);
            """)
            if 'processing_json' not in {row[1] for row in db.execute('PRAGMA table_info(ocr_submission)')}:
                db.execute("ALTER TABLE ocr_submission ADD COLUMN processing_json TEXT NOT NULL DEFAULT '{\"preprocess\":[]}'")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _context(self, workspace_id: str, link_id: str):
        context = self.jobs._context(workspace_id, "ocr", link_id)
        if context.role == "viewer":
            raise PermissionError()
        return context

    def _adapter(self, context):
        return OcrAdapter(context, token=self.jobs._token, live=self.jobs._live, timeout=10)

    def _manifest(self, workspace_id: str, asset_set_id: str, revision: int):
        asset_set = self.sets.get(workspace_id, asset_set_id, revision)
        if not 1 <= len(asset_set.items) <= 1000:
            raise ValueError("OCR import item count is unsupported")
        items = []
        for member in asset_set.items:
            asset = self.registry.get(workspace_id, member.asset_id)
            if (asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash
                    or Path(asset.original_name).suffix.lower() not in IMAGE_EXTENSIONS
                    or not 1 <= asset.size_bytes <= 128 * 1024 * 1024):
                raise AssetChanged("OCR input version or type is unavailable")
            with self.registry.stage_verified(workspace_id, member.asset_id,
                                              revision=member.asset_revision, blob_hash=member.blob_hash):
                pass
            items.append({"item_id": member.item_id, "asset_id": asset.asset_id,
                          "blob_hash": asset.blob_hash, "size_bytes": asset.size_bytes,
                          "name": asset.original_name})
        return {"schema_version": "1.0", "asset_set_id": asset_set_id,
                "asset_set_revision": revision, "manifest_hash": asset_set.manifest_hash,
                "import_mode": "copy", "items": items}

    def _row(self, workspace_id: str, handoff_id: str):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM ocr_handoff WHERE workspace_id=? AND handoff_id=?",
                             (workspace_id, handoff_id)).fetchone()
        if row is None:
            raise KeyError("Handoff not found")
        return row

    def _format(self, row, status: dict | None, *, reachable: bool):
        manifest = json.loads(row["manifest_json"])
        observed = {item["item_id"]: item for item in status["items"]} if status else {}
        items = []
        for source in manifest["items"]:
            native = observed.get(source["item_id"], {})
            items.append({"item_id": source["item_id"], "asset_id": source["asset_id"],
                          "name": source["name"], "status": native.get("status", "unknown"),
                          "target_asset_id": native.get("target_asset_id"),
                          "target_version_id": native.get("target_version_id"),
                          "target_document_id": native.get("target_document_id"),
                          "page_count": native.get("page_count"),
                          "page_numbers": native.get("page_numbers")})
        with closing(self._connect()) as db:
            processing = db.execute("SELECT engines_json,platform_job_id,native_task_ids_json,processing_json "
                                    "FROM ocr_submission WHERE handoff_id=?",
                                    (row["handoff_id"],)).fetchone()
        return {"handoff_id": row["handoff_id"], "workspace_id": row["workspace_id"],
                "link_id": row["link_id"], "asset_set_id": row["asset_set_id"],
                "asset_set_revision": row["asset_set_revision"],
                "request_id": row["request_id"],
                "state": status["state"] if reachable and status else "unknown",
                "availability": "ready" if reachable else "unreachable",
                "processing": ({"engines": json.loads(processing["engines_json"]),
                                **json.loads(processing['processing_json']),
                                "platform_job_id": processing["platform_job_id"],
                                "request_state": "registered" if processing["platform_job_id"]
                                    else "sent" if processing["native_task_ids_json"] else "unknown"}
                               if processing else None),
                "items": items, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _save(self, row, status: dict):
        with closing(self._connect()) as db:
            db.execute("UPDATE ocr_handoff SET last_status_json=?,updated_at=? WHERE handoff_id=?",
                       (_json(status), _utc(), row["handoff_id"]))
        return self._row(row["workspace_id"], row["handoff_id"])

    def _observe(self, adapter: OcrAdapter, row):
        try:
            status = adapter.import_status(row["request_id"])
            return self._save(row, status), status, True
        except OcrUnavailable:
            previous = json.loads(row["last_status_json"]) if row["last_status_json"] else None
            return row, previous, False

    def begin(self, workspace_id: str, link_id: str, asset_set_id: str, revision: int):
        with self.lock:
            context = self._context(workspace_id, link_id)
            manifest = self._manifest(workspace_id, asset_set_id, revision)
            digest = hashlib.sha256(_json(manifest).encode()).hexdigest()
            identity = _json([workspace_id, link_id, asset_set_id, revision]).encode()
            key = hashlib.sha256(identity).hexdigest()
            handoff_id, request_id = "h" + key[:32], "r" + key[:32]
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM ocr_handoff WHERE handoff_id=?", (handoff_id,)).fetchone()
                if row is None:
                    stamp = _utc()
                    db.execute("INSERT INTO ocr_handoff VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (handoff_id, workspace_id, link_id, asset_set_id, revision,
                                digest, request_id, _json(manifest), None, stamp, stamp))
                    row = db.execute("SELECT * FROM ocr_handoff WHERE handoff_id=?", (handoff_id,)).fetchone()
            if row["manifest_hash"] != digest:
                raise AssetSetConflict("The handoff input manifest changed")
            return self._continue(context, row)

    def _continue(self, context, row):
        adapter = self._adapter(context)
        manifest = json.loads(row["manifest_json"])
        asset_set = self.sets.get(row["workspace_id"], row["asset_set_id"], row["asset_set_revision"])
        revisions = {item.item_id: item.asset_revision for item in asset_set.items}
        try:
            status = adapter.import_status(row["request_id"])
        except OcrUnavailable:
            try:
                status = adapter.begin_import(row["request_id"], manifest)
            except OcrImportConflict:
                raise AssetSetConflict("The native request identity conflicts") from None
            except OcrUnavailable:
                row, status, reachable = self._observe(adapter, row)
                return self._format(row, status, reachable=reachable)
        canonical = _json(manifest).encode()
        if (status["payload_hash"] != hashlib.sha256(canonical).hexdigest()
                or [item["item_id"] for item in status["items"]]
                != [item["item_id"] for item in manifest["items"]]):
            raise AssetSetConflict("Native import manifest differs")
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
            except OcrUnavailable:
                row, status, reachable = self._observe(adapter, row)
                if not reachable or status["state"] == "unknown":
                    return self._format(row, status, reachable=reachable)
                if next(item for item in status["items"] if item["item_id"] == source["item_id"])["status"] == "pending":
                    return self._format(row, status, reachable=False)
            row = self._save(row, status)
        row, status, reachable = self._observe(adapter, row)
        return self._format(row, status, reachable=reachable)

    def resume(self, workspace_id: str, handoff_id: str):
        with self.lock:
            row = self._row(workspace_id, handoff_id)
            return self._continue(self._context(workspace_id, row["link_id"]), row)

    def status(self, workspace_id: str, handoff_id: str):
        row = self._row(workspace_id, handoff_id)
        try:
            adapter = self._adapter(self._context(workspace_id, row["link_id"]))
        except (LaunchStatus, OcrUnavailable, ValueError):
            previous = json.loads(row["last_status_json"]) if row["last_status_json"] else None
            return self._format(row, previous, reachable=False)
        row, native, reachable = self._observe(adapter, row)
        return self._format(row, native, reachable=reachable)

    def capabilities(self, workspace_id: str, handoff_id: str):
        row = self._row(workspace_id, handoff_id)
        if any(Path(item["name"]).suffix.lower() == ".pdf"
               for item in json.loads(row["manifest_json"])["items"]):
            return {"recognition": False, "reason": "native_document_flow", "engines": []}
        adapter = self._adapter(self._context(workspace_id, row["link_id"]))
        available = adapter.capabilities()
        state = adapter._request("GET", "/api/state")
        catalog = state.get("engines")
        if not isinstance(catalog, dict):
            raise OcrUnavailable()
        engines = [{"id": key, "name": details.get("name", key)} for key, details in catalog.items()
                   if isinstance(key, str) and ID.fullmatch(key)
                   and isinstance(details, dict) and isinstance(details.get("name", key), str)]
        return {"recognition": available.recognition, "reason": available.reason,
                "engines": engines if available.recognition else []}

    def submit(self, workspace_id: str, handoff_id: str, engines: list[str], *, allow_new=True, preprocess=None):
        if (not isinstance(engines, list) or not 1 <= len(engines) <= 16
                or any(not isinstance(item, str) or not ID.fullmatch(item) for item in engines)
                or len(set(engines)) != len(engines)):
            raise ValueError("Invalid OCR engine selection")
        normalized = sorted(engines)
        preprocess = preprocess or []
        from workbench_platform.jobs.native_ocr import validate_preprocess
        validate_preprocess(preprocess)
        processing = _json({'preprocess': preprocess})
        with self.lock:
            row = self._row(workspace_id, handoff_id)
            if any(Path(item["name"]).suffix.lower() == ".pdf"
                   for item in json.loads(row["manifest_json"])["items"]):
                raise AssetSetConflict("PDF uses the native document workflow")
            context = self._context(workspace_id, row["link_id"])
            adapter = self._adapter(context)
            native = adapter.import_status(row["request_id"])
            if native["state"] != "completed":
                raise AssetSetConflict("OCR import has not completed")
            version_ids = [item["target_version_id"] for item in native["items"]]
            if any(not isinstance(item, str) for item in version_ids):
                raise AssetSetConflict("Imported version is missing")
            digest = hashlib.sha256(_json([handoff_id, normalized, processing] if preprocess else [handoff_id, normalized]).encode()).hexdigest()
            request_id = "r" + digest[:32]
            with closing(self._connect()) as db:
                submission = db.execute("SELECT * FROM ocr_submission WHERE handoff_id=?",
                                        (handoff_id,)).fetchone()
                if submission is None:
                    db.execute("INSERT INTO ocr_submission(handoff_id,engines_json,request_id,version_ids_json,native_task_ids_json,platform_job_id,created_at,processing_json) VALUES(?,?,?,?,?,?,?,?)",
                               (handoff_id, _json(normalized), request_id,
                                _json(version_ids), None, None, _utc(), processing))
                    submission = db.execute("SELECT * FROM ocr_submission WHERE handoff_id=?",
                                            (handoff_id,)).fetchone()
            if (submission["engines_json"] != _json(normalized)
                    or submission['processing_json'] != processing
                    or submission["version_ids_json"] != _json(version_ids)):
                raise AssetSetConflict("OCR processing request is already bound to other inputs")
            task_ids = (json.loads(submission["native_task_ids_json"])
                        if submission["native_task_ids_json"] else None)
            if task_ids is None:
                task_ids = list(adapter.lookup_submission(request_id) or ()) if hasattr(adapter, "lookup_submission") else []
                if not task_ids:
                    if not allow_new:
                        return {"not_submitted": True}
                    if not adapter.capabilities().recognition:
                        raise OcrUnavailable()
                    task_ids = list(adapter.submit(version_ids, normalized, request_id=request_id,
                        **({'preprocess': preprocess} if preprocess else {})))
                with closing(self._connect()) as db:
                    db.execute("UPDATE ocr_submission SET native_task_ids_json=? WHERE handoff_id=?",
                               (_json(task_ids), handoff_id))
            job = self.jobs.register(workspace_id, row["link_id"], "ocr", task_ids)
            with closing(self._connect()) as db:
                db.execute("UPDATE ocr_submission SET platform_job_id=? WHERE handoff_id=?",
                           (job["platform_job_id"], handoff_id))
            return {"handoff": self.status(workspace_id, handoff_id), "job": job}

    def result_snapshot(self, workspace_id: str, handoff_id: str):
        row = self._row(workspace_id, handoff_id)
        manifest = json.loads(row["manifest_json"])
        if any(Path(item["name"]).suffix.lower() == ".pdf" for item in manifest["items"]):
            if any(Path(item["name"]).suffix.lower() != ".pdf" for item in manifest["items"]):
                raise AssetSetConflict("Mixed OCR image and PDF export is unsupported")
            return self._document_result_snapshot(workspace_id, row, manifest)
        with closing(self._connect()) as db:
            submission = db.execute("SELECT * FROM ocr_submission WHERE handoff_id=?",
                                    (handoff_id,)).fetchone()
        if (submission is None or not submission["native_task_ids_json"]
                or not submission["platform_job_id"]):
            raise AssetSetConflict("OCR task is not registered")
        context = self._context(workspace_id, row["link_id"])
        adapter = self._adapter(context)
        task_ids = json.loads(submission["native_task_ids_json"])
        batch = adapter.batch(task_ids)
        if batch.pending or not batch.succeeded:
            raise AssetSetConflict("OCR result is not final or has no successful items")
        imported = adapter.import_status(row["request_id"])
        asset_set = self.sets.get(workspace_id, row["asset_set_id"], row["asset_set_revision"])
        if (imported["state"] != "completed"
                or imported["payload_hash"] != hashlib.sha256(_json(manifest).encode()).hexdigest()
                or len(imported["items"]) != len(asset_set.items)):
            raise AssetSetConflict("OCR input mapping is incomplete")
        sources, inputs = {}, []
        for member, source, native in zip(asset_set.items, manifest["items"], imported["items"]):
            if (member.item_id != source["item_id"] or member.item_id != native["item_id"]
                    or native["status"] != "imported" or not native.get("target_asset_id")
                    or native.get("target_document_id") or
                    Path(source["name"]).suffix.lower() == ".pdf"):
                raise AssetSetConflict("OCR image mapping changed")
            asset = self.registry.get(workspace_id, member.asset_id)
            if (asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash):
                raise AssetSetConflict("OCR source version changed")
            with self.registry.stage_verified(workspace_id, asset.asset_id,
                                              revision=member.asset_revision, blob_hash=member.blob_hash):
                pass
            key = (native["target_asset_id"], native["target_version_id"])
            if key in sources:
                raise AssetSetConflict("Native OCR source identity repeated")
            sources[key] = asset
            inputs.append({"role": "image", "name": asset.original_name,
                           "asset_id": asset.asset_id, "revision": asset.revision,
                           "blob_hash": asset.blob_hash,
                           "source_locator": member.source_locator.model_dump(mode="json")})
        results = adapter.results(task_ids)
        native_tasks = {item['id']: item for item in adapter.snapshot()['tasks']}
        succeeded = {task.native_task_id: task for task in batch.tasks
                     if task.native_status == "succeeded"}
        if len(results) != len(succeeded):
            raise AssetSetConflict("OCR result coverage is incomplete")
        pinned, items = [], []
        for result in results:
            task = succeeded.pop(result.native_task_id, None)
            key = (result.image_id, native_tasks[result.native_task_id].get('input_version_id') or result.version_id)
            if (task is None or task.result_id != result.native_result_id
                    or key not in sources or result.image_id != task.image_id
                    or result.version_id != task.version_id):
                raise AssetSetConflict("OCR result refers to another input")
            revision = adapter.result_revision(result)
            asset = sources[key]
            pinned.append((result, revision))
            items.append({"native_result_id": result.native_result_id,
                          "native_task_id": result.native_task_id,
                          "native_asset_id": result.image_id,
                          "native_version_id": result.version_id,
                          "asset_id": asset.asset_id, "revision": revision,
                          "selected": result.selected})
        if succeeded:
            raise AssetSetConflict("OCR result coverage is incomplete")
        projection = self.jobs.refresh(workspace_id, self.jobs.ledger.get(
            workspace_id, submission["platform_job_id"]))
        if projection["status"] not in {"succeeded", "partial"}:
            raise AssetSetConflict("Platform OCR task is not final")
        exported, media_type = adapter.export_results(pinned)
        project = adapter.snapshot()["project"]
        if not isinstance(project.get("name"), str):
            raise OcrUnavailable()
        return {"workspace_id": workspace_id, "handoff_id": handoff_id,
                "project_id": context.module_project_id, "project_name": project["name"],
                "platform_job_id": submission["platform_job_id"],
                "native_task_ids": task_ids, "engines": json.loads(submission["engines_json"]),
                "input_set_id": asset_set.asset_set_id, "input_set_revision": asset_set.revision,
                "input_manifest_hash": asset_set.manifest_hash, "inputs": inputs,
                "items": items, "platform_job": projection,
                "export": exported, "media_type": media_type}

    def _document_result_snapshot(self, workspace_id, row, manifest):
        context = self._context(workspace_id, row["link_id"])
        adapter = self._adapter(context)
        imported = adapter.import_status(row["request_id"])
        asset_set = self.sets.get(workspace_id, row["asset_set_id"], row["asset_set_revision"])
        if (imported["state"] != "completed"
                or imported["payload_hash"] != hashlib.sha256(_json(manifest).encode()).hexdigest()
                or len(imported["items"]) != len(asset_set.items)):
            raise AssetSetConflict("OCR document mapping is incomplete")
        snapshot = adapter.snapshot()
        documents = {item["id"]: item for item in snapshot["documents"]}
        images = snapshot["images"]
        tasks = snapshot["tasks"]
        with closing(self._connect()) as db:
            page_submission = db.execute('SELECT * FROM ocr_page_submission WHERE handoff_id=?', (row['handoff_id'],)).fetchone()
        explicit = None
        requested_results = {}
        if page_submission:
            receipt = adapter.page_request(page_submission['document_id'], page_submission['request_id'])
            if not receipt['submitted'] or receipt['parameters'] != json.loads(page_submission['parameters_json']):
                raise AssetSetConflict('PDF page request identity changed')
            if any(stage['status'] != 'succeeded' for stage in receipt['stages']):
                raise AssetSetConflict('Selected PDF pages are not fully processed; read page coverage before exporting')
            explicit = {(page_submission['document_id'], stage['page_number']) for stage in receipt['stages']}
            requested_results = {(page_submission['document_id'], stage['page_number']): stage['output']['result_id'] for stage in receipt['stages']}
        sources, inputs = {}, []
        for member, source, native in zip(asset_set.items, manifest["items"], imported["items"]):
            document_id = native.get("target_document_id")
            count = native.get("page_count")
            if (member.item_id != source["item_id"] or member.item_id != native["item_id"]
                    or native["status"] != "imported" or not document_id
                    or native.get("target_asset_id") or document_id in sources
                    or type(count) is not int or not 1 <= count <= 1000
                    or native.get("page_numbers") != list(range(1, count + 1))
                    or document_id not in documents or documents[document_id].get("page_count") != count):
                raise AssetSetConflict("OCR document source identity changed")
            asset = self.registry.get(workspace_id, member.asset_id)
            if asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash:
                raise AssetSetConflict("OCR document source version changed")
            with self.registry.stage_verified(workspace_id, asset.asset_id,
                                              revision=member.asset_revision, blob_hash=member.blob_hash):
                pass
            sources[document_id] = (asset, count)
            inputs.append({"role": "document", "name": asset.original_name,
                           "asset_id": asset.asset_id, "revision": asset.revision,
                           "blob_hash": asset.blob_hash,
                           "source_locator": member.source_locator.model_dump(mode="json")})
        selected = {}
        for image in images:
            document_id = image.get("document_id")
            if document_id not in sources:
                continue
            page = image.get("page_number")
            if explicit is not None and (document_id, page) not in explicit:
                continue
            result_id = image.get("selected_result")
            if explicit is not None:
                result_id = requested_results.get((document_id, page))
                image = {**image, 'selected_result': result_id}
            if (type(page) is not int or not 1 <= page <= sources[document_id][1]
                    or (document_id, page) in selected or not result_id):
                raise AssetSetConflict("OCR document page result is missing or repeated")
            selected[(document_id, page)] = image
        expected = {(document_id, page) for document_id, (_, count) in sources.items()
                    for page in range(1, count + 1)}
        if explicit is not None:
            expected = explicit
            for source in inputs:
                source['requested_pages'] = sorted(page for document_id, page in explicit
                    if sources[document_id][0].asset_id == source['asset_id'])
        if set(selected) != expected:
            raise AssetSetConflict("OCR document pages are not fully processed")
        native_tasks, items = [], []
        for (document_id, page), image in sorted(selected.items()):
            matches = [task for task in tasks if task.get("result_id") == image["selected_result"]]
            if (len(matches) != 1 or matches[0].get("kind") != "document"
                    or matches[0].get("status") != "succeeded"
                    or matches[0].get("image_id") != image["id"]):
                raise AssetSetConflict("OCR document result belongs to another page")
            task = matches[0]
            asset, _ = sources[document_id]
            native_tasks.append(task)
            items.append({"native_result_id": task["result_id"],
                          "native_task_id": task["id"], "native_asset_id": image["id"],
                          "native_version_id": task["version_id"],
                          "native_document_id": document_id, "page_number": page,
                          "asset_id": asset.asset_id, "revision": None, "selected": True,
                          "source_locator": {"schema_version": "1.0", "kind": "page",
                                             "asset_id": asset.asset_id,
                                             "asset_revision": asset.revision,
                                             "blob_hash": asset.blob_hash,
                                             "page_number": page, "precision": "page_only"}})
        task_ids = [task["id"] for task in native_tasks]
        result_rows = adapter.results(task_ids)
        if len(result_rows) != len(native_tasks):
            raise AssetSetConflict("OCR document result coverage is incomplete")
        pinned = []
        for task, result, item in zip(native_tasks, result_rows, items):
            if (result.native_result_id != task["result_id"]
                    or result.image_id != task["image_id"]
                    or result.version_id != task["version_id"]
                    or result.locator.get("document_id") != item["native_document_id"]
                    or result.locator.get("page_number") != item["page_number"]):
                raise AssetSetConflict("OCR document result location changed")
            revision = adapter.result_revision(result)
            item["revision"] = revision
            pinned.append((result, revision))
        projection = self.jobs.register(workspace_id, row["link_id"], "ocr", task_ids)
        if projection["status"] != "succeeded":
            raise AssetSetConflict("Platform OCR document task is not final")
        exported, media_type = adapter.export_results(pinned)
        project = snapshot["project"]
        if not isinstance(project.get("name"), str):
            raise OcrUnavailable()
        return {"workspace_id": workspace_id, "handoff_id": row["handoff_id"],
                "project_id": context.module_project_id, "project_name": project["name"],
                "platform_job_id": projection["platform_job_id"],
                "native_task_ids": task_ids,
                "engines": sorted({task["engine"] for task in native_tasks}),
                "input_set_id": asset_set.asset_set_id, "input_set_revision": asset_set.revision,
                "input_manifest_hash": asset_set.manifest_hash, "inputs": inputs,
                "items": items, "platform_job": projection,
                "export": exported, "media_type": media_type}


def configure_ocr_handoff(app):
    root = os.environ.get("WORKBENCH_DATA_DIR")
    assets = getattr(app.state, "asset_library", None)
    jobs = getattr(app.state, "job_service", None)
    app.state.ocr_handoff = (OcrHandoff(Path(root).resolve() / "ocr-handoffs.sqlite3",
                                        jobs, assets[0], assets[1]) if root and assets and jobs else None)


def _service(request: Request, workspace_id: str, *, write: bool = False):
    if not (_same_loopback_origin(request) if write else _loopback_host(request)):
        raise HTTPException(403, "请从本机平台页面操作")
    if not ID.fullmatch(workspace_id):
        raise HTTPException(400, "工作空间标识无效")
    value = getattr(request.app.state, "ocr_handoff", None)
    if value is None:
        raise HTTPException(503, "OCR 交接尚未配置")
    return value


async def _body(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(400, "请提交 JSON 参数")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise HTTPException(413, "交接参数过大")
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "交接参数无效") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "交接参数无效")
    return value


def _failed(error: Exception):
    if isinstance(error, (KeyError, AssetSetMissing, ProjectionUnavailable)):
        raise HTTPException(404, "资料集或交接不存在") from None
    if isinstance(error, PermissionError):
        raise HTTPException(403, "当前项目不可导入") from None
    if isinstance(error, (LaunchStatus, OcrUnavailable)):
        raise HTTPException(503, "OCR 应用尚未就绪") from None
    if isinstance(error, (AssetUnavailable, AssetSetConflict)):
        raise HTTPException(409, "资料版本已变化或不可用") from None
    if isinstance(error, ProjectionConflict):
        raise HTTPException(409, "原生任务状态已变化，请刷新后重试") from None
    raise HTTPException(400, "交接请求无效") from None


@router.post("", status_code=201)
async def begin_handoff(workspace_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"link_id", "asset_set_id", "asset_set_revision"}:
        raise HTTPException(400, "请选择项目和资料集")
    try:
        return await run_in_threadpool(service.begin, workspace_id,
                                       payload["link_id"], payload["asset_set_id"],
                                       payload["asset_set_revision"])
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict, ProjectionUnavailable,
            PermissionError, LaunchStatus, OcrUnavailable, ValueError) as error:
        _failed(error)


@router.get("/{handoff_id}")
async def get_handoff(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.status, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict, ProjectionUnavailable,
            PermissionError, LaunchStatus, OcrUnavailable, ValueError) as error:
        _failed(error)


@router.post("/{handoff_id}/resume")
async def resume_handoff(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "续传请求不接受参数")
    try:
        return await run_in_threadpool(service.resume, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict, ProjectionUnavailable,
            PermissionError, LaunchStatus, OcrUnavailable, ValueError) as error:
        _failed(error)


@router.get("/{handoff_id}/capabilities")
async def handoff_capabilities(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id)
    try:
        return await run_in_threadpool(service.capabilities, workspace_id, handoff_id)
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict, ProjectionUnavailable,
            PermissionError, LaunchStatus, OcrUnavailable, ValueError) as error:
        _failed(error)


@router.post("/{handoff_id}/tasks", status_code=201)
async def submit_handoff_tasks(workspace_id: str, handoff_id: str, request: Request):
    service = _service(request, workspace_id, write=True)
    payload = await _body(request)
    if set(payload) != {"engines"}:
        raise HTTPException(400, "请选择识别引擎")
    try:
        return await run_in_threadpool(service.submit, workspace_id, handoff_id, payload["engines"])
    except (KeyError, AssetSetMissing, AssetUnavailable, AssetSetConflict, ProjectionConflict,
            ProjectionUnavailable, PermissionError, LaunchStatus, OcrUnavailable, ValueError) as error:
        _failed(error)
