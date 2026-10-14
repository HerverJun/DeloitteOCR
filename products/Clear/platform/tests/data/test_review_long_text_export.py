"""Lossless review XLSX text, independently read from the physical ZIP/XML."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from modules.data_workbench.exporting import results as exporter
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.artifacts.store import ArtifactUnavailable


HEADERS = ['ordinal', 'item_id', 'category', 'disposition', 'note', 'adopted_revision',
           'adopted_value', 'original_json', 'adoption_json', 'source_json']
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def snapshot_for(*, note='', audit='', count=1):
    items = []
    for ordinal in range(count):
        raw = {'ordinal': ordinal, 'category': 'review', 'text': 'original',
               'runtime_audit': {'events': audit if ordinal == 0 else 'short audit'}}
        adoption = {'native_revision': 2, 'native_result_id': 'fixedResult',
                    'snapshot': {'original': raw, 'edited': {'text': 'verified revision 2', 'tables': []}}}
        items.append({'item_id': f'issue{ordinal}', 'category': 'review', 'disposition': 'confirmed',
                      'note': note if ordinal == 0 else '', 'raw_output': raw, 'adoption': adoption,
                      'source': {'result_id': 'fixedResult', 'native_revision': 0}})
    return {'batch': {'batch_id': 'batchA', 'workspace_id': 'workspaceA', 'revision': 2,
        'status': 'succeeded', 'template': {'name': '完整 OCR 复核', 'scenario': 'ocr'},
        'inputs': [], 'artifacts': [], 'summary': {},
        'coverage': {'result_rows': count, 'indexed_items': count, 'index_complete': True}},
        'items': items, 'operations': [], 'history': [{'revision': 2, 'action': 'adopt'}],
        'index': {'result_identity': 'fixedResult'}, 'original_bundles': [],
        'predecessor_bundle_id': None,
        'request': {'request_id': 'reviewA', 'kind': 'final',
                    'actor': {'id': 'localUser', 'display_name': '本机编制人', 'source': 'local'}}}


def render(directory, snapshot):
    directory.mkdir()
    return exporter.render_review_export(directory, snapshot, [item['raw_output'] for item in snapshot['items']])


def physical_rows(path):
    """No exporter or OpenPyXL reader: validate all physical text and coordinates."""
    with ZipFile(path) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    assert not sheet.findall('.//' + NS + 'f')
    rows = []
    for row_number, element in enumerate(sheet.findall('.//' + NS + 'row'), start=1):
        assert element.attrib['r'] == str(row_number)
        values = {}
        for cell in element.findall(NS + 'c'):
            assert cell.attrib['t'] == 'inlineStr'
            match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', cell.attrib['r'])
            assert match and int(match[2]) == row_number
            offset = 0
            for character in match[1]:
                offset = offset * 26 + ord(character) - ord('A') + 1
            assert offset - 1 not in values
            value = ''.join(text.text or '' for text in cell.findall('.//' + NS + 't'))
            assert len(value.encode('utf-16-le')) // 2 <= 32000
            values[offset - 1] = value
        width = len(values) if row_number == 1 else len(rows[0])
        assert not values or max(values) < width
        rows.append([values.get(offset, '') for offset in range(width)])
    return rows


def independent_logical(files):
    rows = physical_rows(files['results.xlsx'])
    provenance = json.loads(files['provenance.json'].read_bytes())
    encoding = provenance.get('xlsx_text_encoding')
    if encoding is None:
        assert rows[0] == HEADERS
        return rows, provenance
    assert encoding['schema_version'] == '1.0'
    assert encoding['kind'] == 'utf16_column_chunks' and encoding['max_utf16_units'] == 32000
    assert encoding['logical_headers'] == HEADERS
    assert rows[0] == encoding['physical_headers'] and rows[0][:10] == HEADERS
    columns = encoding['columns']
    assert set(columns) == set(HEADERS)
    for header in HEADERS:
        assert columns[header] == [header, *(f'{header}__part_{i}' for i in range(2, len(columns[header]) + 1))]
    assert rows[0] == [*HEADERS, *(part for header in HEADERS for part in columns[header][1:])]
    assert len(set(rows[0])) == len(rows[0])
    offsets = {name: offset for offset, name in enumerate(rows[0])}
    spills = {(spill['row_number'], spill['column']): spill for spill in encoding['spills']}
    assert len(spills) == len(encoding['spills'])
    observed = set()
    logical_rows = [HEADERS]
    for row_number, row in enumerate(rows[1:], start=2):
        logical = []
        for header in HEADERS:
            parts = [row[offsets[name]] for name in columns[header]]
            value = ''.join(parts)
            units = len(value.encode('utf-16-le')) // 2
            if units > 32000:
                observed.add((row_number, header))
                spill = spills[(row_number, header)]
                assert spill == {'row_number': row_number, 'column': header,
                    'parts': len([part for part in parts if part]), 'characters': len(value),
                    'utf16_units': units, 'utf8_sha256': hashlib.sha256(value.encode('utf-8')).hexdigest()}
                assert all(parts[:spill['parts']]) and not any(parts[spill['parts']:])
            else:
                assert not any(parts[1:])
            logical.append(value)
        logical_rows.append(logical)
    assert observed == set(spills)
    return logical_rows, provenance


def assert_full_values(files, snapshot):
    logical, provenance = independent_logical(files)
    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(max(previous_limit, files['results.csv'].stat().st_size))
        with files['results.csv'].open(encoding='utf-8-sig', newline='') as stream:
            csv_rows = list(csv.reader(stream))
    finally:
        csv.field_size_limit(previous_limit)
    assert csv_rows[0] == HEADERS
    for csv_row, xlsx_row, item in zip(csv_rows[1:], logical[1:], snapshot['items'], strict=True):
        safe = []
        for value in xlsx_row:
            examined = value.lstrip(' \t\r\n\ufeff')
            unsafe = value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) or examined.startswith(('=', '+', '-', '@'))
            safe.append("'" + value if unsafe else value)
        assert csv_row == safe
        record = dict(zip(HEADERS, xlsx_row))
        assert record['note'] == item['note']
        assert record['adopted_value'] == item['adoption']['snapshot']['edited']['text']
        for header, key in [('original_json', 'raw_output'), ('adoption_json', 'adoption'), ('source_json', 'source')]:
            assert record[header] == canonical(item[key])
            assert json.loads(record[header]) == item[key]
    review = json.loads(files['review.json'].read_bytes())
    assert review == {**snapshot, 'results': [item['raw_output'] for item in snapshot['items']],
                      'unresolved': [], 'constraints': []}
    assert json.loads(files['adoptions.json'].read_bytes()) == {'batch_id': 'batchA', 'revision': 2,
        'items': [{'item_id': item['item_id'], **item['adoption']} for item in snapshot['items']]}
    assert provenance['rows'] == [{'ordinal': item['raw_output']['ordinal'], 'item_id': item['item_id'],
                                   'source': item['source'], 'adoption': item['adoption']} for item in snapshot['items']]
    return logical, provenance


def test_full_runtime_audit_and_adoption_with_padding_round_trip(tmp_path):
    audit = ''.join(f'{number:04d}|审计🪷;' for number in range(9000))
    snapshot = snapshot_for(audit=audit, count=2)
    files = render(tmp_path / 'complete', snapshot)
    logical, provenance = assert_full_values(files, snapshot)
    assert len(logical[1][7]) > 70000 and len(logical[1][8]) > 70000
    assert {spill['column'] for spill in provenance['xlsx_text_encoding']['spills']} == {'original_json', 'adoption_json'}
    assert all(spill['row_number'] == 2 for spill in provenance['xlsx_text_encoding']['spills'])
    assert all(not part for part in physical_rows(files['results.xlsx'])[2][10:])


def test_large_adopted_unicode_and_source_preserve_every_public_text_field(tmp_path):
    snapshot = snapshot_for(note='=' + '长复核说明' * 40001, audit='full runtime_audit ' * 16000)
    item = snapshot['items'][0]
    item['adoption']['snapshot']['edited']['text'] = '=literal ' + '🪷' * 100001
    item['source']['runtime_audit'] = 'full source provenance ' * 10001
    previous_limit = csv.field_size_limit()
    files = render(tmp_path / 'all-fields', snapshot)
    assert csv.field_size_limit() == previous_limit
    _, provenance = assert_full_values(files, snapshot)
    assert {spill['column'] for spill in provenance['xlsx_text_encoding']['spills']} == {
        'note', 'adopted_value', 'original_json', 'adoption_json', 'source_json'}


@pytest.mark.parametrize('note,spill', [
    ('a' * 32000, False), ('🪷' * 16000, False),
    ('a' * 31999 + '🪷', True), ('🪷' * 16001, True),
    ('=' + 'a' * 31999 + '=SUM(A1:A3)', True),
    ('\t=literal ' + '字' * 65000, True), ('e\u0301' * 17000, True),
    ('全' * 200001, True),
], ids=['bmp-limit', 'astral-limit', 'scalar-crossing', 'astral-spill', 'formula-part',
        'tab-formula', 'combining', 'csv-over-131072'])
def test_utf16_boundaries_unicode_and_formula_text(tmp_path, note, spill):
    snapshot = snapshot_for(note=note)
    files = render(tmp_path / 'unicode', snapshot)
    _, provenance = assert_full_values(files, snapshot)
    assert ('xlsx_text_encoding' in provenance) is spill
    if note.startswith('a') and spill:
        physical = physical_rows(files['results.xlsx'])
        assert physical[1][4] == 'a' * 31999
        assert physical[1][10] == '🪷'


def test_short_values_keep_legacy_ten_columns_and_no_encoding(tmp_path):
    snapshot = snapshot_for(note='=literal 文本')
    files = render(tmp_path / 'legacy', snapshot)
    _, provenance = assert_full_values(files, snapshot)
    assert 'xlsx_text_encoding' not in provenance
    assert physical_rows(files['results.xlsx'])[0] == HEADERS


def test_concurrent_long_csv_readback_cannot_restore_an_active_readers_limit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    first, second = tmp_path / 'first.csv', tmp_path / 'second.csv'
    rows = {first: [['a' * 500001]], second: [['b' * 250001]]}
    for path, values in rows.items():
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows([['value'], *values])
    entered_first, entered_second, started_second = Event(), Event(), Event()
    release_first, release_second = Event(), Event()
    original_reader = csv.reader
    previous_limit = csv.field_size_limit()
    def paused_reader(stream, *args, **kwargs):
        if Path(stream.name) == first:
            entered_first.set()
            assert release_first.wait(10)
        else:
            entered_second.set()
            assert release_second.wait(10)
        return original_reader(stream, *args, **kwargs)
    def read_second():
        started_second.set()
        exporter._review_csv_readback(second, ['value'], rows[second])
    monkeypatch.setattr(csv, 'reader', paused_reader)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(exporter._review_csv_readback, first, ['value'], rows[first])
        assert entered_first.wait(10)
        b = pool.submit(read_second)
        assert started_second.wait(10)
        try:
            assert not entered_second.wait(0.1)
            release_first.set()
            a.result(timeout=10)
            assert entered_second.wait(10)
        finally:
            release_first.set()
            release_second.set()
        b.result(timeout=10)
    assert csv.field_size_limit() == previous_limit


@pytest.mark.parametrize('limit', ['row', 'column', 'surrogate'])
def test_impossible_bounds_or_invalid_scalar_rejected_before_writing(tmp_path, monkeypatch, limit):
    directory = tmp_path / 'rejected'
    directory.mkdir()
    snapshot = snapshot_for(note='a' * 32001, count=2)
    if limit == 'row':
        monkeypatch.setattr(exporter, '_EXCEL_MAX_ROWS', 2)
        message = 'worksheet row limit'
    elif limit == 'column':
        monkeypatch.setattr(exporter, '_EXCEL_MAX_COLUMNS', 10)
        message = 'worksheet column limit'
    else:
        snapshot['items'][0]['note'] = '\ud800'
        message = 'invalid Unicode scalar'
    with pytest.raises(exporter.ExportError, match=message):
        exporter.render_review_export(directory, snapshot, [item['raw_output'] for item in snapshot['items']])
    assert not list(directory.iterdir())


@pytest.mark.parametrize('phase', ['render', 'mapping'])
def test_long_text_failure_stays_hidden_and_same_bundle_recovers_exact_bytes(tmp_path, monkeypatch, phase):
    snapshot = snapshot_for(audit='完整 runtime_audit 🪷 ' * 4000)
    center = ArtifactCenter(tmp_path / 'center', None, None, None)
    operation = {'bundle_id': 'bundleA', 'request_id': 'reviewA', 'snapshot': canonical(snapshot),
                 'payload_hash': 'a' * 64, 'snapshot_hash': 'b' * 64}
    prepared = center.store.blob_root.parent / 'review-staging/bundleA'
    original_render, original_publish = exporter.render_review_export, center.store.publish_managed
    if phase == 'render':
        original_xlsx = exporter._xlsx
        def changed_xlsx(path, headers, rows):
            original_xlsx(path, headers, rows)
            with ZipFile(path) as archive:
                entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
            with ZipFile(path, 'w') as archive:
                for info, data in entries:
                    if info.filename == 'xl/worksheets/sheet1.xml':
                        document = ElementTree.fromstring(data)
                        cell = next(cell for cell in document.findall('.//' + NS + 'c') if cell.attrib['r'] == 'H2')
                        text = cell.find('.//' + NS + 't')
                        text.text += '-TAMPERED'
                        data = ElementTree.tostring(document)
                    archive.writestr(info, data)
        monkeypatch.setattr(exporter, '_xlsx', changed_xlsx)
        expected_failure = exporter.ExportError
    else:
        attempts = []
        def interrupted(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 4:
                raise OSError('injected after three managed mappings')
            return original_publish(*args, **kwargs)
        monkeypatch.setattr(center.store, 'publish_managed', interrupted)
        expected_failure = OSError
    with pytest.raises(expected_failure):
        center.publish_batch_review(operation)
    assert center.list('workspaceA', 0, 30)['total'] == 0
    with pytest.raises(ArtifactUnavailable):
        center.file('workspaceA', 'bundleA', 'results.xlsx')
    with sqlite3.connect(center.store.db_path) as db:
        row = db.execute('SELECT status,files_json FROM batch_review_bundle').fetchone()
        mapping = json.loads(row[1])
        assert row[0] == 'publishing'
        assert db.execute('SELECT count(*) FROM published_artifact').fetchone()[0] == len(mapping)
        assert len(mapping) == (0 if phase == 'render' else 3)
    if phase == 'render':
        assert not prepared.exists()
        monkeypatch.setattr(exporter, '_xlsx', original_xlsx)
    else:
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in prepared.iterdir()}
        monkeypatch.setattr(center.store, 'publish_managed', original_publish)
        def forbidden_rerender(*args, **kwargs):
            pytest.fail('Retry must reuse frozen prepared bytes after partial publication')
        monkeypatch.setattr(exporter, 'render_review_export', forbidden_rerender)
    bundle = center.publish_batch_review(operation)
    assert bundle['bundle_id'] == 'bundleA'
    assert {file['name']: file['artifact_id'] for file in bundle['files']}.items() >= mapping.items()
    assert center.publish_batch_review(operation) == bundle
    if phase == 'mapping':
        assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in prepared.iterdir()} == before
    saved = {}
    for descriptor in bundle['files']:
        artifact = center.file('workspaceA', 'bundleA', descriptor['name'])
        with center.store.stage_download('workspaceA', artifact.artifact_id, expected_revision=artifact.revision,
                                         expected_hash=artifact.blob_hash) as pinned:
            path = tmp_path / descriptor['name']
            path.write_bytes(pinned.read_bytes())
            saved[descriptor['name']] = path
    assert_full_values(saved, snapshot)
    monkeypatch.setattr(exporter, 'render_review_export', original_render)
