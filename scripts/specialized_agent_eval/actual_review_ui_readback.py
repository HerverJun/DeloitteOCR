"""Independent literal XLSX/source/revision readback of actual-batch UI copies."""
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import io
import json
import re
import sqlite3
import zipfile
import zlib

from common import digest, fpath, frozen_json, read_json
from cold_backup_restore_native import copy_actual, inventory, require
from actual_review_ui_host import immutable_execution
from precision_route_cold_restore import execution_ledger
from readback import check_book


def entry(detail, source_hash):
    return {**detail, 'id': detail['input_version_id'],
            'image_sha256': source_hash, 'candidate': detail['result']}


def original_http_matches(http, committed, path='', rounded=None):
    """Only the known nanosecond load ordinal may lose integer precision in JS."""
    if http == committed:
        return True
    if isinstance(http, dict) and isinstance(committed, dict) and http.keys() == committed.keys():
        return all(original_http_matches(http[key], committed[key], path + '/' + key, rounded)
                   for key in http)
    if isinstance(http, list) and isinstance(committed, list) and len(http) == len(committed):
        return all(original_http_matches(a, b, path + '/' + str(index), rounded)
                   for index, (a, b) in enumerate(zip(http, committed)))
    if (type(http) is int and type(committed) is int and abs(committed) > 2**53
            and re.fullmatch(r'/screen/sources/\d+/prepared_metadata/engine_load_events/\d+/ordinal', path)
            and float(http) == float(committed)):
        if rounded is not None:
            rounded.append({'path': path, 'browser_json_integer': str(http), 'committed_integer': str(committed)})
        return True
    return False


def decode_revision(value):
    if isinstance(value, bytes):
        require(value.startswith(b'OCRZ1\0'), 'Unknown immutable revision encoding')
        value = zlib.decompress(value[6:])
    return json.loads(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('host', 'browser', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--platform', action='store_true', help='Full Portal host with an additional cancelled submission')
    args = parser.parse_args()
    host, browser, output = map(fpath, (args.host, args.browser, args.output))
    require(not output.exists(), 'Use a new immutable independent readback identity')
    scope, stop, report = read_json(host / 'scope.json'), read_json(host / 'stopped.json'), read_json(browser / 'browser.json')
    require(stop['servers_stopped'] and read_json(browser / 'browser-stopped.json')['browser_stopped']
            and report['passed'], 'UI and no-queue host must be successfully stopped')
    output.mkdir(parents=True)
    original = fpath(scope['source_run']) / 'native/ocr'
    original_bytes = (read_json(host / 'original-bytes-before.json')['native/ocr'] if args.platform
                      else read_json(host / 'original-native-bytes-before.json'))
    require(inventory(original) == original_bytes, 'Original inference evidence changed')
    owned = host / 'runtime/ocr'
    before = inventory(owned)
    copy_actual(owned, output / 'database', {'workbench.sqlite3': 'workbench.sqlite3'})
    copied_ledger = execution_ledger(output / 'database')
    initial_ledger = read_json(host / 'initial-execution-ledger.json')
    if args.platform:
        from actual_platform_review_host import old_scope
        copied_ledger = old_scope(copied_ledger, initial_ledger)
    require(immutable_execution(copied_ledger) == immutable_execution(initial_ledger),
            'Requests, policies, deadlines, attempts, costs or failed tasks changed')
    with closing(sqlite3.connect(output / 'database/workbench.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        results = {row['id']: dict(row) for row in db.execute('SELECT * FROM results')}
        items = {row['input_version_id']: dict(row) for row in db.execute(
            'SELECT * FROM screen_items WHERE batch_id=?', (scope['batch_id'],))}
        revisions = {(row['result_id'], row['revision']): row['edited']
                     for row in db.execute('SELECT result_id,revision,edited FROM result_revisions')}
        if args.platform:
            submission = read_json(browser / 'precision-submission.json')
            new_id = submission['response']['batch_id']
            new_batch = dict(db.execute('SELECT * FROM screen_batches WHERE id=?', (new_id,)).fetchone())
            native_policy = json.loads(new_batch['policy'])
            require(new_batch['request_id'] == submission['request']['request_id'] and
                    native_policy == submission['native']['policy'] == submission['cancelled']['policy'],
                    'Submitted policy/request changed after lookup or cancellation')
            require(all(native_policy[key] == value for key, value in submission['request']['policy'].items()),
                    'Portal precision policy did not reach the persisted native batch')
            created = datetime.fromisoformat(new_batch['created']).timestamp()
            require(abs(new_batch['deadline'] - created - native_policy['batch_budget_seconds']) < 1,
                    'Original submitted deadline does not match the original whole-batch budget')
            new_items = [dict(row) for row in db.execute('SELECT * FROM screen_items WHERE batch_id=?', (new_id,))]
            require(len(new_items) == scope['images'] and all(row['status'] == 'cancelled' and row['result_id'] is None
                    for row in new_items), 'Cancelled submission scope was lost or started recognition')
    platform_bytes = None
    if args.platform:
        platform_root = host / 'runtime/platform'
        platform_bytes = inventory(platform_root)
        copy_actual(platform_root, output / 'platform-database', {'screen-batches.sqlite3': 'screen-batches.sqlite3'})
        with closing(sqlite3.connect(output / 'platform-database/screen-batches.sqlite3')) as db:
            db.row_factory = sqlite3.Row
            submitted_ref = dict(db.execute('SELECT * FROM screen_batch_refs WHERE batch_id=?', (new_id,)).fetchone())
            require(submitted_ref['request_id'] == new_batch['request_id'] and
                    json.loads(submitted_ref['policy_json']) == submission['request']['policy'],
                    'Native request no longer matches the durable platform bridge record')
            export_rows = {row['bundle_id']: dict(row) for row in db.execute('SELECT * FROM screen_batch_exports')}
    initial = {d['input_version_id']: d for d in read_json(host / 'initial-details.json')}
    ledger = read_json(host / 'initial-execution-ledger.json')
    policy = ledger['batches'][0]['policy']
    if isinstance(policy, str):
        policy = json.loads(policy)
    hashes = {row['input_version_id']: row['source_hash'] for row in ledger['items']}
    require(len(initial) == len(items) == scope['images'], 'Full actual scope changed')
    for vid, detail in initial.items():
        current = results[detail['result_id']]
        require(json.loads(current['original']) == detail['result']['original'], 'Original model output changed')
        if vid != scope['selected_version_id']:
            require(json.loads(current['edited']) == detail['result']['edited'] and
                    current['revision'] == detail['result']['revision'] and
                    items[vid]['review_state'] == detail['review_state'], 'Untouched input was edited or confirmed')
    final = read_json(browser / 'final-detail.json')
    require(final['result']['revision'] == results[final['result_id']]['revision'] and
            final['result']['edited'] == json.loads(results[final['result_id']]['edited']),
            'Final HTTP snapshot differs from committed copied database')
    from openpyxl import load_workbook
    reports, browser_ordinal_rounding = [], []
    for download in report['downloads']:
        archive_path = fpath(download['path'])
        require(archive_path.is_relative_to(browser) and digest(archive_path) == download['sha256'], 'Actual browser ZIP bytes changed')
        snapshot = read_json(browser / (download['name'] + '-snapshot.json'))
        require(snapshot['before'] == snapshot['after'] and snapshot['before']['fresh'], 'Live export vector changed')
        current = deepcopy(initial)
        current[scope['selected_version_id']] = snapshot['selected']
        # JavaScript JSON serialization changes 173.0 into 173. The workbook
        # records literal JSON geometry from SQLite. Verify semantic HTTP
        # equality, then use the immutable raw revision for its exact spelling.
        for detail in current.values():
            result = detail['result']
            original_document = json.loads(results[result['id']]['original'])
            encoded_revision = revisions[(result['id'], result['revision'])]
            edited = decode_revision(encoded_revision)
            if (not original_http_matches(result['original'], original_document, rounded=browser_ordinal_rounding)
                    or result['edited'] != edited):
                frozen_json(output / 'revision-mismatch.json', {'download': download['name'],
                    'result_id': result['id'], 'revision': result['revision'],
                    'original_equal': result['original'] == original_document,
                    'http_original': result['original'], 'committed_original': original_document,
                    'http_edited': result['edited'], 'committed_edited': edited})
                raise ValueError('HTTP export snapshot differs from its immutable committed revision')
            result['original'], result['edited'] = original_document, edited
        options = {'include_pending_review': snapshot['request']['include_pending_review'], 'numeric_columns': None}
        live = snapshot['before']
        expected_vector = {key: live[key] for key in ('snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items')}
        expected_vector['export_options'] = options
        if args.platform:
            saved = json.loads(export_rows[download['bundle_id']]['snapshot_json'])
            require(saved['version_vector'] == expected_vector and saved['export_options'] == options,
                    'Managed Portal bundle differs from its persisted native version vector')
        checks = []
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)) and archive.testzip() is None, 'Corrupt or duplicate ZIP members')
            manifest = json.loads(archive.read('manifest.json'))
            require(manifest['version_vector'] == expected_vector and manifest['export_options'] == options,
                    'ZIP does not bind its actual live export options and version vector')
            require(manifest['policy'] == policy, 'Original batch policy changed')
            exported = {row['input_version_id']: row for row in manifest['items']}
            require(len(exported) == len(manifest['items']) == len(current) and set(exported) == set(current), 'Partial export lost full input accounting')
            filenames = []
            for vid, detail in current.items():
                item = exported[vid]
                for key in ('image_id', 'source_name', 'result_id', 'result_revision', 'review_state', 'status', 'human_confirmed'):
                    require(item.get(key) == detail.get(key), 'ZIP identity changed: ' + key)
                should_export = bool(detail['result']['edited']['tables']) and (
                    detail['status'] == 'completed' or options['include_pending_review'] and detail['status'] == 'needs_review')
                require(bool(item.get('file_name')) == should_export, 'Partial download excluded or invented a workbook')
                if not should_export:
                    continue
                name = item['file_name']
                require('/' not in name and '\\' not in name, 'Unsafe ZIP workbook path')
                filenames.append(name)
                book = load_workbook(io.BytesIO(archive.read(name)), data_only=False)
                try:
                    check = check_book(book, name, entry(detail, hashes[vid]))
                    require(check['status'] == 'passed', 'Literal workbook readback failed: ' + json.dumps(check['errors']))
                    checks.append({'input_version_id': vid, 'file_name': name, **check})
                finally:
                    book.close()
            require(len(filenames) == len(set(filenames)) == manifest['success_files'] == download['expected_files']
                    and set(names) == {'manifest.json', *filenames}, 'Actual ZIP workbook scope differs')
        reports.append({'name': download['name'], 'zip_sha256': digest(archive_path),
                        'inputs_accounted': len(current), 'workbooks': len(checks),
                        'version_vector_and_current_options_verified': True, 'checks': checks})
    require(inventory(owned) == before and inventory(original) == original_bytes, 'Readback changed protected runtime/evidence')
    if args.platform:
        require(inventory(platform_root) == platform_bytes, 'Readback changed protected platform evidence')
    frozen_json(output / 'report.json', {'format': 'actual-review-ui-independent-xlsx-readback-v1', 'status': 'passed',
        'host': str(host), 'browser': str(browser), 'source_run': scope['source_run'],
        'source_binding': {'freeze_sha256': digest(host / 'source/freeze.json'), 'browser_sha256': digest(browser / 'browser.json')},
        'inputs': len(initial), 'untouched_results_preserved': len(initial) - 1,
        'downloads': reports, 'workbooks_read': sum(r['workbooks'] for r in reports),
        'browser_load_ordinal_json_rounding': browser_ordinal_rounding,
        'original_model_outputs_preserved': True, 'original_request_budget_cost_and_failures_preserved': True,
        'model_calls': 0, 'agent_calls': 0, 'scripted_review_actions': True,
        'full_production_portal': args.platform,
        'platform_precision_policy_request_and_deadline_verified': args.platform,
        'human_quality_or_relief_qualification': False, 'formal_current': 'v0.9.3',
        'helper_sha256': digest(__file__)})
    print(json.dumps({'status': 'passed', 'inputs': len(initial), 'workbooks_read': sum(r['workbooks'] for r in reports)}))


if __name__ == '__main__':
    main()
