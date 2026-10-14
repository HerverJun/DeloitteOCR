"""Immutable, source-linked OCR exports in the shared artifact catalog."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone

from packages.contracts.models import Counts, JobRef
from workbench_platform.artifacts.store import ArtifactStore, ArtifactUnavailable


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _content_digest(data: bytes, media_type: str) -> str:
    if media_type != "application/zip":
        return hashlib.sha256(data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [(name, hashlib.sha256(archive.read(name)).hexdigest())
                 for name in sorted(archive.namelist()) if not name.endswith("/")]
    return hashlib.sha256(_canonical(files)).hexdigest()


class OcrResults:
    def __init__(self, store: ArtifactStore, handoff):
        self.store, self.handoff = store, handoff
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ocr_artifact_bundle (
                    bundle_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    handoff_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL, platform_job_id TEXT NOT NULL,
                    input_set_id TEXT NOT NULL, input_set_revision INTEGER NOT NULL,
                    result_digest TEXT NOT NULL, input_json TEXT NOT NULL,
                    items_json TEXT NOT NULL, files_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('publishing','ready')),
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id,handoff_id,result_digest));
                CREATE INDEX IF NOT EXISTS ocr_bundle_workspace
                    ON ocr_artifact_bundle(workspace_id,status,created_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.store.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _row(self, workspace_id, bundle_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM ocr_artifact_bundle WHERE workspace_id=? "
                             "AND bundle_id=?", (workspace_id, bundle_id)).fetchone()
        if row is None or row["status"] != "ready":
            raise ArtifactUnavailable("NOT_FOUND")
        return row

    def display(self, row):
        files = []
        for name, artifact_id in json.loads(row["files_json"]).items():
            artifact = self.store.get(row["workspace_id"], artifact_id)
            files.append({"name": name, "artifact_id": artifact_id,
                          "revision": artifact.revision, "media_type": artifact.media_type,
                          "size_bytes": artifact.size_bytes,
                          "availability": artifact.availability})
        return {"bundle_id": row["bundle_id"], "workspace_id": row["workspace_id"],
                "kind": "ocr_result", "title": row["project_name"],
                "project_id": row["project_id"], "handoff_id": row["handoff_id"],
                "platform_job_id": row["platform_job_id"],
                "created_at": row["created_at"], "input_set_id": row["input_set_id"],
                "input_set_revision": row["input_set_revision"],
                "inputs": json.loads(row["input_json"]),
                "items": json.loads(row["items_json"]), "files": files}

    def get(self, workspace_id, bundle_id):
        return self.display(self._row(workspace_id, bundle_id))

    def file(self, workspace_id, bundle_id, name):
        row = self._row(workspace_id, bundle_id)
        artifact_id = json.loads(row["files_json"]).get(name)
        if artifact_id is None:
            raise ArtifactUnavailable("NOT_FOUND")
        return self.store.get(workspace_id, artifact_id)

    @staticmethod
    def _job(snapshot):
        projection = snapshot["platform_job"]
        return JobRef(schema_version="1.0", platform_job_id=snapshot["platform_job_id"],
                      app_id="ocr", instance_id=projection["instance_id"],
                      native_job_id=snapshot["native_task_ids"][0],
                      operation_id=projection["operation_id"],
                      input_manifest_hash=projection["input_manifest_hash"],
                      native_status=projection["native_status"], status=projection["status"],
                      stage=projection["stage"] or projection["status"],
                      counts=Counts(schema_version="1.0", **projection["counts"]),
                      capabilities=[], last_event_seq=projection["last_event_seq"],
                      updated_at=projection["updated_at"])

    def publish(self, workspace_id, handoff_id, *, snapshot=None):
        with self.lock:
            snapshot = snapshot or self.handoff.result_snapshot(workspace_id, handoff_id)
            if snapshot.get("workspace_id") != workspace_id or snapshot.get("handoff_id") != handoff_id:
                raise ArtifactUnavailable("SOURCE_FAILED")
            archive_name = ("ocr-results.zip" if snapshot["media_type"] == "application/zip"
                            else "ocr-result.json")
            provenance = {"schema_version": "1.0", "platform_job_id": snapshot["platform_job_id"],
                          "native_task_ids": snapshot["native_task_ids"],
                          "input_set_id": snapshot["input_set_id"],
                          "input_set_revision": snapshot["input_set_revision"],
                          "input_manifest_hash": snapshot["input_manifest_hash"],
                          "inputs": snapshot["inputs"], "results": snapshot["items"]}
            payloads = {archive_name: (snapshot["export"], snapshot["media_type"]),
                        "provenance.json": (_canonical(provenance), "application/json")}
            identity = {"results": snapshot["items"],
                        "content": _content_digest(snapshot["export"], snapshot["media_type"]),
                        "provenance": hashlib.sha256(payloads["provenance.json"][0]).hexdigest()}
            digest = hashlib.sha256(_canonical(identity)).hexdigest()
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM ocr_artifact_bundle WHERE workspace_id=? "
                                 "AND handoff_id=? AND result_digest=?",
                                 (workspace_id, handoff_id, digest)).fetchone()
                if row is None:
                    bundle_id = "b" + uuid.uuid4().hex
                    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    db.execute("INSERT INTO ocr_artifact_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (bundle_id, workspace_id, handoff_id, snapshot["project_id"],
                                snapshot["project_name"], snapshot["platform_job_id"],
                                snapshot["input_set_id"], snapshot["input_set_revision"],
                                digest, json.dumps(snapshot["inputs"], ensure_ascii=False),
                                json.dumps(snapshot["items"], ensure_ascii=False), "{}",
                                "publishing", stamp))
                    row = db.execute("SELECT * FROM ocr_artifact_bundle WHERE bundle_id=?",
                                     (bundle_id,)).fetchone()
            if row["status"] == "ready":
                return self.display(row)
            job_ref = self._job(snapshot)
            locators = ([item["source_locator"] for item in snapshot["inputs"]]
                        + [item["source_locator"] for item in snapshot["items"]
                           if "source_locator" in item])
            mapping = json.loads(row["files_json"])
            for name, (data, media_type) in payloads.items():
                if name in mapping:
                    continue
                artifact = self.store.publish_managed(
                    workspace_id, io.BytesIO(data), expected_hash=hashlib.sha256(data).hexdigest(),
                    expected_size=len(data), job_ref=job_ref, source_refs=locators,
                    media_type=media_type, application_version="0.2.0",
                    rule_or_model_version=",".join(snapshot["engines"]))
                mapping[name] = artifact.artifact_id
                with closing(self._connect()) as db:
                    db.execute("UPDATE ocr_artifact_bundle SET files_json=? WHERE bundle_id=?",
                               (json.dumps(mapping, sort_keys=True), row["bundle_id"]))
            with closing(self._connect()) as db:
                db.execute("UPDATE ocr_artifact_bundle SET status='ready' WHERE bundle_id=?",
                           (row["bundle_id"],))
            return self.get(workspace_id, row["bundle_id"])
