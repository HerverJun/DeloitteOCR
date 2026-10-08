"""Live business identities, not unchanged counts, qualify current delivery."""
from contextlib import closing
import io
import json
import zipfile

import pytest
import test_screen_batches as fixtures
from workbench_platform.jobs.screen_batches import ScreenSubmit, ScreenExport
from workbench_platform.jobs.native_ocr import OcrResultConflict

bridge = fixtures.bridge


def ready(bridge):
    client, workspace, fixed, state, service, _ = bridge
    service.submit(workspace, ScreenSubmit.model_validate(fixtures.arguments(fixed)))
    state.update(pending=0, status='succeeded')
    return client, workspace, state, service


@pytest.mark.parametrize('bridge', [4], indirect=True)
def test_equal_counts_edited_revision_marks_old_bundle_historical(bridge):
    client, workspace, state, service = ready(bridge)
    bundle = service.export(workspace, 'batchA', ScreenExport())
    route = f'/api/platform/v1/workspaces/{workspace}/artifacts/{bundle["bundle_id"]}/freshness'
    assert client.get(route, headers=fixtures.LOCAL).json()['current'] is True
    state['result_revision'] = 1
    value = client.get(route, headers=fixtures.LOCAL).json()
    assert value['state'] == 'historical' and value['current'] is False
    renewed = service.export(workspace, 'batchA', ScreenExport())
    assert renewed['bundle_id'] != bundle['bundle_id']
    assert service.export_freshness(workspace, 'batchA', renewed['bundle_id'])['current'] is True


@pytest.mark.parametrize('bridge', [4], indirect=True)
def test_review_transition_without_edit_invalidates_export(bridge):
    _, workspace, state, service = ready(bridge)
    bundle = service.export(workspace, 'batchA', ScreenExport())
    state['human_review_status'] = 'confirmed'
    assert service.export_freshness(workspace, 'batchA', bundle['bundle_id'])['current'] is False


@pytest.mark.parametrize('bridge', [4], indirect=True)
def test_stale_snapshot_is_rejected_before_managed_publication(bridge):
    _, workspace, state, service = ready(bridge)
    state['stale_export'] = True
    with pytest.raises(OcrResultConflict):
        service.export(workspace, 'batchA', ScreenExport())


@pytest.mark.parametrize('bridge', [4], indirect=True)
def test_options_and_snapshot_own_idempotency_and_old_no_snapshot_is_history(bridge):
    _, workspace, _, service = ready(bridge)
    first = service.export(workspace, 'batchA', ScreenExport())
    assert service.export(workspace, 'batchA', ScreenExport())['bundle_id'] == first['bundle_id']
    second = service.export(workspace, 'batchA', ScreenExport(include_pending_review=True))
    assert second['bundle_id'] != first['bundle_id']
    with service._connect() as db:
        db.execute('UPDATE screen_batch_exports SET snapshot_json=NULL WHERE bundle_id=?', (first['bundle_id'],))
    assert service.export_freshness(workspace, 'batchA', first['bundle_id'])['state'] == 'historical'


@pytest.mark.parametrize('bridge', [4], indirect=True)
def test_managed_snapshot_pins_service_vector_scope_options_and_merges_reader_marker(bridge):
    _, workspace, _, service = ready(bridge)
    marker_path = service.path.parent / 'product-reader-requirements.json'
    marker = (json.loads(marker_path.read_text(encoding='utf-8')) if marker_path.exists() else
              {'format': 'clear-product-reader-requirements-v1', 'reader_features': ['screen-batch-v1']})
    marker['note'] = '保留原标记字段'
    marker['reader_features'].append('fixed-draft-transfer-v1')
    marker_path.write_text(json.dumps(marker), encoding='utf-8')
    options = ScreenExport(include_pending_review=True)
    bundle = service.export(workspace, 'batchA', options)
    delivery = service.delivery_snapshot(workspace, 'batchA')
    expected_vector = {**{key: delivery[key] for key in
        ('snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items')},
        'export_options': options.model_dump(mode='json')}
    with closing(service._connect()) as db:
        saved_row = db.execute('SELECT * FROM screen_batch_exports WHERE bundle_id=?', (bundle['bundle_id'],)).fetchone()
    saved = json.loads(saved_row['snapshot_json'])
    assert saved_row['workspace_id'] == workspace and saved_row['batch_id'] == 'batchA'
    assert saved['snapshot_id'] == delivery['snapshot_id']
    assert saved['export_options'] == options.model_dump(mode='json')
    assert saved['version_vector'] == expected_vector
    assert {item['input_version_id'] for item in saved['version_vector']['items']} == {
        item['input_version_id'] for item in json.loads(service._row(workspace, 'batchA')['source_json'])}
    marker = json.loads(marker_path.read_text(encoding='utf-8'))
    assert marker['note'] == '保留原标记字段'
    assert {'screen-batch-v1', 'screen-delivery-snapshot-v1', 'fixed-draft-transfer-v1'} <= set(marker['reader_features'])
    assert service.export_freshness(workspace, 'batchA', bundle['bundle_id'])['current'] is True


@pytest.mark.parametrize('bridge', [4], indirect=True)
@pytest.mark.parametrize('tamper', ['source', 'policy', 'options', 'snapshot', 'missing_vector'])
def test_manifest_vector_tamper_cannot_supply_managed_snapshot_or_publish_artifact(bridge, monkeypatch, tamper):
    _, workspace, _, service = ready(bridge)
    native = service.handoff._adapter(None)
    original_export = native.screen_export

    def wrong_vector(project_id, batch_id, options):
        data = original_export(project_id, batch_id, options)
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
            for entry in source.infolist():
                content = source.read(entry.filename)
                if entry.filename == 'manifest.json':
                    manifest = json.loads(content)
                    vector = manifest['version_vector']
                    if tamper == 'source':
                        vector['items'][0]['image_id'] = 'foreign-image'
                    elif tamper == 'policy':
                        vector['policy_sha256'] = '0' * 64
                    elif tamper == 'options':
                        vector['export_options']['include_pending_review'] = True
                    elif tamper == 'snapshot':
                        vector['snapshot_id'] = '0' * 64
                    else:
                        manifest.pop('version_vector')
                    content = json.dumps(manifest).encode()
                target.writestr(entry, content)
        return output.getvalue()

    monkeypatch.setattr(native, 'screen_export', wrong_vector)
    monkeypatch.setattr(service.handoff, '_adapter', lambda context: native)
    publish = []
    monkeypatch.setattr(service.artifacts.ocr, 'publish', lambda *args, **kwargs: publish.append(True))
    with pytest.raises(OcrResultConflict):
        service.export(workspace, 'batchA', ScreenExport())
    assert publish == []
    with closing(service._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM screen_batch_exports').fetchone()[0] == 0
