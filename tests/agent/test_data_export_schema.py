"""Real exports distinguish source locators without changing fixed results."""
import asyncio
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from contextlib import closing
from unittest.mock import patch

import pytest

from workbench_platform.application_services.references import data_reference, verify
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.data_api.table_schema import cleaned_table_schema, schema_from_export


@pytest.mark.parametrize('business_count', [3, 9])
def test_real_export_schema_and_fixed_readback_preserve_columns_values_and_old_refs(tmp_path, monkeypatch, business_count):
    asyncio.run(real_export_case(tmp_path, monkeypatch, business_count))


async def real_export_case(tmp_path, monkeypatch, business_count):
    import test_data_tools as fixtures
    root = tmp_path / 'fixed-data'
    (root / 'fixtures').mkdir(parents=True)
    headers = ['编号', '姓名', '金额'] if business_count == 3 else [
        '编号', '金额', '供应商名称', '地址', '联系人', '城市', '国家', '类型', '备注']
    identities = ['000731', '000732', '000733']
    amounts = ['100.20', '001.20', '118.75']
    original_rows = [[identities[index], ' Alice ', amounts[index]] if business_count == 3 else
                     [identities[index], amounts[index], *['  值' + str(index) + '  '] * 7] for index in range(3)]
    source = root / 'fixtures/source.csv'
    with source.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(original_rows)
    monkeypatch.setattr(fixtures, 'RUN', root)
    monkeypatch.setattr(fixtures, 'PYTHON', Path('F:/OCR/release/Clear/versions/0.10.0/program/runtimes/data/python.exe'))
    fixture = fixtures.DataToolTests(methodName='test_real_clean_compare_export_independent_values_and_native_provenance')
    fixture.setUp()
    try:
        session = await fixture.create()
        await fixture.import_asset(session, 'left', fixture.asset('source.csv'))
        for index in ([2] if business_count == 3 else range(3, 10)):
            value = await fixture.call('data_apply_step', {'data_session_id': session, 'side': 'left',
                'expected_version': fixture.native.get(session)['version'], 'step': {'operator': 'trim', 'column': 'c' + str(index)}})
            assert value['status'] == 'success'
        page = await fixture.call('data_read_table', {'data_session_id': session, 'side': 'left'})
        revision = page['data']['metadata']['revision']
        table_schema = page['data']['table_schema']
        assert table_schema['revision'] == revision and table_schema['row_count'] == 3
        assert table_schema['business_column_count'] == business_count
        assert table_schema['source_locator_column_count'] == 1
        assert table_schema['export_column_count'] == business_count + 1
        assert [column['header'] for column in table_schema['business_columns']] == headers
        assert [column['name'] for column in table_schema['source_locator_columns']] == ['source_row']
        assert [row['source_row'] for row in page['data']['rows']] == [2, 3, 4]
        # A historical reader hashed only the original immutable metadata.
        original_page = fixture.native.page
        def legacy_page(*args, **kwargs):
            result = original_page(*args, **kwargs)
            result.pop('table_schema', None)
            return result
        with patch.object(fixture.native, 'page', side_effect=legacy_page):
            legacy_hash = data_reference(fixture.ctx, session, 'table', revision)['content_sha256']
        old_ref = {**page['result_refs'][0], 'sha256': legacy_hash}
        assert verify(old_ref, fixture.ctx)['content_sha256'] == legacy_hash
        exported = await fixture.call('data_export', {'data_session_id': session, 'side': 'left',
            'expected_version': fixture.native.get(session)['version']})
        assert exported['status'] == 'success'
        assert exported['data']['table_schema'] == table_schema
        bundle = exported['data']['bundle']
        assert bundle['table_schema'] == table_schema
        original_get = fixture.center.get
        def legacy_bundle(*args, **kwargs):
            value = original_get(*args, **kwargs)
            value.pop('table_schema', None)
            return value
        with patch.object(fixture.center, 'get', side_effect=legacy_bundle):
            legacy_bundle_hash = verify(exported['artifact_refs'][0], fixture.ctx)['content_sha256']
        assert verify({**exported['artifact_refs'][0], 'sha256': legacy_bundle_hash}, fixture.ctx)['content_sha256'] == legacy_bundle_hash
        with closing(fixture.native.db()) as db:
            jobs_before = [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')]
        state_before = fixture.native.get(session)
        fixed_bytes = {}
        output = root / 'readback'
        output.mkdir()
        for file in bundle['files']:
            artifact = fixture.center.file('wtest', bundle['bundle_id'], file['name'])
            with fixture.center.store.stage_download('wtest', artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                content = path.read_bytes()
            fixed_bytes[file['name']] = content
            (output / file['name']).write_bytes(content)
        # A new artifact reader can explain old bundles from their managed
        # files even if the original native runtime is not configured.
        cold_center = ArtifactCenter(fixture.center.store.db_path.parent, fixture.assets, fixture.sets, None)
        assert cold_center.get('wtest', bundle['bundle_id'])['table_schema'] == table_schema
        csv_artifact = cold_center.file('wtest', bundle['bundle_id'], 'cleaned.csv')
        assert cold_center.read_file_context('wtest', csv_artifact.artifact_id) == {
            'bundle_id': bundle['bundle_id'], 'file_name': 'cleaned.csv', 'table_schema': table_schema}
        assert cold_center.read_file_context('wother', csv_artifact.artifact_id) is None
        from workbench_platform.agent.tools.results import read_text, TextRead
        text = read_text(TextRead(reference={'kind': 'artifact', 'workspace_id': 'wtest',
            'object_id': csv_artifact.artifact_id, 'revision': 1}, limit=4000), fixture.ctx)
        assert text['status'] == 'success'
        assert text['data']['table_schema'] == table_schema
        assert text['data']['file_sha256'] == csv_artifact.blob_hash
        fixture.center._data_table_schemas.clear()
        assert fixture.center.get('wtest', bundle['bundle_id'])['table_schema'] == table_schema
        assert fixture.native.get(session) == state_before
        with closing(fixture.native.db()) as db:
            assert [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')] == jobs_before
        assert verify(old_ref, fixture.ctx)['content_sha256'] == legacy_hash
        reader = """import json,sys,duckdb
from pathlib import Path
from openpyxl import load_workbook
p=Path(sys.argv[1]); book=load_workbook(p/'cleaned.xlsx',read_only=True,data_only=False)
xlsx=list(book.active.values);book.close()
with duckdb.connect(config={'temp_directory':str(p/'temp'),'extension_directory':str(p/'extensions'),'threads':1,'autoinstall_known_extensions':False,'autoload_known_extensions':False}) as db:
 c=db.execute('SELECT * FROM read_parquet(?)',[str(p/'cleaned.parquet')]); columns=[r[0] for r in c.description];parquet=c.fetchall()
print(json.dumps({'xlsx':xlsx,'parquet_headers':columns,'parquet_rows':parquet},ensure_ascii=False))
"""
        independent = json.loads(subprocess.check_output([str(fixtures.PYTHON), '-B', '-c', reader, str(output)],
            cwd=output, text=True, encoding='utf-8'))
        exported_rows = list(csv.reader(fixed_bytes['cleaned.csv'].decode('utf-8-sig').splitlines()))
        expected_rows = [[str(index + 2), *[value.strip() if headers[col] not in {'编号', '金额'} else value
                            for col, value in enumerate(row)]] for index, row in enumerate(original_rows)]
        assert exported_rows[0] == table_schema['export_columns']
        assert exported_rows[1:] == expected_rows
        assert independent['xlsx'] == exported_rows
        assert independent['parquet_headers'] == table_schema['export_columns']
        assert independent['parquet_rows'] == expected_rows
        for name, content in fixed_bytes.items():
            artifact = cold_center.file('wtest', bundle['bundle_id'], name)
            with cold_center.store.stage_download('wtest', artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                assert path.read_bytes() == content and hashlib.sha256(content).hexdigest() == artifact.blob_hash
        from workbench_platform.artifacts.store import ArtifactUnavailable
        summary_artifact = cold_center.file('wtest', bundle['bundle_id'], 'summary.json')
        original_stage = cold_center.store.stage_download
        def missing_summary(workspace_id, artifact_id, **kwargs):
            if artifact_id == summary_artifact.artifact_id:
                raise ArtifactUnavailable('MISSING')
            return original_stage(workspace_id, artifact_id, **kwargs)
        cold_center._data_table_schemas.clear()
        with patch.object(cold_center.store, 'stage_download', side_effect=missing_summary):
            degraded = cold_center.get('wtest', bundle['bundle_id'])
            assert degraded['files'] == bundle['files']
            assert degraded['table_schema']['status'] == 'unknown' and degraded['table_schema']['reason']
            assert 'business_column_count' not in degraded['table_schema']
            with cold_center.store.stage_download('wtest', csv_artifact.artifact_id,
                    expected_revision=csv_artifact.revision, expected_hash=csv_artifact.blob_hash) as path:
                assert path.read_bytes() == fixed_bytes['cleaned.csv']
    finally:
        fixture.tearDown()


def test_grouped_source_locator_schema_is_separate_and_unknown_export_column_is_rejected():
    columns = [{'column_id': 'c1', 'header': '类别'}]
    schema = cleaned_table_schema(columns, 2, revision='fixed', has_source_rows=True)
    assert schema['business_column_count'] == 1 and schema['source_locator_column_count'] == 2
    assert schema['export_columns'] == ['source_row', 'c1 | 类别', 'source_rows_json']
    with pytest.raises(ValueError, match='定位列'):
        schema_from_export({'columns': columns, 'rows': 2, 'revision': 'fixed'}, ['source_row', 'c1 | 类别', 'unexpected'])
