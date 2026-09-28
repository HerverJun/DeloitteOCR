"""Publish complete data-workbench exports into a workspace artifact catalog."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from packages.contracts.models import AssetSetItem, Counts, JobRef, SourceLocator
from workbench_platform.artifacts.store import ArtifactStore, ArtifactUnavailable
from workbench_platform.artifacts.classification_results import ClassificationResults
from workbench_platform.assets.registry import AssetRegistry, AssetUnavailable
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.data_api.service import DataError, DataWorkbenchService


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ArtifactCenter:
    def __init__(self, root: Path, registry: AssetRegistry, sets: AssetSetStore,
                 data: DataWorkbenchService | None, classification_handoff=None):
        root.mkdir(exist_ok=True)
        blobs = root / "blobs"
        blobs.mkdir(exist_ok=True)
        self.store = ArtifactStore(root / "artifacts.sqlite3", blobs)
        self.registry = registry
        self.sets = sets
        self.data = data
        self.classification = (ClassificationResults(self.store, classification_handoff)
                               if classification_handoff is not None else None)
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS data_artifact_bundle (
                    bundle_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, session_name TEXT NOT NULL,
                    export_id TEXT NOT NULL,
                    result_id TEXT, export_fingerprint TEXT NOT NULL,
                    asset_set_id TEXT NOT NULL, asset_set_revision INTEGER NOT NULL,
                    input_json TEXT NOT NULL, files_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('publishing','ready')),
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, session_id, export_id));
                CREATE INDEX IF NOT EXISTS data_bundle_workspace
                    ON data_artifact_bundle(workspace_id, status, created_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.store.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _row(self, workspace_id: str, bundle_id: str):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM data_artifact_bundle WHERE workspace_id=? AND bundle_id=?",
                             (workspace_id, bundle_id)).fetchone()
        if row is None or row["status"] != "ready":
            raise ArtifactUnavailable("NOT_FOUND")
        return row

    def _display(self, row):
        mapping = json.loads(row["files_json"])
        files = []
        for name, artifact_id in mapping.items():
            artifact = self.store.get(row["workspace_id"], artifact_id)
            files.append({"name": name, "artifact_id": artifact_id,
                          "revision": artifact.revision, "media_type": artifact.media_type,
                          "size_bytes": artifact.size_bytes, "availability": artifact.availability})
        return {"bundle_id": row["bundle_id"], "workspace_id": row["workspace_id"],
                "kind": "data_comparison" if row["result_id"] else "data_cleaned",
                "title": row["session_name"],
                "session_id": row["session_id"], "session_name": row["session_name"],
                "export_id": row["export_id"],
                "result_id": row["result_id"], "created_at": row["created_at"],
                "input_set_id": row["asset_set_id"],
                "input_set_revision": row["asset_set_revision"],
                "inputs": json.loads(row["input_json"]), "files": files}

    def list(self, workspace_id: str, offset: int, limit: int):
        if self.classification is not None:
            with closing(self._connect()) as db:
                total = db.execute("SELECT (SELECT COUNT(*) FROM data_artifact_bundle "
                                   "WHERE workspace_id=? AND status='ready') + "
                                   "(SELECT COUNT(*) FROM classification_artifact_bundle "
                                   "WHERE workspace_id=? AND status='ready')",
                                   (workspace_id, workspace_id)).fetchone()[0]
                keys = db.execute("SELECT 'data' kind,bundle_id,created_at "
                                  "FROM data_artifact_bundle WHERE workspace_id=? AND status='ready' "
                                  "UNION ALL SELECT 'classification' kind,bundle_id,created_at "
                                  "FROM classification_artifact_bundle WHERE workspace_id=? AND status='ready' "
                                  "ORDER BY created_at DESC,bundle_id DESC LIMIT ? OFFSET ?",
                                  (workspace_id, workspace_id, limit, offset)).fetchall()
            items = [(self._display(self._row(workspace_id, row["bundle_id"]))
                      if row["kind"] == "data" else
                      self.classification.get(workspace_id, row["bundle_id"])) for row in keys]
            return {"items": items, "total": total,
                    "next_offset": offset + limit if offset + limit < total else None}
        with closing(self._connect()) as db:
            total = db.execute("SELECT COUNT(*) FROM data_artifact_bundle WHERE workspace_id=? "
                               "AND status='ready'", (workspace_id,)).fetchone()[0]
            rows = db.execute("SELECT * FROM data_artifact_bundle WHERE workspace_id=? "
                              "AND status='ready' ORDER BY created_at DESC,bundle_id DESC "
                              "LIMIT ? OFFSET ?", (workspace_id, limit, offset)).fetchall()
        return {"items": [self._display(row) for row in rows], "total": total,
                "next_offset": offset + limit if offset + limit < total else None}

    def get(self, workspace_id: str, bundle_id: str):
        try:
            return self._display(self._row(workspace_id, bundle_id))
        except ArtifactUnavailable as error:
            if error.code != "NOT_FOUND" or self.classification is None:
                raise
            return self.classification.get(workspace_id, bundle_id)

    def file(self, workspace_id: str, bundle_id: str, name: str):
        try:
            row = self._row(workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != "NOT_FOUND" or self.classification is None:
                raise
            return self.classification.file(workspace_id, bundle_id, name)
        artifact_id = json.loads(row["files_json"]).get(name)
        if artifact_id is None:
            raise ArtifactUnavailable("NOT_FOUND")
        return self.store.get(workspace_id, artifact_id)

    def publish_classification_result(self, workspace_id: str, handoff_id: str):
        if self.classification is None:
            raise ArtifactUnavailable("SOURCE_UNAVAILABLE")
        return self.classification.publish(workspace_id, handoff_id)

    def _inputs(self, workspace_id: str, export: dict, summary: dict, session_id: str):
        if export["result_id"]:
            if summary.get("comparison_id") != export["result_id"]:
                raise DataError("成果版本与摘要不一致", 409)
            sources = [("left", summary.get("left_source")),
                       ("right", summary.get("right_source"))]
        else:
            sources = [("table", summary.get("source"))]
        items, inputs = [], []
        for role, source in sources:
            if (not isinstance(source, dict) or not isinstance(source.get("asset_id"), str)
                    or not isinstance(source.get("sha256"), str)):
                raise DataError("成果来源清单不完整", 409)
            upload = self.data.upload_source(session_id, source["asset_id"])
            if (upload["sha256"] != source["sha256"]
                    or upload["format"] != source.get("format")):
                raise DataError("成果来源与原始输入不一致", 409)
            if upload["source_asset_id"]:
                if (upload["source_workspace_id"] != workspace_id
                        or upload["source_blob_hash"] != upload["sha256"]):
                    raise DataError("成果来源与资料库身份不一致", 409)
                asset = self.registry.get(workspace_id, upload["source_asset_id"])
                if (asset.revision != upload["source_asset_revision"]
                        or asset.blob_hash != upload["source_blob_hash"]):
                    raise DataError("资料库输入版本已变化", 409)
                with self.registry.stage_verified(workspace_id, asset.asset_id,
                                                  revision=asset.revision, blob_hash=asset.blob_hash):
                    pass
            else:
                media = mimetypes.guess_type(upload["name"])[0] or "application/octet-stream"
                asset = self.registry.register(workspace_id, "data", upload["registry_path"],
                                               media_type=media, storage_mode="managed",
                                               original_name=upload["name"])
            if asset.blob_hash != upload["sha256"] or asset.size_bytes != upload["size"]:
                raise DataError("成果来源内容已变化", 409)
            if source["format"] == "xlsx":
                sheet = source.get("sheet_name")
                if not isinstance(sheet, str) or not sheet:
                    raise DataError("工作表来源缺失", 409)
                locator = SourceLocator(schema_version="1.0", kind="table",
                                        asset_id=asset.asset_id, asset_revision=asset.revision,
                                        blob_hash=asset.blob_hash, sheet_name=sheet,
                                        table_number=1, precision="exact")
            else:
                locator = SourceLocator(schema_version="1.0", kind="file",
                                        asset_id=asset.asset_id, asset_revision=asset.revision,
                                        blob_hash=asset.blob_hash, precision="exact")
            items.append(AssetSetItem(schema_version="1.0", item_id="i" + uuid.uuid4().hex,
                                      asset_id=asset.asset_id, asset_revision=asset.revision,
                                      blob_hash=asset.blob_hash, source_locator=locator))
            inputs.append({"role": role, "name": asset.original_name,
                           "asset_id": asset.asset_id, "revision": asset.revision,
                           "blob_hash": asset.blob_hash, "source_locator": locator.model_dump(mode="json")})
        return items, inputs

    def publish_data_export(self, workspace_id: str, session_id: str, export_id: str):
        if self.data is None:
            raise DataError("数据工作台尚未配置", 503)
        with self.lock:
            export = self.data.export_info(session_id, export_id)
            paths = {}
            for file in export["files"]:
                path = self.data.download(session_id, export_id, file["name"])
                if path.stat().st_size != file["bytes"]:
                    raise DataError("成果文件大小已变化", 409)
                paths[file["name"]] = path
            try:
                summary = json.loads(paths["summary.json"].read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                raise DataError("成果摘要不可读取", 409) from None
            fingerprint = hashlib.sha256(_json({"export": export, "summary": summary}).encode()).hexdigest()
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM data_artifact_bundle WHERE workspace_id=? "
                                 "AND session_id=? AND export_id=?",
                                 (workspace_id, session_id, export_id)).fetchone()
            if row is not None and row["export_fingerprint"] != fingerprint:
                raise DataError("已归集成果的内容版本发生变化", 409)
            if row is None:
                session_name = self.data.get(session_id)["name"]
                items, inputs = self._inputs(workspace_id, export, summary, session_id)
                asset_set = self.sets.create(workspace_id, items)
                bundle_id = "b" + uuid.uuid4().hex
                with closing(self._connect()) as db:
                    db.execute("INSERT INTO data_artifact_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (bundle_id, workspace_id, session_id, session_name, export_id,
                                export["result_id"], fingerprint, asset_set.asset_set_id,
                                asset_set.revision, _json(inputs), "{}", "publishing", _utc()))
                with closing(self._connect()) as db:
                    row = db.execute("SELECT * FROM data_artifact_bundle WHERE bundle_id=?",
                                     (bundle_id,)).fetchone()
            if row["status"] == "ready":
                return self._display(row)
            asset_set = self.sets.get(workspace_id, row["asset_set_id"], row["asset_set_revision"])
            inputs = json.loads(row["input_json"])
            refs = [item["source_locator"] for item in inputs]
            job = JobRef(schema_version="1.0", platform_job_id=export_id, app_id="data",
                         instance_id="data_local", native_job_id=export_id,
                         operation_id=export_id, input_manifest_hash=asset_set.manifest_hash,
                         native_status="succeeded", status="succeeded", stage="export",
                         counts=Counts(schema_version="1.0", total=1, succeeded=1,
                                       failed=0, cancelled=0, pending=0),
                         capabilities=[], last_event_seq=0, updated_at=_utc())
            mapping = json.loads(row["files_json"])
            for file in export["files"]:
                name = file["name"]
                if name in mapping:
                    continue
                with paths[name].open("rb") as stream:
                    artifact = self.store.publish_managed(
                        workspace_id, stream, expected_hash=file["sha256"],
                        expected_size=file["bytes"], job_ref=job, source_refs=refs,
                        media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                        application_version="0.1.0", rule_or_model_version="data-export-v1")
                mapping[name] = artifact.artifact_id
                with closing(self._connect()) as db:
                    db.execute("UPDATE data_artifact_bundle SET files_json=? WHERE bundle_id=?",
                               (_json(mapping), row["bundle_id"]))
            with closing(self._connect()) as db:
                db.execute("UPDATE data_artifact_bundle SET status='ready' WHERE bundle_id=?",
                           (row["bundle_id"],))
            return self.get(workspace_id, row["bundle_id"])
