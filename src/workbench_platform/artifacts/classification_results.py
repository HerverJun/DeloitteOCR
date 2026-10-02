"""Immutable classification result snapshots in the shared artifact store."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from packages.contracts.models import Counts, JobRef
from workbench_platform.artifacts.store import ArtifactStore, ArtifactUnavailable


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _csv_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


class ClassificationResults:
    def __init__(self, store: ArtifactStore, handoff):
        self.store, self.handoff = store, handoff
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS classification_artifact_bundle (
                    bundle_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    handoff_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL, platform_job_id TEXT NOT NULL,
                    native_job_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    input_set_id TEXT NOT NULL, input_set_revision INTEGER NOT NULL,
                    result_digest TEXT NOT NULL, input_json TEXT NOT NULL,
                    files_json TEXT NOT NULL, status TEXT NOT NULL
                        CHECK(status IN ('publishing','ready')),
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id,handoff_id,native_job_id,result_digest));
                CREATE INDEX IF NOT EXISTS classification_bundle_workspace
                    ON classification_artifact_bundle(workspace_id,status,created_at);
            """)

    def _connect(self):
        db = sqlite3.connect(self.store.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _row(self, workspace_id, bundle_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM classification_artifact_bundle WHERE workspace_id=? "
                             "AND bundle_id=?", (workspace_id, bundle_id)).fetchone()
        if row is None or row["status"] != "ready":
            raise ArtifactUnavailable("NOT_FOUND")
        return row

    def display(self, row):
        mapping = json.loads(row["files_json"])
        files = []
        for name, artifact_id in mapping.items():
            artifact = self.store.get(row["workspace_id"], artifact_id)
            files.append({"name": name, "artifact_id": artifact_id,
                          "revision": artifact.revision, "media_type": artifact.media_type,
                          "size_bytes": artifact.size_bytes,
                          "availability": artifact.availability})
        return {"bundle_id": row["bundle_id"], "workspace_id": row["workspace_id"],
                "kind": "classification_result", "title": row["project_name"],
                "project_id": row["project_id"], "handoff_id": row["handoff_id"],
                "platform_job_id": row["platform_job_id"],
                "native_job_id": row["native_job_id"], "model_id": row["model_id"],
                "created_at": row["created_at"], "input_set_id": row["input_set_id"],
                "input_set_revision": row["input_set_revision"],
                "inputs": json.loads(row["input_json"]), "files": files}

    def get(self, workspace_id, bundle_id):
        return self.display(self._row(workspace_id, bundle_id))

    def file(self, workspace_id, bundle_id, name):
        row = self._row(workspace_id, bundle_id)
        artifact_id = json.loads(row["files_json"]).get(name)
        if artifact_id is None:
            raise ArtifactUnavailable("NOT_FOUND")
        return self.store.get(workspace_id, artifact_id)

    @staticmethod
    def _files(snapshot):
        report = {"schema_version": "1.0", "platform_job_id": snapshot["platform_job_id"],
                  "native_job_id": snapshot["native_job_id"], "model_id": snapshot["model_id"],
                  "native_state": snapshot["native_state"],
                  "counts": snapshot["platform_job"]["counts"],
                  "items": snapshot["items"]}
        provenance = {"schema_version": "1.0", "input_set_id": snapshot["input_set_id"],
                      "input_set_revision": snapshot["input_set_revision"],
                      "input_manifest_hash": snapshot["input_manifest_hash"],
                      "inputs": snapshot["inputs"]}
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        columns = ("source_name", "asset_id", "native_asset_id", "state",
                   "prediction_label_id", "prediction_status", "prediction_score",
                   "human_label_id", "disposition_state", "disposition_revision",
                   "disposition_note", "current_annotation_label_id", "error")
        writer.writerow(columns)
        for item in snapshot["items"]:
            writer.writerow([_csv_cell(item.get(column)) for column in columns])
        return {"classification-results.json": (_canonical(report), "application/json"),
                "classification-results.csv": (('\ufeff' + stream.getvalue()).encode("utf-8"), "text/csv"),
                "provenance.json": (_canonical(provenance), "application/json")}

    @staticmethod
    def _job(snapshot):
        projection = snapshot["platform_job"]
        counts = projection["counts"]
        return JobRef(schema_version="1.0", platform_job_id=snapshot["platform_job_id"],
                      app_id="classification", instance_id=projection["instance_id"],
                      native_job_id=snapshot["native_job_id"],
                      operation_id=projection["operation_id"],
                      input_manifest_hash=projection["input_manifest_hash"],
                      native_status=projection["native_status"], status=projection["status"],
                      stage=projection["stage"],
                      counts=Counts(schema_version="1.0", **counts), capabilities=[],
                      last_event_seq=projection["last_event_seq"],
                      updated_at=projection["updated_at"])

    def publish(self, workspace_id, handoff_id, *, snapshot=None):
        with self.lock:
            snapshot = snapshot or self.handoff.result_snapshot(workspace_id, handoff_id)
            if snapshot.get("workspace_id") != workspace_id or snapshot.get("handoff_id") != handoff_id:
                raise ArtifactUnavailable("SOURCE_FAILED")
            payloads = self._files(snapshot)
            identity = {"job": snapshot["native_job_id"], "model": snapshot["model_id"],
                        "inputs": snapshot["input_manifest_hash"], "items": snapshot["items"],
                        "state": snapshot["native_state"]}
            digest = hashlib.sha256(_canonical(identity)).hexdigest()
            with closing(self._connect()) as db:
                row = db.execute("SELECT * FROM classification_artifact_bundle "
                                 "WHERE workspace_id=? AND handoff_id=? AND native_job_id=? "
                                 "AND result_digest=?", (workspace_id, handoff_id,
                                                        snapshot["native_job_id"], digest)).fetchone()
            if row is None:
                bundle_id = "b" + uuid.uuid4().hex
                from datetime import datetime, timezone
                created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                with closing(self._connect()) as db:
                    db.execute("INSERT INTO classification_artifact_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (bundle_id, workspace_id, handoff_id, snapshot["project_id"],
                                snapshot["project_name"], snapshot["platform_job_id"],
                                snapshot["native_job_id"], snapshot["model_id"],
                                snapshot["input_set_id"], snapshot["input_set_revision"],
                                digest, json.dumps(snapshot["inputs"], ensure_ascii=False),
                                "{}", "publishing", created_at))
                    row = db.execute("SELECT * FROM classification_artifact_bundle WHERE bundle_id=?",
                                     (bundle_id,)).fetchone()
            if row["status"] == "ready":
                return self.display(row)
            job_ref = self._job(snapshot)
            locators = [item["source_locator"] for item in snapshot["inputs"]]
            mapping = json.loads(row["files_json"])
            for name, (data, media_type) in payloads.items():
                if name in mapping:
                    continue
                artifact = self.store.publish_managed(
                    workspace_id, io.BytesIO(data), expected_hash=hashlib.sha256(data).hexdigest(),
                    expected_size=len(data), job_ref=job_ref, source_refs=locators,
                    media_type=media_type, application_version="0.2.0",
                    rule_or_model_version=snapshot["model_id"])
                mapping[name] = artifact.artifact_id
                with closing(self._connect()) as db:
                    db.execute("UPDATE classification_artifact_bundle SET files_json=? WHERE bundle_id=?",
                               (json.dumps(mapping, sort_keys=True), row["bundle_id"]))
            with closing(self._connect()) as db:
                db.execute("UPDATE classification_artifact_bundle SET status='ready' WHERE bundle_id=?",
                           (row["bundle_id"],))
            return self.get(workspace_id, row["bundle_id"])
