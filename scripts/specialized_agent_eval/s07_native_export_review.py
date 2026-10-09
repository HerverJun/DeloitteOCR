"""Independent CPU readback of literal OCR/data artifacts, including failed runs."""
from __future__ import annotations
import argparse
from contextlib import closing
import csv
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from common import digest, fpath, frozen_bytes, frozen_json, read_json
from s07_native_read import cold_copy, inventory
from s07_agent_native_followup import all_comparison_categories, platform_binding


def rows(database, sql, args=()):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def xlsx_rows(path):
    namespace = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as book:
        shared = []
        if 'xl/sharedStrings.xml' in book.namelist():
            shared = [''.join(item.itertext()) for item in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('x:si', namespace)]
        observed = []
        for row in ET.fromstring(book.read('xl/worksheets/sheet1.xml')).findall('x:sheetData/x:row', namespace):
            values = {}
            for cell in row.findall('x:c', namespace):
                value = cell.find('x:v', namespace)
                text = value.text if value is not None else ''.join(cell.find('x:is', namespace).itertext()) if cell.find('x:is', namespace) is not None else ''
                values[''.join(c for c in cell.attrib['r'] if c.isalpha())] = shared[int(text)] if cell.attrib.get('t') == 's' else text
            observed.append(values)
    headers = observed[0]
    return [{headers[key]: row.get(key, '') for key in headers} for row in observed[1:]]


def review(args):
    original, source, root = map(fpath, (args.original_run, args.platform_source, args.output_root))
    ledger = fpath(args.ledger) if args.ledger else original / 'actual-agent-operations.json'
    if root.exists():
        raise ValueError('Independent export review requires a new immutable F: root')
    before, bound = inventory(original), platform_binding(source)
    root.mkdir(parents=True); temp = root / 'temp'; temp.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
        os.environ[name] = str(temp)
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.artifacts.store import ArtifactStore
    databases = root / 'databases'; databases.mkdir()
    copied = {}
    for label, relative in (('agent', 'platform/agent/operations.sqlite3'),
                            ('data', 'platform/data/sessions.sqlite3'),
                            ('audit', 'platform/audit/audit.sqlite3')):
        copied[label] = cold_copy(original / relative, databases / (label + '.sqlite3'))
    frozen_json(root / 'original-database-copy-qualification.json', copied)
    operations = read_json(ledger)
    originals = rows(databases / 'agent.sqlite3', 'SELECT * FROM operations WHERE run_id=? ORDER BY created', (args.run_id,))
    if len(originals) != len(operations):
        raise ValueError('Supplied literal ledger is not the complete actual run')
    for expected, actual in zip(operations, originals, strict=True):
        if (expected['id'] != actual['id'] or expected['call_id'] != actual['call_id']
                or expected['arguments'] != json.loads(actual['arguments'])
                or expected['receipt'] != (json.loads(actual['result']) if actual['result'] else None)):
            raise ValueError('Supplied ledger differs from original read-only SQLite')
    success = {entry['tool']: entry for entry in operations if (entry['receipt'] or {}).get('status') == 'success'}
    exported = success['data_export']; bundle = exported['receipt']['data']['bundle']
    if not all_comparison_categories(exported['arguments'].get('categories')):
        raise ValueError('Actual task exported a category subset')
    owned = root / 'artifacts'; owned.mkdir()
    cold_copy(original / 'platform/artifacts/artifacts.sqlite3', owned / 'artifacts.sqlite3')
    shutil.copytree(original / 'platform/artifacts/blobs', owned / 'blobs')
    store = ArtifactStore(owned / 'artifacts.sqlite3', owned / 'blobs')
    files = {}
    for entry in bundle['files']:
        artifact = store.get(bundle['workspace_id'], entry['artifact_id'])
        with store.stage_download(bundle['workspace_id'], artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            target = root / 'delivered' / entry['name']
            frozen_bytes(target, path.read_bytes())
        if target.stat().st_size != entry['size_bytes'] or digest(target) != artifact.blob_hash:
            raise ValueError('Actual downloaded artifact bytes or size changed')
        files[entry['name']] = {'sha256': digest(target), 'size_bytes': target.stat().st_size, 'artifact_id': artifact.artifact_id}
    summary, provenance = [read_json(root / 'delivered' / name) for name in ('summary.json', 'provenance.json')]
    session = rows(databases / 'data.sqlite3', 'SELECT * FROM sessions WHERE id=?', (bundle['session_id'],))[0]
    current = json.loads(session['state'])
    if (current['result_id'] != bundle['result_id'] or summary['comparison_id'] != bundle['result_id']
            or provenance['comparison_id'] != bundle['result_id'] or not all_comparison_categories(summary['included_categories'])):
        raise ValueError('Delivery does not include the current full-category comparison')
    for side in ('left', 'right'):
        if (summary[side + '_revision'] != current['tables'][side]['revision']
                or summary[side + '_source'] != current['tables'][side]['source']):
            raise ValueError('Export source version is no longer current')
    if summary['policy'] != current['comparison']['spec']:
        raise ValueError('Export comparison policy differs from committed state')
    with (root / 'delivered/results.csv').open(encoding='utf-8-sig', newline='') as stream:
        csv_rows = list(csv.DictReader(stream))
    if len(csv_rows) != 7 or summary['coverage']['same_pairs'] != 6 or summary['coverage']['different_pairs'] != 1:
        raise ValueError('Actual export coverage differs from the explicit engineering sample')
    differences = [row for row in csv_rows if row['category'] == 'different']
    if (len(differences) != 1 or differences[0]['L_c1_raw'] != 'rs4387287'
            or differences[0]['L_c3_raw'] != 'OBFCI' or differences[0]['R_c3_raw'] != 'OBFC1'):
        raise ValueError('Actual export lost the original literal difference')
    for row, trace in zip(csv_rows, provenance['rows'], strict=True):
        for side, prefix in (('left', 'L'), ('right', 'R')):
            if (trace['category'] != row['category'] or trace[side]['row_id'] != row[side + '_row_id']
                    or trace[side]['source_row'] != int(row[side + '_source_row'])
                    or trace[side]['blob_hash'] != summary[side + '_source']['sha256']):
                raise ValueError('Row-level source provenance changed')
    sheets = xlsx_rows(root / 'delivered/results.xlsx')
    fields = ('category', 'key_json', 'left_row_id', 'right_row_id', 'left_source_row', 'right_source_row', 'L_c1_raw', 'L_c3_raw', 'R_c3_raw')
    if len(sheets) != len(csv_rows) or any(any(sheet[key] != row[key] for key in fields)
                                        for sheet, row in zip(sheets, csv_rows, strict=True)):
        raise ValueError('XLSX comparison facts differ from CSV/provenance')
    evidence = success['audit_read_evidence']['receipt']['data']
    stored = json.loads(rows(databases / 'audit.sqlite3', 'SELECT body FROM direct_audit_evidence WHERE evidence_id=?',
                             (evidence['evidence_id'],))[0]['body'])
    actual_ref = success['data_read_result']['receipt']['result_refs'][0]
    if stored['snapshot_hash'] != evidence['snapshot_hash'] or stored['reference'] != actual_ref or evidence['stale'] is not False:
        raise ValueError('Direct evidence binding/current source changed')
    deliveries = [entry for entry in operations if entry['tool'] == 'delivery_publish' and (entry['receipt'] or {}).get('status') == 'success']
    for delivery in deliveries:
        receipt = delivery['receipt']; result_refs = receipt['result_refs']
        if (receipt['data']['verified'] is not True or receipt['data']['completeness'] != 'complete'
                or bundle['bundle_id'] not in {item['bundle_id'] for item in receipt['data']['delivery']}
                or actual_ref not in result_refs or not any(ref['kind'] == 'evidence' and ref['object_id'] == evidence['evidence_id']
                    and ref['sha256'] == evidence['snapshot_hash'] for ref in result_refs)):
            raise ValueError('Actual delivery references differ from exact current export/evidence')
    status = rows(databases / 'agent.sqlite3', 'SELECT status FROM runs WHERE id=?', (args.run_id,))[0]['status']
    if inventory(original) != before or platform_binding(source) != bound:
        raise ValueError('Original run or frozen source changed during independent review')
    frozen_json(root / 'export-review.json', {'format': 's07-native-export-independent-review-v1', 'status': 'pass',
        'original_run': str(original), 'run_id': args.run_id, 'original_agent_status': status,
        'original_run_unchanged': True, 'terminal_status_rewritten': False, 'ledger_sha256': digest(ledger),
        'source_files': bound, 'artifacts': files, 'coverage': summary['coverage'], 'full_categories': summary['included_categories'],
        'literal_difference': {key: differences[0][key] for key in fields}, 'provenance_rows_verified': len(csv_rows),
        'xlsx_comparison_rows_verified': len(sheets), 'direct_evidence_snapshot_hash': evidence['snapshot_hash'],
        'verified_delivery_count': len(deliveries), 'controller_calls': 0, 'gpu_calls': 0, 'helper_sha256': digest(__file__),
        'scope': 'Independent production artifact download on a copied store plus original read-only committed source/version facts; no run-status repair or business accuracy claim'})
    print('independent_export_review_pass', status, len(deliveries), len(files))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original-run', 'platform-source', 'output-root', 'run-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--ledger')
    review(parser.parse_args())
