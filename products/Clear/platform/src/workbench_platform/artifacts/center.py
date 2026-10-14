"""Publish complete data-workbench exports into a workspace artifact catalog."""

from __future__ import annotations

import hashlib
import csv
import json
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from packages.contracts.models import AssetSetItem, Counts, JobRef, SourceLocator
from workbench_platform.artifacts.store import ArtifactStore, ArtifactUnavailable
from workbench_platform.artifacts.classification_results import ClassificationResults
from workbench_platform.artifacts.ocr_results import OcrResults
from workbench_platform.assets.registry import AssetRegistry, AssetUnavailable
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.data_api.service import DataError, DataWorkbenchService


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ArtifactCenter:
    def __init__(self, root: Path, registry: AssetRegistry, sets: AssetSetStore,
                 data: DataWorkbenchService | None, classification_handoff=None,
                 ocr_handoff=None):
        root.mkdir(exist_ok=True)
        blobs = root / "blobs"
        blobs.mkdir(exist_ok=True)
        self.store = ArtifactStore(root / "artifacts.sqlite3", blobs)
        self.registry = registry
        self.sets = sets
        self.data = data
        self.classification = (ClassificationResults(self.store, classification_handoff)
                               if classification_handoff is not None else None)
        self.ocr = OcrResults(self.store, ocr_handoff) if ocr_handoff is not None else None
        self.lock = threading.RLock()
        self.batch_store = None
        self.audit_service = None
        from workbench_platform.forensics.artifacts import configure
        configure(self)
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
                CREATE TABLE IF NOT EXISTS batch_review_bundle (
                    bundle_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,batch_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,snapshot_hash TEXT NOT NULL,
                    snapshot TEXT NOT NULL,files_json TEXT NOT NULL,status TEXT NOT NULL,
                    predecessor_bundle_id TEXT,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS review_bundle_workspace
                    ON batch_review_bundle(workspace_id,status,created_at);
                CREATE TABLE IF NOT EXISTS audit_workpaper_bundle (
                    bundle_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,project_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,snapshot_hash TEXT NOT NULL,
                    snapshot TEXT NOT NULL,files_json TEXT NOT NULL,status TEXT NOT NULL,
                    predecessor_bundle_id TEXT,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_draft_bundle(bundle_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,snapshot TEXT NOT NULL,files_json TEXT NOT NULL,status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS audit_bundle_workspace
                    ON audit_workpaper_bundle(workspace_id,status,created_at);
            """)

        self.configure_native_exports()

    def _connect(self):
        db = sqlite3.connect(self.store.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def configure_native_exports(self):
        from .native_exports import configure
        configure(self)

    def publish_audit_draft(self, bundle_id, request_id, payload_hash, snapshot):
        from workbench_platform.audit.draft_artifacts import publish
        return publish(self, bundle_id, request_id, payload_hash, snapshot)

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
        from workbench_platform.multimodal.artifacts import configure as configure_manual
        configure_manual(self)
        parts = ["SELECT 'data' kind,bundle_id,created_at FROM data_artifact_bundle "
                 "WHERE workspace_id=? AND status='ready'"]
        parts.append("SELECT 'native_export' kind,bundle_id,created_at FROM native_export_bundles WHERE workspace_id=? AND json_extract(payload,'$.status')='ready'")
        parts.append("SELECT 'review' kind,bundle_id,created_at FROM batch_review_bundle WHERE workspace_id=? AND status='ready'")
        parts.append("SELECT 'audit' kind,bundle_id,created_at FROM audit_workpaper_bundle WHERE workspace_id=? AND status='ready'")
        parts.append("SELECT 'draft' kind,bundle_id,json_extract(snapshot,'$.created_at') created_at FROM audit_draft_bundle WHERE workspace_id=? AND status='ready'")
        if self.classification is not None:
            parts.append("SELECT 'classification' kind,bundle_id,created_at "
                         "FROM classification_artifact_bundle WHERE workspace_id=? AND status='ready'")
        if self.ocr is not None:
            parts.append("SELECT 'ocr' kind,bundle_id,created_at FROM ocr_artifact_bundle "
                         "WHERE workspace_id=? AND status='ready'")
        parts.append("SELECT 'forensics' kind,bundle_id,created_at FROM forensic_artifact_bundle WHERE workspace_id=? AND status='ready'")
        parts.append("SELECT 'manual' kind,bundle_id,created_at FROM manual_artifact_bundle WHERE workspace_id=? AND status='ready'")
        union = " UNION ALL ".join(parts)
        params = (workspace_id,) * len(parts)
        with closing(self._connect()) as db:
            total = db.execute(f"SELECT COUNT(*) FROM ({union})", params).fetchone()[0]
            keys = db.execute(f"{union} ORDER BY created_at DESC,bundle_id DESC LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
        items = []
        for row in keys:
            bundle_id = row["bundle_id"]
            if row['kind'] == 'manual':
                from workbench_platform.multimodal.artifacts import get as get_manual
                items.append(get_manual(self, workspace_id, bundle_id))
            elif row["kind"] == "forensics":
                from workbench_platform.forensics.artifacts import get
                items.append(get(self, workspace_id, bundle_id))
            elif row['kind'] == 'native_export':
                from .native_exports import get as get_native
                items.append(get_native(self, workspace_id, bundle_id))
            elif row['kind'] == 'draft':
                from workbench_platform.audit.draft_artifacts import row as draft_row, display
                items.append(display(self, draft_row(self, workspace_id, bundle_id)))
            elif row["kind"] == "classification":
                items.append(self.classification.get(workspace_id, bundle_id))
            elif row["kind"] == "ocr":
                items.append(self.ocr.get(workspace_id, bundle_id))
            elif row['kind'] == 'review':
                items.append(self._review_display(self._review_row(workspace_id, bundle_id)))
            elif row['kind'] == 'audit':
                items.append(self._audit_display(self._audit_row(workspace_id, bundle_id)))
            else:
                items.append(self._display(self._row(workspace_id, bundle_id)))
        return {"items": items, "total": total,
                "next_offset": offset + limit if offset + limit < total else None}

    def get(self, workspace_id: str, bundle_id: str):
        from workbench_platform.multimodal.artifacts import configure as configure_manual, get as get_manual
        configure_manual(self)
        try:
            return get_manual(self, workspace_id, bundle_id)
        except ArtifactUnavailable:
            pass
        from .native_exports import get as get_native
        try:
            return get_native(self, workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        from workbench_platform.audit.draft_artifacts import row as draft_row, display
        try:
            return display(self, draft_row(self, workspace_id, bundle_id))
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        from workbench_platform.forensics.artifacts import get
        try:
            return get(self, workspace_id, bundle_id)
        except ArtifactUnavailable:
            pass
        try:
            return self._audit_display(self._audit_row(workspace_id, bundle_id))
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        try:
            return self._review_display(self._review_row(workspace_id, bundle_id))
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        try:
            return self._display(self._row(workspace_id, bundle_id))
        except ArtifactUnavailable as error:
            if error.code != "NOT_FOUND":
                raise
        if self.classification is not None:
            try:
                return self.classification.get(workspace_id, bundle_id)
            except ArtifactUnavailable as error:
                if error.code != "NOT_FOUND":
                    raise
        if self.ocr is not None:
            return self.ocr.get(workspace_id, bundle_id)
        raise ArtifactUnavailable("NOT_FOUND")

    def file(self, workspace_id: str, bundle_id: str, name: str):
        from workbench_platform.multimodal.artifacts import configure as configure_manual, get as get_manual
        configure_manual(self)
        try:
            manual = get_manual(self, workspace_id, bundle_id)
        except ArtifactUnavailable:
            pass
        else:
            file = next((f for f in manual['files'] if f['name']==name),None)
            if file is None:raise ArtifactUnavailable('NOT_FOUND')
            return self.store.get(workspace_id,file['artifact_id'])
        from .native_exports import get as get_native
        try:
            native = get_native(self, workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        else:
            item = next((item for item in native['files'] if item['name'] == name), None)
            if item is None:
                raise ArtifactUnavailable('NOT_FOUND')
            return self.store.get(workspace_id, item['artifact_id'])
        from workbench_platform.audit.draft_artifacts import row as draft_row
        try:
            draft = draft_row(self, workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        else:
            identifier = json.loads(draft['files_json']).get(name)
            if not identifier:
                raise ArtifactUnavailable('NOT_FOUND')
            return self.store.get(workspace_id, identifier)
        from workbench_platform.forensics.artifacts import get
        try:
            bundle = get(self, workspace_id, bundle_id)
        except ArtifactUnavailable:
            pass
        else:
            item = next((file for file in bundle["files"] if file["name"] == name), None)
            if item is None:
                raise ArtifactUnavailable("NOT_FOUND")
            return self.store.get(workspace_id, item["artifact_id"])
        try:
            audit = self._audit_row(workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        else:
            artifact_id = json.loads(audit['files_json']).get(name)
            if artifact_id is None:
                raise ArtifactUnavailable('NOT_FOUND')
            return self.store.get(workspace_id, artifact_id)
        try:
            review = self._review_row(workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != 'NOT_FOUND':
                raise
        else:
            identity = json.loads(review['files_json']).get(name)
            if identity is None:
                raise ArtifactUnavailable('NOT_FOUND')
            return self.store.get(workspace_id, identity)
        try:
            row = self._row(workspace_id, bundle_id)
        except ArtifactUnavailable as error:
            if error.code != "NOT_FOUND":
                raise
            if self.classification is not None:
                try:
                    return self.classification.file(workspace_id, bundle_id, name)
                except ArtifactUnavailable as native_error:
                    if native_error.code != "NOT_FOUND":
                        raise
            if self.ocr is not None:
                return self.ocr.file(workspace_id, bundle_id, name)
            raise ArtifactUnavailable("NOT_FOUND") from None
        artifact_id = json.loads(row["files_json"]).get(name)
        if artifact_id is None:
            raise ArtifactUnavailable("NOT_FOUND")
        return self.store.get(workspace_id, artifact_id)

    def _review_row(self, workspace_id, bundle_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM batch_review_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'",
                             (workspace_id, bundle_id)).fetchone()
        if not row:
            raise ArtifactUnavailable('NOT_FOUND')
        return row

    def _review_display(self, row):
        snapshot = json.loads(row['snapshot'])
        batch = snapshot['batch']
        status = 'outdated'
        if self.batch_store is not None:
            if self.batch_store.review_delivery_current(row['batch_id'], row['bundle_id'], row['snapshot_hash']):
                status = 'current'
        with closing(self._connect()) as db:
            successor_count = db.execute("SELECT count(*) FROM batch_review_bundle WHERE predecessor_bundle_id=? AND workspace_id=? "
                "AND batch_id=? AND status='ready'", (row['bundle_id'], row['workspace_id'], row['batch_id'])).fetchone()[0]
            successors = [item[0] for item in db.execute("SELECT bundle_id FROM batch_review_bundle WHERE predecessor_bundle_id=? "
                "AND workspace_id=? AND batch_id=? AND status='ready' ORDER BY created_at,bundle_id LIMIT 100",
                (row['bundle_id'], row['workspace_id'], row['batch_id']))]
        files = []
        for name, identity in json.loads(row['files_json']).items():
            artifact = self.store.get(row['workspace_id'], identity)
            files.append({'name': name, 'artifact_id': identity, 'revision': artifact.revision,
                'media_type': artifact.media_type, 'size_bytes': artifact.size_bytes,
                'availability': artifact.availability, 'blob_hash': artifact.blob_hash})
        unresolved = [{'item_id': item['item_id'], 'category': item['category'], 'disposition': item['disposition'],
                       'title': item.get('title', item['category'])} for item in snapshot['items'] if item['disposition'] != 'confirmed']
        return {'bundle_id': row['bundle_id'], 'workspace_id': row['workspace_id'], 'kind': 'batch_review',
            'batch_id': row['batch_id'], 'batch_revision': batch['revision'], 'delivery_kind': snapshot['request']['kind'],
            'predecessor_bundle_id': row['predecessor_bundle_id'], 'successor_bundle_ids': successors,
            'successor_count': successor_count,
            'status': status, 'request_id': row['request_id'], 'payload_hash': row['payload_hash'],
            'snapshot_hash': row['snapshot_hash'], 'title': batch['template']['name'] + ' · 复核成果',
            'created_at': row['created_at'], 'files': files, 'inputs': batch['inputs'], 'coverage': batch['coverage'],
            'unresolved_count': len(unresolved), 'unresolved_preview': unresolved[:30],
            'execution_status': batch['status'], 'pending_operations': batch['summary'].get('pending_operations', 0)}

    def publish_batch_review(self, operation):
        """A frozen request has one bundle; only fully verified bundles are visible."""
        bundle_id = operation['bundle_id']
        snapshot = json.loads(operation['snapshot'])
        batch = snapshot['batch']
        workspace_id = batch['workspace_id']
        with self.store.publication_lock(bundle_id):
            with closing(self._connect()) as db:
                row = db.execute('SELECT * FROM batch_review_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            if row is not None:
                if (row['payload_hash'], row['snapshot_hash']) != (operation['payload_hash'], operation['snapshot_hash']):
                    raise ArtifactUnavailable('INPUT_CHANGED')
                if row['status'] == 'ready':
                    return self._review_display(row)
                snapshot = json.loads(row['snapshot'])
            else:
                originals = []
                for original in batch['artifacts']:
                    if original.get('kind') == 'batch_review':
                        continue
                    bundle = self.get(workspace_id, original['bundle_id'])
                    descriptors = []
                    for file in bundle['files']:
                        artifact = self.file(workspace_id, original['bundle_id'], file['name'])
                        descriptors.append({'name': file['name'], 'artifact_id': artifact.artifact_id,
                            'revision': artifact.revision, 'blob_hash': artifact.blob_hash, 'size_bytes': artifact.size_bytes,
                            'media_type': artifact.media_type})
                    originals.append({'bundle_id': original['bundle_id'], 'files': descriptors})
                snapshot['original_bundles'] = originals
                with closing(self._connect()) as db:
                    db.execute('INSERT INTO batch_review_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (bundle_id, workspace_id, batch['batch_id'], operation['request_id'], operation['payload_hash'],
                         operation['snapshot_hash'], _json(snapshot), '{}', 'publishing', snapshot['predecessor_bundle_id'], _utc()))
                with closing(self._connect()) as db:
                    row = db.execute('SELECT * FROM batch_review_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            root = self.store.blob_root.parent / 'review-staging'
            root.mkdir(exist_ok=True)
            prepared = root / bundle_id
            with tempfile.TemporaryDirectory(prefix=bundle_id + '-', dir=root) as temporary:
                directory = Path(temporary)
                originals = {}
                result_rows = None
                for position, original in enumerate(snapshot['original_bundles'], 1):
                    for file in original['files']:
                        name = f"original-{position}-{file['name']}"
                        path = directory / name
                        if prepared.exists():
                            shutil.copyfile(prepared / name, path)
                        else:
                            with self.store.stage_download(workspace_id, file['artifact_id'], expected_revision=file['revision'],
                                                           expected_hash=file['blob_hash']) as pinned:
                                shutil.copyfile(pinned, path)
                        originals[name] = path
                        if batch['template']['scenario'] == 'data_compare' and file['name'] == 'results.csv':
                            with path.open(encoding='utf-8-sig', newline='') as stream:
                                result_rows = [{'ordinal': index, **record} for index, record in enumerate(csv.DictReader(stream))]
                if result_rows is None:
                    result_rows = [item['raw_output'] for item in snapshot['items'] if 'ordinal' in item['raw_output']]
                if snapshot['request']['kind'] == 'final' and len(result_rows) != batch['coverage']['result_rows']:
                    raise DataError('复核成果缺少固定全量结果，不能发布正式成果', 409)
                from modules.data_workbench.exporting.results import render_review_export, _digest
                # Preserve the exact verified bytes before the first artifact
                # insertion. XLSX ZIP timestamps must never change a retry's
                # content identity after a partial metadata publication.
                if not prepared.exists():
                    files = {**render_review_export(directory, snapshot, result_rows), **originals}
                    manifest = {name: {'sha256': _digest(path), 'bytes': path.stat().st_size} for name, path in files.items()}
                    (directory / 'prepared.json').write_text(_json(manifest), encoding='utf-8')
                    os.replace(directory, prepared)
                manifest = json.loads((prepared / 'prepared.json').read_text(encoding='utf-8'))
                files = {name: prepared / name for name in manifest}
                for name, path in files.items():
                    if (_digest(path), path.stat().st_size) != (manifest[name]['sha256'], manifest[name]['bytes']):
                        raise ArtifactUnavailable('CORRUPT')
                refs = [SourceLocator(schema_version='1.0', kind='file', asset_id=item['asset_id'],
                    asset_revision=item['asset_revision'], blob_hash=item['blob_hash'], precision='exact') for item in batch['inputs']]
                for item in batch['inputs']:
                    with self.registry.stage_verified(workspace_id, item['asset_id'], revision=item['asset_revision'], blob_hash=item['blob_hash']):
                        pass
                job = JobRef(schema_version='1.0', platform_job_id=bundle_id,
                    app_id='data' if batch['template']['scenario'] == 'data_compare' else batch['template']['scenario'],
                    instance_id='platform_review', native_job_id=bundle_id, operation_id=bundle_id,
                    input_manifest_hash=hashlib.sha256(_json(batch['inputs']).encode()).hexdigest(),
                    native_status=batch['status'], status='succeeded', stage='review_export',
                    counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
                    capabilities=[], last_event_seq=0, updated_at=_utc())
                mapping = json.loads(row['files_json'])
                for name, path in files.items():
                    digest, size = _digest(path), path.stat().st_size
                    if name in mapping:
                        artifact = self.store.get(workspace_id, mapping[name])
                        if (artifact.blob_hash, artifact.size_bytes) != (digest, size):
                            raise ArtifactUnavailable('INPUT_CHANGED')
                        with self.store.stage_download(workspace_id, artifact.artifact_id,
                                expected_revision=artifact.revision, expected_hash=digest):
                            pass
                        continue
                    with path.open('rb') as stream:
                        def register_mapping(db, artifact):
                            saved = db.execute("SELECT files_json,status FROM batch_review_bundle WHERE bundle_id=?", (bundle_id,)).fetchone()
                            if saved is None or saved['status'] != 'publishing':
                                raise ArtifactUnavailable('INPUT_CHANGED')
                            bindings = json.loads(saved['files_json'])
                            if name in bindings:
                                raise ArtifactUnavailable('INPUT_CHANGED')
                            bindings[name] = artifact.artifact_id
                            db.execute('UPDATE batch_review_bundle SET files_json=? WHERE bundle_id=?', (_json(bindings), bundle_id))
                        artifact = self.store.publish_managed(workspace_id, stream, expected_hash=digest, expected_size=size,
                            job_ref=job, source_refs=refs, media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream',
                            application_version='0.3.0', rule_or_model_version='platform-review-v2', on_insert=register_mapping)
                    mapping[name] = artifact.artifact_id
                with closing(self._connect()) as db:
                    db.execute("UPDATE batch_review_bundle SET status='ready' WHERE bundle_id=?", (bundle_id,))
            return self._review_display(self._review_row(workspace_id, bundle_id))

    def _audit_row(self, workspace_id, bundle_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM audit_workpaper_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'",
                             (workspace_id, bundle_id)).fetchone()
        if row is None:
            raise ArtifactUnavailable('NOT_FOUND')
        return row

    def _audit_display(self, row):
        from packages.contracts.audit import WorkpaperBundle
        snapshot = json.loads(row['snapshot'])
        project = snapshot['project']
        current = self.audit_service is not None and self.audit_service.workpaper_current(
            row['project_id'], row['bundle_id'], snapshot)
        with closing(self._connect()) as db:
            args = (row['bundle_id'], row['workspace_id'], row['project_id'])
            successor_count = db.execute("SELECT count(*) FROM audit_workpaper_bundle WHERE predecessor_bundle_id=? "
                "AND workspace_id=? AND project_id=? AND status='ready'", args).fetchone()[0]
            successors = [item[0] for item in db.execute("SELECT bundle_id FROM audit_workpaper_bundle WHERE predecessor_bundle_id=? "
                "AND workspace_id=? AND project_id=? AND status='ready' ORDER BY created_at,bundle_id LIMIT 100", args)]
        files = []
        for name, artifact_id in json.loads(row['files_json']).items():
            artifact = self.store.get(row['workspace_id'], artifact_id)
            files.append({'name': name, 'artifact_id': artifact_id, 'revision': artifact.revision,
                          'media_type': artifact.media_type, 'size_bytes': artifact.size_bytes,
                          'availability': artifact.availability, 'blob_hash': artifact.blob_hash})
        return WorkpaperBundle.model_validate({'schema_version': '1.0', 'audit_version': '1.0',
            'workpaper_id': row['bundle_id'], 'bundle_id': row['bundle_id'], 'workspace_id': row['workspace_id'],
            'project_id': row['project_id'], 'project_revision': project['revision'], 'kind': 'audit_workpaper',
            'delivery_kind': snapshot['request']['kind'], 'status': 'current' if current else 'outdated',
            'request_id': row['request_id'], 'payload_hash': row['payload_hash'], 'snapshot_hash': row['snapshot_hash'],
            'actor': snapshot['request']['actor'], 'title': (project['project']['title'][:190] + ' · 审计底稿'),
            'created_at': row['created_at'], 'predecessor_workpaper_id': row['predecessor_bundle_id'],
            'successor_workpaper_ids': successors, 'successor_count': successor_count, 'files': files,
            'review_refs': snapshot['review_refs'],
            'forensic_refs': [ref for finding in snapshot['findings'] for ref in finding['evidence_refs'] if ref.get('scenario') == 'image_forensics'],
            'unresolved_count': snapshot['unresolved_count'],
            'unresolved_reasons': snapshot['unresolved_reasons'][:30]}).model_dump(mode='json')

    def publish_audit_workpaper(self, operation):
        """Reuse the existing managed artifact publication and verified downloads."""
        import io
        from workbench_platform.audit.render import render_workpaper
        bundle_id, workspace_id = operation['workpaper_id'], operation['workspace_id']
        snapshot = operation['snapshot']
        with self.store.publication_lock(bundle_id):
            with closing(self._connect()) as db:
                row = db.execute('SELECT * FROM audit_workpaper_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            if row:
                if (row['payload_hash'], row['snapshot_hash']) != (operation['payload_hash'], operation['snapshot_hash']):
                    raise ArtifactUnavailable('INPUT_CHANGED')
                if row['status'] == 'ready':
                    return self._audit_display(row)
            else:
                with closing(self._connect()) as db:
                    db.execute('INSERT INTO audit_workpaper_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (bundle_id, workspace_id, operation['project_id'], operation['request_id'], operation['payload_hash'],
                         operation['snapshot_hash'], _json(snapshot), '{}', 'publishing',
                         snapshot['predecessor_workpaper_id'], operation['created_at']))
                with closing(self._connect()) as db:
                    row = db.execute('SELECT * FROM audit_workpaper_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            files = render_workpaper(operation)
            refs = []
            for evidence in snapshot['evidence']:
                value = evidence['snapshot']
                sources = value.get('source_assets') or value['sources']
                for source in sources:
                    if source not in refs:
                        refs.append(source)
            refs = [SourceLocator.model_validate(source) for source in refs]
            job = JobRef(schema_version='1.0', platform_job_id=bundle_id, app_id='data',
                instance_id='platform_audit', native_job_id=bundle_id, operation_id=bundle_id,
                input_manifest_hash=operation['snapshot_hash'], native_status='succeeded', status='succeeded',
                stage='audit_workpaper', counts=Counts(schema_version='1.0', total=1, succeeded=1,
                    failed=0, cancelled=0, pending=0), capabilities=[], last_event_seq=0,
                updated_at=operation['created_at'])
            mapping = json.loads(row['files_json'])
            for name, payload in files.items():
                digest = hashlib.sha256(payload).hexdigest()
                if name in mapping:
                    artifact = self.store.get(workspace_id, mapping[name])
                    if (artifact.blob_hash, artifact.size_bytes) != (digest, len(payload)):
                        raise ArtifactUnavailable('INPUT_CHANGED')
                    with self.store.stage_download(workspace_id, artifact.artifact_id,
                            expected_revision=artifact.revision, expected_hash=digest):
                        pass
                    continue
                def register_mapping(db, artifact):
                    saved = db.execute('SELECT files_json,status FROM audit_workpaper_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
                    if saved is None or saved['status'] != 'publishing':
                        raise ArtifactUnavailable('INPUT_CHANGED')
                    bindings = json.loads(saved['files_json'])
                    if name in bindings:
                        raise ArtifactUnavailable('INPUT_CHANGED')
                    bindings[name] = artifact.artifact_id
                    db.execute('UPDATE audit_workpaper_bundle SET files_json=? WHERE bundle_id=?', (_json(bindings), bundle_id))
                artifact = self.store.publish_managed(workspace_id, io.BytesIO(payload), expected_hash=digest,
                    expected_size=len(payload), job_ref=job, source_refs=refs,
                    media_type='text/markdown' if name.endswith('.md') else 'application/json',
                    application_version='0.4.0', rule_or_model_version='audit-workpaper-v1', on_insert=register_mapping)
                mapping[name] = artifact.artifact_id
            with closing(self._connect()) as db:
                db.execute("UPDATE audit_workpaper_bundle SET status='ready' WHERE bundle_id=?", (bundle_id,))
            return self._audit_display(self._audit_row(workspace_id, bundle_id))

    def publish_classification_result(self, workspace_id: str, handoff_id: str):
        if self.classification is None:
            raise ArtifactUnavailable("SOURCE_UNAVAILABLE")
        return self.classification.publish(workspace_id, handoff_id)

    def publish_ocr_result(self, workspace_id: str, handoff_id: str):
        if self.ocr is None:
            raise ArtifactUnavailable("SOURCE_UNAVAILABLE")
        return self.ocr.publish(workspace_id, handoff_id)

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
        project_id = self.data.get(session_id).get('workspace_id')
        if project_id is not None and project_id != workspace_id:
            raise DataError('该输出属于其他项目', 409)
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
