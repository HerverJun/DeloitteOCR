"""Artifact text is read through the real dispatcher, source scope and blob store."""
import asyncio
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from packages.contracts.agent import AgentRef
from packages.contracts.models import SourceLocator
from workbench_platform.agent.registry import ToolRegistry
from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.tools import results
from workbench_platform.application_services.context import ToolContext
from workbench_platform.application_services.result_text import read_text
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.artifacts.native_exports import publish
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.sets import AssetSetStore


@pytest.fixture
def fixture(tmp_path):
    uploads, blobs = tmp_path / 'uploads', tmp_path / 'asset-blobs'
    uploads.mkdir(); blobs.mkdir()
    (uploads / 'original.csv').write_bytes(b'number,amount\r\n000731,100.20\r\n')
    (uploads / 'other.csv').write_bytes(b'other source')
    assets = AssetRegistry(tmp_path / 'assets.sqlite3', blobs, {'uploads': uploads})
    sets = AssetSetStore(assets)
    source = assets.register('workspace', 'uploads', 'original.csv', media_type='text/csv', storage_mode='managed')
    other = assets.register('workspace', 'uploads', 'other.csv', media_type='text/csv', storage_mode='managed')
    center = ArtifactCenter(tmp_path / 'artifacts', assets, sets, None)
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', 'Read fixed output', [source.asset_id], 'original-session')
    run, _ = store.start_run(session['id'], 'Verify original output without recomputation', 'original-goal', 0)
    services = SimpleNamespace(artifact_center=center, asset_library=(assets, sets, None),
                              workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    ctx = ToolContext('workspace', session['id'], run['id'], 1, 0, 'opublish', (source.asset_id,), services, store)
    locator = SourceLocator(schema_version='1.0', kind='file', asset_id=source.asset_id,
                            asset_revision=1, blob_hash=source.blob_hash, precision='exact')
    csv_text = 'source_row,c1 | 编号,c2 | 金额\r\n1,000731,100.20\r\n2,000732,118.75\r\n'
    long_text = '第００１项 10.00 😀\r\n' * 9000
    payloads = {'cleaned.csv': (csv_text.encode('utf-8-sig'), 'text/csv'),
                'notes.txt': (long_text.encode('utf-8'), 'text/plain'),
                'summary.json': ('{"number":"000731","amount":"100.20"}\r\n'.encode('utf-8'), 'application/json'),
                'workpaper.md': ('# 发现草稿\n编号000731，差额18.55。\n'.encode('utf-8'), 'text/markdown'),
                'workpaper.html': ('<main contenteditable="true">差额18.55<script>not executed</script></main>'.encode('utf-8'), 'text/html'),
                'cleaned.parquet': (b'PAR1\0binary', 'application/vnd.apache.parquet'),
                'invalid.txt': (b'not UTF8: \xff', 'text/plain'),
                'nul.txt': (b'text\0binary', 'text/plain')}
    bundle = publish(ctx, 'Fixed export', payloads,
        [{'asset_id': source.asset_id, 'revision': 1, 'blob_hash': source.blob_hash}], [locator], 'fixed-export-v1')
    registry = ToolRegistry()
    results.register(registry)
    files = {name: center.file('workspace', bundle['bundle_id'], name) for name in payloads}
    def reference(name, **updates):
        file = files[name]
        # Match the real failing call: the catalog supplied a file artifact ID,
        # revision=1, and workspace, without a bundle/hash/application field.
        return {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': file.artifact_id,
                'revision': file.revision, **updates}
    return SimpleNamespace(ctx=ctx, registry=registry, center=center, source=source, other=other,
        bundle=bundle, files=files, payloads=payloads, reference=reference, csv_text=csv_text, long_text=long_text)


def dispatch(fixture, reference, **kwargs):
    call_id = 'read_' + str(len(fixture.ctx.store.rows('SELECT * FROM operations')))
    return asyncio.run(fixture.registry.dispatch({'call_id': call_id, 'tool': 'results_read_text',
        'arguments': {'reference': reference, **kwargs}}, fixture.ctx))


def test_real_failing_csv_file_reference_with_limit_4000_is_fixed_and_hashed(fixture):
    before = [file.model_dump(mode='json') for file in fixture.files.values()]
    result = dispatch(fixture, fixture.reference('cleaned.csv'), limit=4000)
    assert result['status'] == 'success'
    data = result['data']
    assert data['text'] == fixture.csv_text
    assert data['text_total_chars'] == len(fixture.csv_text) and data['text_next_offset'] is None
    assert data['text_truncated'] is False and data['utf8_bom_removed'] is True
    assert data['file_sha256'] == fixture.files['cleaned.csv'].blob_hash
    assert data['text_sha256'] == hashlib.sha256(fixture.csv_text.encode('utf-8')).hexdigest()
    assert data['file_reference']['object_id'] == fixture.files['cleaned.csv'].artifact_id
    assert data['file_reference']['revision'] == 1 and data['file_reference']['sha256'] == data['file_sha256']
    assert data['source_locators'][0]['asset_id'] == fixture.source.asset_id
    assert 'source_row' in result['warnings'][0]
    assert [file.model_dump(mode='json') for file in fixture.files.values()] == before
    assert fixture.ctx.store.rows('SELECT * FROM model_requests') == []
    assert fixture.ctx.store.rows('SELECT DISTINCT tool FROM operations') == [{'tool': 'results_read_text'}]


@pytest.mark.parametrize('name', ['notes.txt', 'summary.json', 'workpaper.md', 'workpaper.html'])
def test_supported_plain_and_json_preserve_exact_utf8_text_and_offsets(fixture, name):
    artifact = fixture.files[name]
    result = dispatch(fixture, fixture.reference(name, sha256=artifact.blob_hash), offset=3, limit=17)
    expected = fixture.payloads[name][0].decode('utf-8')
    assert result['status'] == 'success' and result['data']['text'] == expected[3:20]
    assert result['data']['text_offset'] == 3 and result['data']['text_next_offset'] == 20
    assert result['data']['text_total_chars'] == len(expected)
    assert result['data']['file_sha256'] == artifact.blob_hash
    assert result['data']['text_truncated'] and not result['data']['utf8_bom_removed']


def test_long_multibyte_artifact_reconstructs_across_stream_chunks_and_pages(fixture):
    offset, pages = 0, []
    reference = fixture.reference('notes.txt')
    while offset is not None:
        value = read_text(reference, fixture.ctx, offset=offset, limit=32000)
        assert value['status'] == 'success'
        assert len(value['data']['text']) <= 32000
        pages.append(value['data']['text'])
        offset = value['data']['text_next_offset']
    assert ''.join(pages) == fixture.long_text


def test_bundle_requires_actual_file_choice_and_returns_usable_fixed_file_references(fixture):
    bundle_ref = {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': fixture.bundle['bundle_id'], 'revision': 1}
    missing = dispatch(fixture, bundle_ref, limit=4000)
    assert missing['status'] == 'unsupported' and missing['error']['code'] == 'artifact_file_required'
    assert 'file_name' in missing['data']['failure']['user_action']
    csv_choice = next(file for file in missing['data']['files'] if file['name'] == 'cleaned.csv')
    assert csv_choice['readable_text'] is True
    selected = dispatch(fixture, bundle_ref, file_name='cleaned.csv', limit=4000)
    direct = dispatch(fixture, csv_choice['reference'], limit=4000)
    assert selected['status'] == direct['status'] == 'success'
    assert selected['data']['text'] == direct['data']['text'] == fixture.csv_text
    absent = dispatch(fixture, bundle_ref, file_name='does-not-exist.csv')
    assert absent['error']['code'] == 'artifact_file_required'


@pytest.mark.parametrize('name,code', [('cleaned.parquet', 'artifact_text_unsupported'),
                                       ('invalid.txt', 'artifact_encoding_unsupported'),
                                       ('nul.txt', 'artifact_encoding_unsupported')])
def test_binary_non_utf8_and_nul_have_specific_repair_actions_without_partial_text(fixture, name, code):
    result = dispatch(fixture, fixture.reference(name), limit=4000)
    assert result['status'] == 'unsupported' and result['error']['code'] == code
    assert result['data']['failure']['user_action']
    assert result['data']['file_reference']['object_id'] == fixture.files[name].artifact_id
    assert 'text' not in result['data']


@pytest.mark.parametrize('updates,code', [({'revision': 2}, 'artifact_revision_mismatch'),
                                          ({'sha256': 'a' * 64}, 'artifact_hash_mismatch'),
                                          ({'workspace_id': 'different'}, 'text_workspace_denied')])
def test_wrong_fixed_identity_never_returns_content(fixture, updates, code):
    result = dispatch(fixture, fixture.reference('cleaned.csv', **updates), limit=4000)
    assert result['status'] == 'error' and result['error']['code'] == code
    assert result['data']['failure']['user_action'] and 'text' not in result['data']


def test_unselected_source_and_foreign_artifact_id_do_not_bypass_workspace_or_scope(fixture):
    restricted = replace(fixture.ctx, selected_assets=(fixture.other.asset_id,))
    denied = read_text(fixture.reference('cleaned.csv'), restricted, limit=4000)
    assert denied['error']['code'] == 'artifact_scope_denied' and 'text' not in denied['data']
    foreign = fixture.reference('cleaned.csv')
    ctx = replace(fixture.ctx, workspace_id='different')
    foreign['workspace_id'] = 'different'
    absent = read_text(foreign, ctx, limit=4000)
    assert absent['error']['code'] == 'artifact_text_unavailable' and 'text' not in absent['data']


def test_changed_blob_is_detected_and_never_reconstructed_or_reexported(fixture):
    artifact = fixture.files['cleaned.csv']
    path = fixture.center.store._blob(artifact.blob_hash)
    path.write_bytes(b'changed')
    result = dispatch(fixture, fixture.reference('cleaned.csv'), limit=4000)
    assert result['error']['code'] == 'artifact_text_unavailable'
    assert result['data']['failure']['reason'] == 'CORRUPT' and 'text' not in result['data']
    assert fixture.center.store.get('workspace', artifact.artifact_id).availability == 'corrupt'
    assert path.read_bytes() == b'changed'


def test_file_with_no_source_cannot_escape_selected_input_scope(fixture):
    bundle = publish(replace(fixture.ctx, operation_id='ounbound'), 'No proven input',
        {'orphan.txt': (b'unbound content', 'text/plain')}, [], [], 'fixed-export-v1')
    file = fixture.center.file('workspace', bundle['bundle_id'], 'orphan.txt')
    result = read_text({'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': file.artifact_id,
                       'revision': 1}, fixture.ctx)
    assert result['error']['code'] == 'artifact_scope_denied' and 'text' not in result['data']


def test_single_fixed_file_remains_readable_when_a_sibling_is_unavailable(fixture):
    artifact = fixture.files['summary.json']
    fixture.center.store._blob(artifact.blob_hash).unlink()
    reference = fixture.reference('cleaned.csv', parent_id=fixture.bundle['bundle_id'])
    result = dispatch(fixture, reference, limit=4000)
    assert result['status'] == 'success' and result['data']['text'] == fixture.csv_text


def test_legacy_windows_excel_mime_needs_real_catalog_csv_name_and_verified_utf8(fixture):
    from contextlib import closing
    import json
    source = fixture.source
    locator = SourceLocator(schema_version='1.0', kind='file', asset_id=source.asset_id,
                            asset_revision=1, blob_hash=source.blob_hash, precision='exact')
    bundle = publish(replace(fixture.ctx, operation_id='olegacy'), 'Legacy MIME', {
        'cleaned.csv': (fixture.csv_text.encode('utf-8-sig'), 'application/vnd.ms-excel'),
        'binary.csv': (b'\xd0\xcf\x11\xe0\x00binary', 'application/vnd.ms-excel'),
        'book.xls': (b'not a text contract', 'application/vnd.ms-excel')},
        [{'asset_id': source.asset_id, 'revision': 1, 'blob_hash': source.blob_hash}], [locator], 'legacy-export')
    def ref(name, parent=None):
        file = fixture.center.file('workspace', bundle['bundle_id'], name)
        return {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': file.artifact_id, 'revision': 1,
                **({'parent_id': parent} if parent else {})}
    # Generic bundles cannot opt in merely by naming Excel content .csv.
    assert dispatch(fixture, ref('cleaned.csv', bundle['bundle_id']))['error']['code'] == 'artifact_text_unsupported'
    mapping = {file['name']: file['artifact_id'] for file in bundle['files']}
    data_bundle = 'bdata_legacy'
    with closing(fixture.center._connect()) as db:
        db.execute('INSERT INTO data_artifact_bundle VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (data_bundle, 'workspace', 'original-data-session', 'Registered Data export', 'original-export', None,
             'a' * 64, 'original-set', 1,
             json.dumps([{'asset_id': source.asset_id, 'revision': 1, 'blob_hash': source.blob_hash}]),
             json.dumps(mapping), 'ready', 'now'))
    success = dispatch(fixture, ref('cleaned.csv'), limit=4000)
    assert success['status'] == 'success' and success['data']['text'] == fixture.csv_text
    assert success['data']['media_type'] == 'application/vnd.ms-excel' and success['data']['text_format'] == 'csv'
    assert success['data']['file_sha256'] == fixture.center.file('workspace', bundle['bundle_id'], 'cleaned.csv').blob_hash
    assert 'source_row' in success['warnings'][0]
    assert success['data']['bundle_id'] == data_bundle
    assert dispatch(fixture, ref('binary.csv'))['error']['code'] == 'artifact_encoding_unsupported'
    assert dispatch(fixture, ref('book.xls'))['error']['code'] == 'artifact_text_unsupported'
    # A caller-provided filename cannot turn the registered XLS into CSV.
    assert dispatch(fixture, ref('book.xls'), file_name='cleaned.csv')['error']['code'] == 'artifact_file_name_unnecessary'


def test_cancelled_generation_never_reads_or_returns_artifact_contents(fixture):
    from workbench_platform.application_services.identity import RunSuperseded
    with fixture.ctx.store.transaction() as db:
        db.execute("UPDATE runs SET status='cancelled',generation=generation+1 WHERE id=?", (fixture.ctx.run_id,))
    with patch.object(fixture.center.store, 'stage_download', side_effect=AssertionError('Fenced read')):
        with pytest.raises(RunSuperseded):
            read_text(fixture.reference('cleaned.csv'), fixture.ctx)


def test_unsupported_data_result_names_actual_structured_reader(fixture):
    value = dispatch(fixture, {'kind': 'table', 'workspace_id': 'workspace', 'object_id': 'revision',
        'revision': 'revision', 'application': 'data', 'parent_id': 'original-session'})
    assert value['status'] == 'unsupported' and value['error']['code'] == 'text_reference_unsupported'
    assert value['data']['alternative_tools'] == ['data_read_table']
    assert 'data' not in value['data']


def test_ocr_fixed_version_reader_and_tool_schema_contract_are_preserved(fixture):
    reference = AgentRef(kind='result', workspace_id='workspace', object_id='native-result', revision=2,
                         application='ocr', parent_id='original-handoff', sha256='a' * 64)
    with patch('workbench_platform.application_services.result_text.verify', return_value={
        'snapshot': {'available': True, 'edited': {'text': '原文 000731 100.20\r\n'}}, 'locator': {'page_number': 2}}):
        result = dispatch(fixture, reference.model_dump(mode='json'), limit=4000)
    assert result['status'] == 'success' and result['data']['revision'] == 2
    assert result['data']['source_locator'] == {'page_number': 2}
    assert result['result_refs'][0] == reference.model_dump(mode='json')
    schema = fixture.registry.describe('results_read_text')
    assert schema['input_schema']['properties']['limit']['maximum'] == 32000
    assert 'file_name' in schema['input_schema']['properties'] and 'file_name' not in schema['input_schema']['required']
    assert schema['input_kinds'] == ('result', 'artifact')
    assert 'CSV' in schema['description'] and 'file_name' in schema['description']
