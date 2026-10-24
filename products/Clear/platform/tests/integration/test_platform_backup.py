"""A stopped platform workspace reopens from a verified new-directory restore."""

import hashlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import shutil

import pytest
from fastapi.testclient import TestClient

from packages.contracts.models import Counts, JobRef
from scripts.backup_workspace import create, restore, verify
from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_platform_backup_restores_assets_and_managed_result(tmp_path, monkeypatch):
    source, archive, recovered = (tmp_path / name for name in ("source", "archive", "recovered"))
    source.mkdir()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(source))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "恢复检查"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{workspace}/assets"
        asset = client.post(base + "/uploads?name=来源.txt", headers=LOCAL,
                            content="备份前原件".encode()).json()
        fixed = client.post(base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        payload = b"immutable-result"
        job = JobRef(schema_version="1.0", platform_job_id="jobA", app_id="ocr",
                     instance_id="instanceA", native_job_id="nativeA",
                     operation_id="operationA", input_manifest_hash="a" * 64,
                     native_status="succeeded", status="succeeded", stage="complete",
                     counts=Counts(schema_version="1.0", total=1, succeeded=1,
                                   failed=0, cancelled=0, pending=0), capabilities=[],
                     last_event_seq=1, updated_at="2026-09-28T00:00:00Z")
        locator = {"schema_version": "1.0", "kind": "file", "asset_id": asset["asset_id"],
                   "asset_revision": 1, "blob_hash": asset["blob_hash"], "precision": "exact"}
        artifact = app.state.artifact_center.store.publish_managed(
            workspace, io.BytesIO(payload), expected_hash=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload), job_ref=job, source_refs=[locator],
            media_type="text/plain", application_version="0.2.0",
            rule_or_model_version="fixture")
    manifest = create(source, archive)
    assert "assets.sqlite3" in manifest["files"]
    assert "artifact-center/artifacts.sqlite3" in manifest["files"]
    assert "ocr-handoffs.sqlite3" in manifest["files"]
    assert verify(archive)["files"] == manifest["files"]
    restore(archive, recovered)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(recovered))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        base = f"/api/platform/v1/workspaces/{workspace}/assets"
        assert client.get(base + "/sets/" + fixed["asset_set_id"]).json()["revision"] == 1
        original = client.get(base + "/" + asset["asset_id"] + "/content?revision=1")
        assert original.status_code == 200 and original.content == "备份前原件".encode()
        stored = app.state.artifact_center.store.get(workspace, artifact.artifact_id)
        with app.state.artifact_center.store.stage_download(
                workspace, stored.artifact_id, expected_revision=stored.revision,
                expected_hash=stored.blob_hash) as path:
            assert path.read_bytes() == payload
    with pytest.raises(ValueError, match="new directory"):
        restore(archive, recovered)
    corrupt = next((archive / "asset-blobs").rglob("*"))
    if corrupt.is_dir():
        corrupt = next(corrupt.rglob("*"))
    corrupt.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="changed"):
        verify(archive)


def test_ocr_cold_backup_keeps_project_files_and_omits_runtime_state(tmp_path):
    source, archive, recovered = (tmp_path / name for name in ("ocr", "backup", "recovered"))
    (source / "projects" / "projectA").mkdir(parents=True)
    (source / "projects" / "projectA" / "source.pdf").write_bytes(b"pdf-source")
    (source / "pdf-ipc").mkdir()
    (source / "pdf-ipc" / "request.json").write_text("temporary", encoding="utf-8")
    (source / "service.lock").write_bytes(b"0")
    with sqlite3.connect(source / "workbench.sqlite3") as db:
        db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO projects VALUES('projectA', 'Original')")
    manifest = create(source, archive, kind="ocr")
    assert manifest["kind"] == "ocr"
    assert "projects/projectA/source.pdf" in manifest["files"]
    assert "service.lock" not in manifest["files"]
    assert "pdf-ipc/request.json" not in manifest["files"]
    assert verify(archive)["kind"] == "ocr"
    restore(archive, recovered)
    with sqlite3.connect(recovered / "workbench.sqlite3") as db:
        assert db.execute("SELECT name FROM projects WHERE id='projectA'").fetchone()[0] == "Original"
    assert (recovered / "projects/projectA/source.pdf").read_bytes() == b"pdf-source"
    assert not (recovered / "service.lock").exists()


@pytest.mark.parametrize('kind,database_name', [('ocr', 'workbench.sqlite3'), ('platform', 'assets.sqlite3')])
def test_cold_backup_reads_committed_residual_wal_without_changing_source(tmp_path, kind, database_name, record_property):
    source, archive, recovered = (tmp_path / name for name in ('wal-source', 'wal-archive', 'wal-restored'))
    source.mkdir()
    database = source / database_name
    # Exit without sqlite close, as after a stopped/crashed writer. The baseline
    # is checkpointed; the second committed row exists only in the residual WAL.
    script = '''import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT)')
db.execute("INSERT INTO projects VALUES('baseline','checkpointed')")
db.commit()
db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
db.execute("INSERT INTO projects VALUES('wal-only','committed before writer exit')")
db.commit()
os._exit(0)
'''
    subprocess.run([sys.executable, '-I', '-B', '-u', '-X', 'utf8', '-c', script, str(database)],
                   cwd=source, check=True, timeout=20,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    wal = database.with_name(database.name + '-wal')
    assert wal.is_file() and wal.stat().st_size > 0
    if kind == 'platform':
        with closing(sqlite3.connect(source / 'workspaces.sqlite3')) as db:
            db.execute('CREATE TABLE workspaces(id TEXT PRIMARY KEY)')
            db.commit()
    # A standalone copy of only the DB proves the latest row needs the WAL.
    baseline = tmp_path / 'main-file-only.sqlite3'
    shutil.copyfile(database, baseline)
    with closing(sqlite3.connect(baseline.as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('SELECT id FROM projects').fetchall() == [('baseline',)]

    def identity(path):
        stat = path.stat()
        return {'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    before = {'database': identity(database), 'wal': identity(wal)}
    record_property('source_persistent_before', json.dumps(before, sort_keys=True))
    try:
        manifest = create(source, archive, kind=kind)
    finally:
        after = {'database': identity(database), 'wal': identity(wal) if wal.exists() else None}
        record_property('source_persistent_after', json.dumps(after, sort_keys=True))
    # SHM is transient reader coordination and is deliberately not claimed
    # byte-identical. DB and WAL are the persistent source content checked here.
    assert after == before
    assert verify(archive) == manifest
    restore(archive, recovered)
    with closing(sqlite3.connect((recovered / database_name).as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('SELECT id,name FROM projects ORDER BY id').fetchall() == [
            ('baseline', 'checkpointed'), ('wal-only', 'committed before writer exit')]
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not any(name.endswith(('.sqlite3-wal', '.sqlite3-shm')) for name in manifest['files'])


def test_cold_backup_corrupt_source_still_fails_without_publication_or_source_change(tmp_path):
    source, archive = tmp_path / 'corrupt-source', tmp_path / 'unpublished-backup'
    source.mkdir()
    database = source / 'workbench.sqlite3'
    original = b'not a SQLite database; preserve this failed source'
    database.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        create(source, archive, kind='ocr')
    assert database.read_bytes() == original
    assert not archive.exists()
    assert not list(tmp_path.glob('unpublished-backup.pending-*'))


def test_cold_backup_still_rejects_a_genuine_source_change(tmp_path, monkeypatch):
    from scripts import backup_workspace as backup_module
    source, archive = tmp_path / 'changed-source', tmp_path / 'unpublished-backup'
    source.mkdir()
    database = source / 'workbench.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY)')
        db.commit()
    original = database.read_bytes()
    check_database = backup_module._check_db

    def external_change_after_copy(target):
        check_database(target)
        with database.open('ab') as writer:
            writer.write(b'external modification during the cold backup')

    monkeypatch.setattr(backup_module, '_check_db', external_change_after_copy)
    with pytest.raises(ValueError, match='Source changed during backup'):
        create(source, archive, kind='ocr')
    assert database.read_bytes() == original + b'external modification during the cold backup'
    assert not archive.exists()
    assert not list(tmp_path.glob('unpublished-backup.pending-*'))


def test_platform_cold_backup_keeps_batch_templates_receipts_and_decisions(tmp_path, monkeypatch):
    from workbench_platform.batches.store import BatchStore

    source, archive, recovered = (tmp_path / name for name in ("batch-source", "batch-archive", "batch-recovered"))
    source.mkdir()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(source))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        assert client.get("/api/platform/v1/health").status_code == 200
    store = BatchStore(source / "batches")
    template = store.save_template({"schema_version": "1.0", "workspace_id": "workspaceA",
                                   "name": "月度核对", "scenario": "data_compare",
                                   "config": {"policy": "fixed-original"}})
    request = {"schema_version": "1.0", "request_key": "requestA", "workspace_id": "workspaceA",
               "template_id": template["template_id"], "template_version": template["version"],
               "inputs": [{"role": "left", "asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}]}
    batch, _ = store.accept(request, template)
    store.update(batch["batch_id"], status="succeeded", receipts={"export": {"job_id": "fixedExportA"}},
                 issues=[{"item_id": "itemA", "raw_output": {"identifier": "00000000000000000001"}, "disposition": "pending"}])
    store.disposition(batch["batch_id"], {'request_id': 'backupReview', 'expected_revision': 1,
        'actor': {'id': 'localReviewer', 'display_name': '本机复核人', 'source': 'local'},
        'item_ids': ['itemA'], 'action': 'defer', 'note': '仍需业务解释'})
    manifest = create(source, archive)
    assert "batches/batches.sqlite3" in manifest["files"]
    restore(archive, recovered)
    reopened = BatchStore(recovered / "batches")
    assert reopened.template("workspaceA", template["template_id"], 1) == template
    persisted = reopened.get(batch["batch_id"])
    assert persisted["receipts"] == {"export": {"job_id": "fixedExportA"}}
    assert persisted["issues"][0]["raw_output"]["identifier"] == "00000000000000000001"
    assert persisted["issues"][0]["disposition"] == "deferred"
    assert persisted["issues"][0]["note"] == "仍需业务解释"
    with sqlite3.connect(reopened.path) as db:
        assert db.execute("SELECT action,note FROM disposition_events").fetchall() == [("defer", "仍需业务解释")]


def test_platform_cold_restore_keeps_disposition_ledger_adoption_and_unknown_origin(tmp_path, monkeypatch):
    from workbench_platform.batches.store import BatchStore, present_batch
    from workbench_platform.data_api.service import DataError

    source, archive, recovered = (tmp_path / name for name in ("ledger-source", "ledger-archive", "ledger-recovered"))
    source.mkdir()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(source))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        assert client.get("/api/platform/v1/health").status_code == 200
    store = BatchStore(source / "batches")
    template = store.save_template({"schema_version": "1.0", "workspace_id": "workspaceA",
        "name": "固定分类原件", "scenario": "classification", "config": {"link_id": "projectA"}})
    batch, _ = store.accept({"schema_version": "1.0", "workspace_id": "workspaceA", "request_key": "restoreRun",
        "template_id": template["template_id"], "template_version": template["version"],
        "inputs": [{"role": "images", "asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}]}, template)
    batch_id = batch["batch_id"]
    original = {"item_id": "itemA", "category": "review",
        "source": {"result_id": "originalResultA", "asset_id": "assetA", "blob_hash": "a" * 64},
        "raw_output": {"ordinal": 0, "prediction_label_id": "A", "native_revision": 1,
                       "native_item_id": "itemA", "identifier": "00000000000000000001"},
        "disposition": "pending", "note": ""}
    store.begin_index(batch_id, "originalResultA", 1, 1, 1, 0)
    store.index_page(batch_id, "originalResultA", 0, [{"ordinal": 0}], [original])
    bundle = source / "batches" / "data" / "original-A.zip"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"original-export-A-before-human-B")
    original_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    store.update(batch_id, status="succeeded", artifacts=[{"bundle_id": "originalBundleA", "sha256": original_hash}],
        receipts={"results": {"result_id": "originalResultA", "original_sha256": "a" * 64}})
    actor = {"id": "localReviewer", "display_name": "本机复核人", "source": "local"}
    adopt_payload = {"request_id": "restoreAdopt", "expected_revision": 1, "actor": actor,
        "item_ids": ["itemA"], "action": "adopt", "note": "采用所见 B 修订",
        "adoption": {"native_revision": 2, "human_label_id": "B"}}
    origin = {"instance_id": "instanceA", "project_id": "projectA", "native_job_id": "jobA",
              "original_sha256": "a" * 64}
    adopt = store.begin_operation(batch_id, adopt_payload, origin=origin)
    # The native receipt is a storage fixture. Native execution is verified by
    # the adapter/bridge tests, while this scenario checks cold persistence.
    native_receipt = {"operation_id": adopt["operation_id"], "instance_id": "instanceA", "state": "committed",
        "project_id": "projectA", "native_revision": 3, "human_label_id": "B", "payload_sha256": "b" * 64}
    store.checkpoint_operation(adopt["operation_id"], "native_committed", native_receipt=native_receipt,
        intent={**adopt["intent"], "native_started": True})
    adoption = {"native_revision": 2, "committed_native_revision": 3,
                "human_label_id": "B", "native_receipt": native_receipt}
    committed = store.commit_operation(adopt["operation_id"], adoption=adoption)
    unknown_payload = {"request_id": "restoreUnknown", "expected_revision": 2, "actor": actor,
                       "item_ids": ["itemA"], "action": "reopen", "note": "原生响应失联"}
    unknown = store.begin_operation(batch_id, unknown_payload, origin=origin)
    saved_unknown = store.checkpoint_operation(unknown["operation_id"], "needs_verification",
        reason="原实例回执尚不可核实", intent={**unknown["intent"], "native_started": True,
        "segments": [{"operation_id": unknown["operation_id"] + ":0", "item_ids": ["itemA"], "payload_sha256": "c" * 64}]})
    old_history = store.history(batch_id)
    with sqlite3.connect(store.path) as db:
        original_body = db.execute("SELECT original_body FROM batch_items WHERE batch_id=?", (batch_id,)).fetchone()[0]
    manifest = create(source, archive)
    assert verify(archive) == manifest
    assert manifest["files"]["batches/data/original-A.zip"]["sha256"] == original_hash
    assert "batches/batches.sqlite3" in manifest["files"]
    restore(archive, recovered)
    reopened = BatchStore(recovered / "batches")
    repeated = BatchStore(recovered / "batches")
    assert reopened.template("workspaceA", template["template_id"], 1) == template
    replay = reopened.begin_operation(batch_id, adopt_payload, origin={**origin, "instance_id": "instanceB"})
    assert replay["operation_id"] == adopt["operation_id"] and replay["intent"]["origin"] == origin
    assert reopened.commit_operation(replay["operation_id"]) == committed
    assert reopened.operation(unknown["operation_id"], private=True) == saved_unknown
    assert reopened.history(batch_id) == repeated.history(batch_id) == old_history
    event = old_history["items"][0]
    assert event["actor"] == actor and event["revision"] == 2 and event["legacy"] is False
    assert event["previous_state"]["raw_output"]["prediction_label_id"] == "A"
    assert event["adoption"] == adoption and event["current_state"]["adoption"] == adoption
    item = reopened.selected_items(batch_id, ["itemA"])[0]
    assert item["raw_output"] == original["raw_output"] and item["adoption"] == adoption
    snapshot, counts, _ = reopened.snapshot(batch_id)
    displayed = present_batch(snapshot, counts)
    assert displayed["revision"] == 2 and displayed["coverage"]["confirmed"] == 1
    assert displayed["coverage"]["index_complete"] is True and displayed["summary"]["review_complete"] is False
    assert displayed["summary"]["pending_operation"]["operation_id"] == unknown["operation_id"]
    with pytest.raises(DataError, match="待核实"):
        reopened.begin_operation(batch_id, {**unknown_payload, "request_id": "restoreBlindRetry"},
                                 origin={**origin, "instance_id": "instanceB"})
    with sqlite3.connect(reopened.path) as db:
        assert db.execute("SELECT original_body FROM batch_items WHERE batch_id=?", (batch_id,)).fetchone()[0] == original_body
        assert db.execute("SELECT count(*) FROM batch_disposition_history").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM batch_disposition_operations").fetchone()[0] == 2
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256((recovered / "batches" / "data" / "original-A.zip").read_bytes()).hexdigest() == original_hash


def test_three_component_cold_restore_keeps_real_native_revisions_and_unknown_operation(tmp_path, monkeypatch):
    """Data-level cold fixture; D07 review publication/HTTP belongs to final D11."""
    from PIL import Image
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.artifacts.store import ArtifactStore
    from workbench_platform.batches.store import BatchStore, present_batch
    from workbench_platform.data_api.service import DataError
    from workbench_platform.store import WorkspaceStore
    from workbench_platform.supervisor.processes import require_f_path

    platform_source = Path(__file__).resolve().parents[2]
    ocr_source, class_source = platform_source.parent / 'ocr', platform_source.parent / 'classification'
    monkeypatch.syspath_prepend(str(ocr_source / 'src'))
    monkeypatch.syspath_prepend(str(class_source / 'src'))
    from ocr_workbench import store as ocr_module
    from ocr_workbench.imaging import add_image
    from image_platform import store as class_module
    from image_platform import backup as class_backup
    from image_platform.datasets import register_asset
    from image_platform.settings import Settings
    assert Path(ocr_module.__file__).resolve().is_relative_to(ocr_source.resolve())
    assert Path(class_module.__file__).resolve().is_relative_to(class_source.resolve())
    assert Path(class_backup.__file__).resolve().is_relative_to(class_source.resolve())
    require_f_path(tmp_path, 'Three-component fixture')
    roots = {name: tmp_path / ('source-' + name) for name in ('platform', 'ocr', 'classification')}
    actor = {'id': 'coldReviewer', 'display_name': '本机冷备复核人', 'source': 'local'}
    original = tmp_path / 'external-original.png'
    Image.new('RGB', (24, 16), 'white').save(original)
    original_bytes = original.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()

    ocr = ocr_module.Store(roots['ocr'])
    project = ocr.project('真实 Store 冷备项目')
    import_temporary = tmp_path / 'ocr-import-temporary.png'
    import_temporary.write_bytes(original_bytes)
    image = add_image(ocr, project['id'], original.name, import_temporary)
    assert not import_temporary.exists() and original.read_bytes() == original_bytes
    task_id = ocr.enqueue(project['id'], [image['active_version']], ['ppocr'], request_id='cold_ocr_submit')[0]
    assert ocr.claim()['id'] == task_id
    raw = {'text': '原始 00000000000000000001', 'tables': [], 'blocks': [], 'engine': 'ppocr'}
    assert ocr.complete(task_id, raw)  # Fixed engineering output; no engine is run.
    result_id = ocr.one('tasks', task_id)['result_id']
    ocr.save(result_id, {'text': '第一次人工校对', 'tables': []}, 0)
    ocr.save(result_id, {'text': '第二次人工校对', 'tables': []}, 1)
    fixed_snapshots = [ocr.result_snapshot(result_id, revision) for revision in range(3)]

    classifier = class_module.Store(Settings(roots['classification']))
    with classifier.connect(write=True) as db:
        db.execute('INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)',
                   ('coldProject', '分类冷备', class_module.dump([
                       {'label_id': 'a', 'display_name': 'A'}, {'label_id': 'b', 'display_name': 'B'}]), class_module.now()))
    native_asset, _ = register_asset(classifier, 'coldProject', original, None)
    native_job = classifier.submit('coldProject', 'inference', {'model_id': 'frozenModel'}, [native_asset])
    prediction = {'label_id': 'a', 'score': 0.8, 'status': 'needs_review'}
    with classifier.connect(write=True) as db:
        db.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (native_job,))
        db.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (class_module.dump(prediction), native_job))
    native_item = classifier.one('SELECT id FROM job_items WHERE job_id=?', (native_job,))['id']
    classifier.dispose_results(native_job, [native_item], 'confirmed', 'b', '人工改判 B',
        {native_item: 0}, operation_id='coldManualB', request_id='coldManualB', actor=actor, instance_id='instanceA')
    dataset = roots['classification'] / 'datasets/fixed.json'
    dataset.write_text(class_module.dump({'items': [dict(native_asset, split='train')]}), encoding='utf-8')
    model = roots['classification'] / 'models/frozenModel'
    model.mkdir()
    (model / 'model.onnx').write_bytes(b'path-relocation-only-model-fixture')
    with classifier.connect(write=True) as db:
        db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)', ('frozenDataset', 'coldProject', '固定来源',
            '[]', str(dataset), hashlib.sha256(dataset.read_bytes()).hexdigest(), '{}', class_module.now()))
        db.execute('INSERT INTO models VALUES(?,?,?,?,?,?,?,?)', ('frozenModel', 'coldProject', native_job,
            '固定路径夹具', str(model), '{}', '{}', class_module.now()))

    for name in ('asset-uploads', 'asset-blobs', 'artifact-center/blobs'):
        (roots['platform'] / name).mkdir(parents=True)
    workspace = WorkspaceStore(roots['platform'] / 'workspaces.sqlite3').create_workspace('一致冷备')
    (roots['platform'] / 'asset-uploads/source.png').write_bytes(original_bytes)
    registry = AssetRegistry(roots['platform'] / 'assets.sqlite3', roots['platform'] / 'asset-blobs',
                             {'uploads': roots['platform'] / 'asset-uploads'})
    asset = registry.register(workspace.workspace_id, 'uploads', 'source.png', media_type='image/png', storage_mode='managed')
    ledger = BatchStore(roots['platform'] / 'batches')

    def batch_fixture(scenario, item_id, native_output, native_project):
        template = ledger.save_template({'schema_version': '1.0', 'workspace_id': workspace.workspace_id,
            'name': scenario, 'scenario': scenario, 'config': {'link_id': native_project}})
        batch, _ = ledger.accept({'schema_version': '1.0', 'workspace_id': workspace.workspace_id,
            'request_key': 'cold' + scenario, 'template_id': template['template_id'], 'template_version': template['version'],
            'inputs': [{'role': 'images' if scenario == 'classification' else 'documents', 'asset_id': asset.asset_id,
                        'asset_revision': asset.revision, 'blob_hash': original_hash}]}, template)
        item = {'item_id': item_id, 'category': 'review', 'title': '冷备复核', 'detail': '固定来源',
            'source': {'result_id': result_id if scenario == 'ocr' else native_job, 'asset_id': asset.asset_id,
                       'blob_hash': original_hash}, 'raw_output': {'ordinal': 0, **native_output}, 'disposition': 'pending', 'note': ''}
        ledger.begin_index(batch['batch_id'], scenario + 'FixedResult', 1, 1, 1, 0)
        ledger.index_page(batch['batch_id'], scenario + 'FixedResult', 0, [{'ordinal': 0}], [item])
        ledger.update(batch['batch_id'], status='succeeded')
        return batch, item

    class_batch, class_item = batch_fixture('classification', 'classColdItem',
        {'native_item_id': native_item, 'prediction_label_id': 'a', 'native_revision': 0}, 'coldProject')
    ocr_batch, ocr_item = batch_fixture('ocr', 'ocrColdItem',
        {'native_result_id': result_id, 'native_revision': 0, 'text': raw['text']}, project['id'])
    class_payload = {'request_id': 'coldClassAdopt', 'expected_revision': 1, 'actor': actor,
        'item_ids': [class_item['item_id']], 'action': 'adopt', 'note': '采用 B',
        'adoption': {'native_revision': 1, 'human_label_id': 'b'}}
    class_origin = {'instance_id': 'instanceA', 'project_id': 'coldProject', 'native_job_id': native_job,
                    'original_sha256': original_hash}
    class_operation = ledger.begin_operation(class_batch['batch_id'], class_payload, origin=class_origin)
    class_arguments = dict(operation_id=class_operation['operation_id'], request_id=class_payload['request_id'],
        actor=actor, instance_id='instanceA', intent='adopt')
    class_receipt = classifier.dispose_results(native_job, [native_item], 'confirmed', 'b', class_payload['note'],
                                               {native_item: 1}, **class_arguments)
    class_adoption = {'native_revision': 1, 'committed_native_revision': 2, 'human_label_id': 'b', 'native_receipt': class_receipt}
    ledger.checkpoint_operation(class_operation['operation_id'], 'native_committed', native_receipt=class_receipt)
    class_committed = ledger.commit_operation(class_operation['operation_id'], adoption=class_adoption)

    def ocr_payload(request_id, revision):
        return {'request_id': request_id, 'expected_revision': revision, 'actor': actor,
            'item_ids': [ocr_item['item_id']], 'action': 'adopt', 'note': '采用第二次校对',
            'adoption': {'native_revision': 2, 'native_result_id': result_id, 'native_version_id': image['active_version']}}

    def ocr_body():
        return {'project_id': project['id'], 'instance_id': 'instanceA', 'result_id': result_id,
            'task_id': task_id, 'image_id': image['id'], 'version_id': image['active_version'], 'revision': 2,
            'original_sha256': original_hash, 'actor': actor, 'note': '采用第二次校对'}

    ocr_origin = {'instance_id': 'instanceA', 'project_id': project['id'], 'native_task_id': task_id,
                  'original_sha256': original_hash}
    known_payload = ocr_payload('coldOcrAdopt', 1)
    known = ledger.begin_operation(ocr_batch['batch_id'], known_payload, origin=ocr_origin)
    known_receipt = ocr.adopt_result(known['operation_id'], ocr_body())
    adoption = {'native_revision': 2, 'native_result_id': result_id, 'native_version_id': image['active_version'],
                'snapshot': fixed_snapshots[2], 'native_receipt': known_receipt}
    ledger.checkpoint_operation(known['operation_id'], 'native_committed', native_receipt=known_receipt)
    known_committed = ledger.commit_operation(known['operation_id'], adoption=adoption)
    lost_payload = ocr_payload('coldLostReply', 2)
    lost = ledger.begin_operation(ocr_batch['batch_id'], lost_payload, origin=ocr_origin)
    lost_receipt = ocr.adopt_result(lost['operation_id'], ocr_body())
    unknown = ledger.checkpoint_operation(lost['operation_id'], 'needs_verification', reason='原生成功但响应丢失',
        intent={**lost['intent'], 'native_started': True, 'segments': [{'operation_id': lost['operation_id'],
            'item_ids': [ocr_item['item_id']], 'payload': ocr_body(), 'payload_hash': lost_receipt['payload_hash'],
            'native_started': True, 'state': 'needs_verification'}]})
    before_history = {batch['batch_id']: ledger.history(batch['batch_id']) for batch in (class_batch, ocr_batch)}
    artifact_store = ArtifactStore(roots['platform'] / 'artifact-center/artifacts.sqlite3', roots['platform'] / 'artifact-center/blobs')
    artifacts = []
    for ordinal, payload in enumerate((b'fixed-original-A', '固定采用 B 与 OCR 校对 2'.encode())):
        job = JobRef(schema_version='1.0', platform_job_id='coldJob' + str(ordinal), app_id='ocr', instance_id='instanceA',
            native_job_id=task_id, operation_id=known['operation_id'], input_manifest_hash=original_hash,
            native_status='succeeded', status='succeeded', stage='complete',
            counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
            capabilities=[], last_event_seq=1, updated_at='2026-10-03T00:00:00Z')
        artifacts.append(artifact_store.publish_managed(workspace.workspace_id, io.BytesIO(payload),
            expected_hash=hashlib.sha256(payload).hexdigest(), expected_size=len(payload), job_ref=job,
            source_refs=[{'schema_version': '1.0', 'kind': 'file', 'asset_id': asset.asset_id, 'asset_revision': 1,
                          'blob_hash': original_hash, 'precision': 'exact'}], media_type='text/plain',
            application_version='0.1.0', rule_or_model_version='cold-data-fixture'))

    # All Store calls above close their SQLite connections. No native writer or
    # queue runs; the same stopped window covers all three component snapshots.
    archive, recovered = tmp_path / 'cold-group', tmp_path / 'fresh-restored-group'
    archive.mkdir()
    manifests = {'platform': create(roots['platform'], archive / 'platform'),
                 'ocr': create(roots['ocr'], archive / 'ocr', kind='ocr'),
                 'classification': class_backup.backup(roots['classification'], archive / 'classification')}
    source_schemas = {}
    for name, database in [('ocr', 'workbench.sqlite3'), ('classification', 'platform.sqlite3')]:
        with sqlite3.connect(roots[name] / database) as db:
            source_schemas[name] = db.execute('PRAGMA user_version').fetchone()[0]
    assert source_schemas == {'ocr': 17, 'classification': 5}
    group = {'format': 1, 'mode': 'cold', 'components': {name: {'files': len(manifest['files']),
        'manifest_sha256': hashlib.sha256((archive / name / 'manifest.json').read_bytes()).hexdigest()}
        for name, manifest in manifests.items()}, 'source_schemas': source_schemas,
        'proof': {'original_sha256': original_hash, 'ocr_result_id': result_id, 'unknown_operation_id': lost['operation_id'],
                  'original_instance_id': 'instanceA', 'artifacts': [item.model_dump(mode='json') for item in artifacts]}}
    (archive / 'installation-manifest.json').write_text(json.dumps(group, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, manifest in manifests.items():
        checked = class_backup.verify(archive / name) if name == 'classification' else verify(archive / name)
        assert checked == manifest
        assert hashlib.sha256((archive / name / 'manifest.json').read_bytes()).hexdigest() == group['components'][name]['manifest_sha256']
    restored = {name: recovered / name for name in roots}
    restore(archive / 'platform', restored['platform'])
    restore(archive / 'ocr', restored['ocr'])
    class_backup.restore(archive / 'classification', restored['classification'])
    restored_ocr = ocr_module.Store(restored['ocr'])
    restored_class = class_module.Store(Settings(restored['classification']))
    reopened = BatchStore(restored['platform'] / 'batches')
    for name, database, version in [('ocr', 'workbench.sqlite3', 17), ('classification', 'platform.sqlite3', 5)]:
        with sqlite3.connect(restored[name] / database) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == version
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert [restored_ocr.result_snapshot(result_id, revision) for revision in range(3)] == fixed_snapshots
    assert restored_ocr.result_history(result_id)['total'] == 3
    assert restored_ocr.file(image['original_path']).read_bytes() == original_bytes
    version = restored_ocr.one('versions', image['active_version'])
    assert hashlib.sha256(restored_ocr.file(version['path']).read_bytes()).hexdigest() == version['sha256']
    assert restored_ocr.adoption_operation(lost['operation_id'], project['id'], 'instanceA') == lost_receipt
    with pytest.raises(ocr_module.Conflict):
        restored_ocr.adoption_operation(lost['operation_id'], project['id'], 'instanceB')
    relocated = restored_class.one('SELECT * FROM assets WHERE id=?', (native_asset['id'],))
    assert Path(relocated['path']).is_relative_to(restored['classification'])
    assert Path(relocated['path']).read_bytes() == original_bytes and relocated['label_id'] == 'b'
    relocated_dataset = restored_class.one('SELECT * FROM datasets WHERE id=?', ('frozenDataset',))
    assert Path(relocated_dataset['manifest_path']).is_relative_to(restored['classification'])
    assert hashlib.sha256(Path(relocated_dataset['manifest_path']).read_bytes()).hexdigest() == relocated_dataset['manifest_sha256']
    assert Path(json.loads(Path(relocated_dataset['manifest_path']).read_text('utf-8'))['items'][0]['path']).is_relative_to(restored['classification'])
    assert Path(restored_class.one('SELECT package_path FROM models WHERE id=?', ('frozenModel',))['package_path']).is_relative_to(restored['classification'])
    assert json.loads(restored_class.one('SELECT prediction FROM job_items WHERE id=?', (native_item,))['prediction']) == prediction
    assert restored_class.disposition_operation('coldProject', class_operation['operation_id']) == class_receipt
    assert restored_class.dispose_results(native_job, [native_item], 'confirmed', 'b', class_payload['note'], {native_item: 1}, **class_arguments) == class_receipt
    assert restored_class.one('SELECT count(*) total FROM result_disposition_events')['total'] == 2
    for batch, payload, operation, committed in [(class_batch, class_payload, class_operation, class_committed),
                                               (ocr_batch, known_payload, known, known_committed)]:
        replay = reopened.begin_operation(batch['batch_id'], payload, origin={'instance_id': 'instanceB'})
        assert replay['operation_id'] == operation['operation_id']
        assert reopened.commit_operation(replay['operation_id']) == committed
        assert reopened.history(batch['batch_id']) == before_history[batch['batch_id']]
        assert reopened.selected_items(batch['batch_id'], payload['item_ids'])[0]['adoption'] == committed['batch']['issues'][0]['adoption']
    assert reopened.operation(lost['operation_id'], private=True) == unknown
    snapshot, counts, _ = reopened.snapshot(ocr_batch['batch_id'])
    assert snapshot['coverage']['index_complete'] and counts['confirmed'] == 1
    assert not present_batch(snapshot, counts)['summary']['review_complete']
    with pytest.raises(DataError, match='待核实'):
        reopened.begin_operation(ocr_batch['batch_id'], {**lost_payload, 'request_id': 'coldBlindRetry'})
    recovered_operation = reopened.begin_operation(ocr_batch['batch_id'], lost_payload, origin={'instance_id': 'instanceB'})
    assert recovered_operation['intent']['origin'] == ocr_origin
    assert restored_ocr.adopt_result(lost['operation_id'], ocr_body()) == lost_receipt
    recovered_adoption = {**adoption, 'native_receipt': lost_receipt}
    reopened.checkpoint_operation(lost['operation_id'], 'native_committed', native_receipt=lost_receipt)
    completed = reopened.commit_operation(lost['operation_id'], adoption=recovered_adoption)
    assert reopened.commit_operation(lost['operation_id']) == completed and completed['revision'] == 3
    assert reopened.history(ocr_batch['batch_id'])['total'] == 2
    assert len(restored_ocr.rows('SELECT * FROM platform_adoptions')) == 2
    restored_artifacts = ArtifactStore(restored['platform'] / 'artifact-center/artifacts.sqlite3', restored['platform'] / 'artifact-center/blobs')
    for artifact in artifacts:
        assert restored_artifacts.get(workspace.workspace_id, artifact.artifact_id) == artifact
        with restored_artifacts.stage_download(workspace.workspace_id, artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as staged:
            assert hashlib.sha256(staged.read_bytes()).hexdigest() == artifact.blob_hash
    assert original.read_bytes() == original_bytes
    assert json.loads(dataset.read_text('utf-8'))['items'][0]['path'] == str(original)


def _component_backup_functions(component, monkeypatch):
    if component == 'platform':
        return create, verify, restore
    source = Path(__file__).resolve().parents[3] / 'classification/src'
    monkeypatch.syspath_prepend(str(source))
    from image_platform import backup as class_backup
    assert Path(class_backup.__file__).resolve().is_relative_to(source.resolve())
    return class_backup.backup, class_backup.verify, class_backup.restore


@pytest.mark.parametrize('component', ['platform', 'classification'])
@pytest.mark.parametrize('operation', ['create_destination', 'create_source', 'verify', 'restore_destination', 'restore_source'])
def test_backup_rejects_outside_drive_before_creating_any_output(tmp_path, monkeypatch, component, operation):
    create_component, verify_component, restore_component = _component_backup_functions(component, monkeypatch)
    outside = Path(os.environ.get('SystemRoot', 'C:/Windows')).resolve()
    assert outside.is_dir() and outside.drive.upper() == 'C:'
    source, archive, output = (tmp_path / name for name in ('source', 'archive', 'output'))
    source.mkdir()
    archive.mkdir()
    calls = {'create_destination': lambda: create_component(source, outside / 'codex-forbidden-output'),
             'create_source': lambda: create_component(outside, output), 'verify': lambda: verify_component(outside),
             'restore_destination': lambda: restore_component(archive, outside / 'codex-forbidden-output'),
             'restore_source': lambda: restore_component(outside, output)}
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*'))
    with pytest.raises(ValueError, match='local F drive'):
        calls[operation]()
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')) == before
    assert not output.exists()


@pytest.mark.parametrize('component', ['platform', 'classification'])
def test_backup_rejects_f_junction_into_readonly_c_before_any_write(tmp_path, monkeypatch, component):
    from workbench_platform.supervisor.processes import require_f_path
    create_component, verify_component, restore_component = _component_backup_functions(component, monkeypatch)
    outside = Path(os.environ.get('SystemRoot', 'C:/Windows')).resolve()
    assert outside.is_dir() and outside.drive.upper() == 'C:'
    link = tmp_path / 'own-readonly-c-junction'
    require_f_path(link, 'Own negative-test link')
    assert not link.exists()
    source, archive, output = (tmp_path / name for name in ('source', 'archive', 'output'))
    source.mkdir()
    archive.mkdir()
    try:
        created = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)], cwd=tmp_path,
            env=os.environ.copy(), capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, timeout=10)
        assert created.returncode == 0, created.stdout + created.stderr
        assert link.is_junction() and link.resolve() == outside
        before = sorted(path.name for path in tmp_path.iterdir())
        for call in (lambda: create_component(source, link / 'forbidden-output'), lambda: create_component(link, output),
                     lambda: verify_component(link), lambda: restore_component(archive, link / 'forbidden-output'),
                     lambda: restore_component(link, output)):
            with pytest.raises(ValueError, match='local F drive|Linked backup path'):
                call()
        assert sorted(path.name for path in tmp_path.iterdir()) == before
        assert not any(source.iterdir()) and not any(archive.iterdir())
        assert not output.exists()
        assert not any(tmp_path.glob('*.pending-*'))
    finally:
        if link.is_junction():
            os.rmdir(link)  # Remove only this own F junction, never its C target.
    assert not link.exists()
