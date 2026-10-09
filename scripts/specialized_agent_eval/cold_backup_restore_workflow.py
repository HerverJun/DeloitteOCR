"""Cold-restore stopped M2 HTTP/UI data with actual production consumers.

Back up the platform and native OCR independently. Keep the original fixture
bytes immutable; relocate only owned copies, isolate their old locations, and
reject restored native access to any former data root. No model is invoked.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from cold_backup_restore_native import (checkpoint_owned, database_state,
    freeze_consumers, inventory, read_native, require)


def platform_screen_reads(root, seed, base, token, native_batch):
    """Rebind only the explicitly injected engineering host's loopback endpoint."""
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.bridge.context import ResolvedContext
    from workbench_platform.jobs.ocr_handoff import OcrHandoff
    from workbench_platform.jobs.screen_batches import ScreenBatches

    registry = AssetRegistry(root/'assets.sqlite3', root/'asset-blobs',
                             {'uploads': root/'asset-uploads'})
    sets = AssetSetStore(registry)
    ctx = ResolvedContext(workspace_id=seed['workspace_id'], app_id='ocr',
        instance_id='m2-instance', link_id=seed['link_id'],
        module_project_id=native_batch['project_id'], role='owner',
        return_route='/ocr', base_url=base)

    def context(workspace, app, link):
        require((workspace, app, link) == (ctx.workspace_id, ctx.app_id, ctx.link_id),
                'Cold workflow consumer escaped its original explicit fixture scope')
        return ctx

    jobs = SimpleNamespace(_context=context, _token=lambda: token,
                           _live=lambda current: current == ctx)
    handoff = OcrHandoff(root/'ocr-handoffs.sqlite3', jobs, registry, sets)
    center = ArtifactCenter(root/'artifact-center', registry, sets, None, ocr_handoff=handoff)
    batches = ScreenBatches(root/'screen-batches.sqlite3', handoff, center)
    snapshot = batches.delivery_snapshot(ctx.workspace_id, seed['batch_id'])
    with closing(sqlite3.connect((root/'screen-batches.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        bundle_ids = [row[0] for row in db.execute(
            'SELECT bundle_id FROM screen_batch_exports WHERE workspace_id=? AND batch_id=? ORDER BY bundle_id',
            (ctx.workspace_id, seed['batch_id']))]
    require(len(bundle_ids) >= 2, 'Cold workflow needs both historical and current native ZIP bundles')
    bundles = []
    for bundle_id in bundle_ids:
        freshness = batches.export_freshness(ctx.workspace_id, seed['batch_id'], bundle_id)
        description = center.get(ctx.workspace_id, bundle_id)
        downloads = []
        for file in description['files']:
            artifact = center.file(ctx.workspace_id, bundle_id, file['name'])
            with center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                require(fpath(path).is_relative_to(root), 'Artifact staged outside the owned cold root')
                downloads.append({'name': file['name'], 'artifact_id': artifact.artifact_id,
                    'revision': artifact.revision, 'sha256': digest(path), 'bytes': path.stat().st_size})
        bundles.append({'bundle_id': bundle_id, 'freshness': freshness,
                        'description': description, 'actual_downloads': downloads})
    require(any(row['freshness']['current'] for row in bundles)
            and any(not row['freshness']['current'] for row in bundles),
            'Historical/current ZIP distinction was not preserved')
    return {'snapshot': snapshot, 'bundles': bundles, 'loopback_context_injection': True,
            'human_or_model_action': False, 'ordinary_registered_services': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fixture', 'output', 'platform-source', 'native-source', 'bundle',
                 'pdf-layer', 'native-python', 'agent-layer'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--negative-grid-contract', help='Exact immutable engineering-fixture rejection; no general bypass')
    args = parser.parse_args()
    require_environment()
    fixture, output = fpath(args.fixture), fpath(args.output)
    require(not output.exists(), 'Use a new immutable cold workflow output')
    stop, ready, seed, browser = [read_json(fixture/name) for name in
        ('stopped.json', 'ready.json', 'seed.json', 'browser.json')]
    require(stop['servers_stopped'] and browser['passed'] and read_json(fixture/'browser-stopped.json')['browser_stopped'],
            'Actual HTTP/UI source is not stopped and qualified')
    source_native, source_platform = fpath(ready['native_data']), fpath(ready['platform_data'])
    negative_path = fpath(args.negative_grid_contract) if args.negative_grid_contract else None
    negative = read_json(negative_path) if negative_path else None
    if negative:
        require(negative['format'] == 'm2-cold-negative-grid-fixture-v1'
                and fpath(negative['fixture']) == fixture
                and negative['fixture_lineage_sha256'] == digest(fixture/'lineage.json')
                and negative['batch_id'] == seed['batch_id'] and negative['input_version_id'] == seed['version_id'],
                'Negative grid contract is not the exact original declared engineering fixture')
    require(source_native.is_relative_to(fixture) and source_platform.is_relative_to(fixture),
            'The stopped runtime is outside its declared evidence root')
    require(fpath(stop['native_sqlite']).parent == source_native and fpath(stop['platform_data']) == source_platform,
            'Stop receipt names a different live runtime')
    original = {'native': inventory(source_native), 'platform': inventory(source_platform)}
    output.mkdir(parents=True)
    for key in ('TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR'):
        path = output/'environment'/key.lower(); path.mkdir(parents=True)
        os.environ[key] = str(path)
    sys.path.insert(0, str(fpath(args.agent_layer)))
    bindings = freeze_consumers(args, output)
    helper_bindings = {}
    for name in ('cold_backup_restore_workflow.py', 'cold_backup_restore_native.py',
                 'common.py', 'controlled_recovery.py', 's07_native_read.py'):
        path = fpath(__file__).parent/name; copied = output/'helper-source'/name
        copied.parent.mkdir(parents=True, exist_ok=True); copied.write_bytes(path.read_bytes())
        require(digest(path) == digest(copied), 'Executed cold helper changed while archiving')
        helper_bindings[name] = bound(copied)
    sys.path[:0] = [args.platform_source, str(fpath(args.platform_source)/'src')]
    from scripts.backup_workspace import create, verify, restore, _relocate_screen_paths
    from scripts.product_compatibility import check, DATA_COMPATIBILITY
    from workbench_platform.reader_requirements import infer_reader_requirements, marker_requirements

    args.source_run = fixture; args.s07_run = fixture
    args.additional_forbid_data_roots = []
    if (fixture/'lineage.json').is_file():
        lineage = read_json(fixture/'lineage.json')
        require(lineage['passed'], 'The stopped fixture lineage is not qualified')
        origin = fpath(lineage['source'])
        require(origin != fixture, 'Fixture lineage must preserve a distinct former source')
        args.additional_forbid_data_roots.append(origin)
    owned = output/'sources'
    source = {'native': owned/'native/ocr', 'platform': owned/'platform'}
    restored = {'native': output/'restored/native/ocr', 'platform': output/'restored/platform'}
    report = {'format': 'm2-live-workflow-cold-restore-v1', 'status': 'running',
        'utc': datetime.now(timezone.utc).isoformat(), 'fixture': str(fixture),
        'fixture_browser': bound(fixture/'browser.json'), 'stopped_source': bound(fixture/'stopped.json'),
        'source_consumer_inventory': bound(output/'consumer-source-inventory.json'),
        'driver': bound(__file__), 'controller_calls': 0, 'model_calls': 0, 'formal_current_changed': False,
        'archived_helpers': helper_bindings,
        'declared_negative_grid_fixture': bound(negative_path) if negative_path else None,
        'fixture_lineage': bound(fixture/'lineage.json') if (fixture/'lineage.json').is_file() else None,
        'scope': 'Deterministic engineering fixture; actual HTTP edits, native ZIP and schema19 state. No recognition, Agent or final 240/24 qualification.'}
    try:
        for name, origin in (('native', source_native), ('platform', source_platform)):
            shutil.copytree(origin, source[name])
            require(inventory(source[name]) == original[name], 'Stopped runtime byte copy differs')
        frozen_json(output/'original-inventories-before.json', original)
        checkpoint_owned(owned)
        members = {name: {'sha256': sha, 'sqlite': name.endswith('.sqlite3')}
                   for name, sha in inventory(source['native']).items() if not name.endswith(('-wal', '-shm'))}
        relocation = _relocate_screen_paths(source['native'], source['native'],
            {'kind': 'ocr', 'source_data_dir': str(source_native), 'files': members})
        with closing(sqlite3.connect((source['native']/'workbench.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
            batches = db.execute('SELECT batch_id,COUNT(*) FROM screen_items GROUP BY batch_id ORDER BY batch_id').fetchall()
            complete_inputs = sum(count for _, count in batches)
            local_count = db.execute('SELECT COUNT(*) FROM screen_local_requests').fetchone()[0]
            edited_count = sum(bool(__import__('json').loads(row[0]).get('screen',{}).get('metadata_edit_log'))
                               for row in db.execute('SELECT edited FROM results'))
        require(complete_inputs > 0 and local_count > 0 and edited_count > 0,
                'Cold workflow needs persisted business edits and actual schema19 candidate state')
        def consume_native(phase, roots):
            consumed = {}
            for index, (batch_id, count) in enumerate(batches):
                args.batch_id = batch_id; args.expected_inputs = count
                args.export_required = batch_id == seed['batch_id']
                args.negative_grid_contract = negative if negative and batch_id == negative['batch_id'] else None
                consumer = (lambda base, token, batch: platform_screen_reads(roots['platform'], seed, base, token, batch)) if args.export_required else None
                consumed[batch_id] = read_native(args, roots['native'], output, phase+'-'+str(index), consumer=consumer)
            return consumed
        source_read = consume_native('source', source)
        require(any(batch['local_candidate_reads'] for batch in source_read.values()),
                'Actual native local-candidate API was not consumed')
        positive_batches = [batch for batch in source_read.values()
                            if not batch['actual_read_audit']['declared_negative_grid_fixture']]
        require(positive_batches and all(batch['actual_read_audit']['diagnostics_source_pixel_read_events'] > 0
                                        for batch in positive_batches),
                'Cold workflow lacks actual positive diagnostics pixel consumption')
        checkpoint_owned(owned)
        cold_state, cold_bytes = database_state(owned), inventory(owned)
        components = {}
        for name, kind in (('native', 'ocr'), ('platform', 'platform')):
            source_requirements = infer_reader_requirements(source[name])
            source_marker = marker_requirements(source[name])
            backup = output/'backups'/name
            manifest = create(source[name], backup, kind=kind)
            require(verify(backup) == manifest, 'Actual independent component backup failed verification')
            receipt = restore(backup, restored[name])
            restored_requirements = infer_reader_requirements(restored[name])
            restored_marker = marker_requirements(restored[name])
            features = restored_requirements['reader_features']
            require(restored_requirements == source_requirements,
                    'Restored component actual-record reader inference changed')
            expected_features = set(source_marker.get('reader_features', [])) | set(source_requirements['reader_features'])
            require(set(restored_marker.get('reader_features', [])) == expected_features
                    and 'screen-business-metadata-edit-v1' in expected_features,
                    'Restored component lost its persisted metadata reader admission')
            components[name] = {'kind': kind, 'backup_manifest': bound(backup/'manifest.json'),
                'actual_restore_receipt': receipt, 'reader_features': features,
                'source_actual_record_reader_inference': source_requirements,
                'restored_actual_record_reader_inference': restored_requirements,
                'source_reader_marker': source_marker, 'restored_reader_marker': restored_marker}
        require(inventory(owned) == cold_bytes, 'Creating backups changed the cold owned sources')
        isolated = output/'isolated-cold-sources'; owned.rename(isolated)
        require(not owned.exists() and inventory(isolated) == cold_bytes, 'Cold source isolation altered bytes')
        frozen_json(output/'cold-source-isolation.json', {'former_owned_root_absent': True,
            'isolated_owned_root': str(isolated), 'original_fixture_not_moved': True})
        restored_state = database_state(output/'restored')
        require(cold_state == restored_state, 'Business rows/revisions/budgets/local candidates changed in restoration')
        frozen_json(output/'cold-logical-state.json', cold_state)
        frozen_json(output/'restored-logical-state.json', restored_state)
        check(restored['platform'], DATA_COMPATIBILITY)
        check(output/'restored', DATA_COMPATIBILITY)
        restored_read = consume_native('restored', restored)
        keys = ('version_vector', 'details', 'policy', 'source_and_prepared_files',
                'existing_export_files', 'local_candidate_reads', 'additional_consumption')
        for batch_id, batch in source_read.items():
            for key in keys:
                require(batch.get(key) == restored_read[batch_id].get(key), 'Restored actual consumer differs: '+key)
        require(database_state(output/'restored') == restored_state, 'Read-only consumers changed business rows')
        require(inventory(isolated) == cold_bytes, 'Consumer altered the isolated cold source')
        require(inventory(output/'consumers') == bindings['files_sha256'], 'Tested consumer bytes changed')
        report.update(status='passed', scope_inputs=complete_inputs, native_batch_count=len(batches),
            business_metadata_edited_results=edited_count, local_candidate_rows=local_count,
            logical_state_equal=True, actual_native_http_equal=True,
            local_candidate_state_and_artifact_downloads_equal=True,
            platform_current_and_historical_zip_reads_equal=True,
            positive_diagnostic_pixel_batches=len(positive_batches),
            declared_negative_dimension_rejection_preserved=negative is not None,
            old_scope_native_reads=0, components=components, source_copy_live_relocation=relocation,
            actual_native_read_receipts={phase: [bound(output/(phase+'-'+str(index)+'-native-read-with-audit.json'))
                for index in range(len(batches))] for phase in ('source','restored')})
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        after = {'native': inventory(source_native), 'platform': inventory(source_platform)}
        report['original_source_files_and_sha_unchanged'] = original == after
        frozen_json(output/'original-inventories-after.json', after)
        if original != after:
            report.update(status='failed', error='Original fixture bytes changed')
        frozen_json(output/'cold-restore-result.json', report)
    print({key: report[key] for key in ('status', 'scope_inputs', 'local_candidate_rows',
           'platform_current_and_historical_zip_reads_equal', 'original_source_files_and_sha_unchanged')})


if __name__ == '__main__':
    main()
