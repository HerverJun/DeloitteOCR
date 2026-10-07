"""Offline score adapter for actual native batches; never impersonates a formal run.

Accepts the real Agent native harness or eight-input controlled recovery output.
Checks frozen source/config, actual SQLite/snapshot identities and normalized
visible input pixels. Writes an immutable native receipt index before opening
truth. No GPU, service, controller, model or recognition is started.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.dont_write_bytecode = True

TABLES = ('images', 'versions', 'tasks', 'results', 'screen_batches',
          'screen_items', 'screen_tasks', 'screen_contexts', 'screen_final_tasks')
PREPARED_FORMATS = {'screen-agent-actual-native-prepared-v1': 'agent',
                    'screen-native-eight-recovery-prepared-v1': 'recovery'}
TERMINAL = {'completed', 'needs_review', 'failed', 'cancelled'}


def fpath(path):
    path = Path(path).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('Native report paths must remain on local F:')
    return path.resolve()


def digest(path):
    with fpath(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def encoded(value, *, ascii=False):
    return json.dumps(value, ensure_ascii=ascii, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value, *, ascii=False):
    return hashlib.sha256(encoded(value, ascii=ascii).encode('utf-8')).hexdigest()


def read_json(path):
    return json.loads(fpath(path).read_text('utf-8'))


def write_new(path, value):
    path = fpath(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def indexed(rows, key, label):
    output = {row[key]: row for row in rows}
    if len(output) != len(rows):
        raise ValueError('Duplicate ' + label + ' identities')
    return output


def object_value(value):
    result = json.loads(value) if isinstance(value, str) else deepcopy(value)
    if not isinstance(result, dict):
        raise ValueError('Expected a native object')
    return result


def source_inventory(source):
    source = fpath(source)
    files = {}
    for name in ('src', 'config'):
        for path in sorted((source / name).rglob('*')):
            if path.is_symlink() or path.is_junction():
                raise ValueError('Frozen source contains a linked entry')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files[path.relative_to(source).as_posix()] = digest(path)
    if not {'src/ocr_workbench/service.py', 'config/screen-policy.json'} <= set(files):
        raise ValueError('Frozen actual native service/policy is missing')
    return files


def normalized_pixels(path):
    # Same EXIF/RGB/white-alpha normalization as native image_utils, without
    # importing any recognition/model modules or changing a native file.
    from PIL import Image, ImageOps
    with Image.open(fpath(path)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGBA', rgba.size, 'white')
            background.alpha_composite(rgba)
            rgb = background.convert('RGB')
        else:
            rgb = image.convert('RGB')
        return {'width': rgb.width, 'height': rgb.height,
                'rgb_pixel_sha256': hashlib.sha256(rgb.tobytes()).hexdigest()}


def read_database(database):
    with closing(sqlite3.connect(fpath(database).as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'images', 'versions', 'tasks', 'results', 'screen_batches', 'screen_items', 'screen_final_tasks'}
        if not required <= names:
            raise ValueError('Actual native database is incomplete')
        return {name: [dict(row) for row in db.execute('SELECT * FROM "' + name + '"')]
                for name in TABLES if name in names}


def table_identity(tables):
    return fingerprint({name: sorted(rows, key=encoded) for name, rows in tables.items()})


def checked_snapshots(run, flavor, current):
    identities = {}
    if flavor == 'recovery':
        path = run / 'after-restart-tables.json'
        saved = read_json(path)
        identities[str(path)] = digest(path)
    else:
        saved = {}
        for name in TABLES:
            path = run / 'native-receipts' / (name + '.json')
            if path.is_file():
                saved[name] = read_json(path)
                identities[str(path)] = digest(path)
    if not {'tasks', 'results', 'versions', 'screen_batches', 'screen_items'} <= set(saved):
        raise ValueError('Authoritative native receipt snapshots are missing')
    for name, rows in saved.items():
        if name in current and sorted(rows, key=encoded) != sorted(current[name], key=encoded):
            raise ValueError('Native database changed after captured ' + name + ' receipts')
    return identities


def compare_policy(frozen, actual):
    for key, expected in frozen.items():
        if key == 'fallback_engines':
            expected = [engine for engine in expected if engine != actual.get('primary_engine')]
        if actual.get(key) != expected:
            raise ValueError('Actual native policy differs from frozen field: ' + key)
    if not actual.get('strategy_version') or actual.get('primary_engine') not in {'glm', 'paddlevl', 'hunyuan'}:
        raise ValueError('Native policy has no explicit strategy/model identity')


def load_native_index(native_run, inputs_path):
    """Build only actual observed identities/content, before any truth read."""
    run, inputs_path = fpath(native_run), fpath(inputs_path)
    prepared_path = run / 'prepared.json'
    prepared = read_json(prepared_path)
    flavor = PREPARED_FORMATS.get(prepared.get('format'))
    if flavor is None or prepared.get('truth_read') is not False or prepared.get('frozen_results_loaded') is not False:
        raise ValueError('Not a truth-isolated actual native harness preparation')
    inputs = read_json(inputs_path)
    if (inputs.get('truth_available_to_inference') is not False
            or prepared.get('inputs_sha256') != digest(inputs_path)
            or fpath(prepared['inputs']) != inputs_path):
        raise ValueError('Frozen input manifest binding differs')
    samples = indexed(inputs['samples'], 'id', 'input sample')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', ident) or ident in {'.', '..'} for ident in samples):
        raise ValueError('Unsafe frozen sample identity')
    source = fpath(prepared['native_source'])
    files = source_inventory(source)
    if (files != prepared['native_source_files']
            or fingerprint(files, ascii=flavor == 'agent') != prepared['native_source_inventory_sha256']):
        raise ValueError('Frozen native source inventory changed')
    bundle = fpath(prepared['bundle'])
    if digest(bundle / 'config/engines.json') != prepared['bundle_engines_sha256']:
        raise ValueError('Model engine configuration changed')
    if flavor == 'agent':
        policy_path = source / 'config/screen-policy.json'
        frozen_policy = prepared['screen_policy']
        policy_hash = prepared['screen_policy_sha256']
        summary_path = run / 'agent-native-summary.json'
        map_path = run / 'input-map.json'
    else:
        policy_path = run / 'run-policy.json'
        frozen_policy = prepared['run_policy']
        policy_hash = prepared['run_policy_sha256']
        if digest(source / 'config/screen-policy.json') != prepared['source_default_policy_sha256']:
            raise ValueError('Frozen source default policy changed')
        summary_path = run / 'native-recovery-summary.json'
        map_path = run / 'native-input-map.json'
    if digest(policy_path) != policy_hash or read_json(policy_path) != frozen_policy:
        raise ValueError('Frozen native policy bytes/content changed')
    summary = read_json(summary_path)
    expected_summary = 'screen-agent-actual-native-result-v1' if flavor == 'agent' else 'screen-native-eight-recovery-result-v1'
    if (summary.get('format') != expected_summary or summary.get('truth_read') is not False
            or summary.get('frozen_results_loaded') is not False
            or summary.get('native_source_inventory_sha256') != prepared['native_source_inventory_sha256']):
        raise ValueError('Actual native run summary binding differs')
    if flavor == 'agent' and (summary.get('real_queue_and_engine_adapter') is not True
            or summary.get('shared_context_injected') is not False
            or summary.get('inputs_sha256') != prepared['inputs_sha256']
            or summary.get('screen_policy_sha256') != policy_hash):
        raise ValueError('Native Agent execution was not actual isolated recognition')
    if flavor == 'recovery' and (summary.get('prefilled_cache') is not False
            or summary.get('run_policy_sha256') != policy_hash
            or summary.get('prepared_sha256') != digest(prepared_path)):
        raise ValueError('Native recovery execution binding differs')
    mapping = read_json(map_path)['items']
    mapped = indexed(mapping, 'sample_id', 'native sample mapping')
    selected = prepared.get('selected_ids') if flavor == 'recovery' else list(samples)
    scope = prepared.get('scope_inputs', prepared.get('input_count'))
    if (not selected or len(selected) != len(set(selected))
            or not set(selected) <= set(samples) or set(mapped) != set(selected)
            or len(mapped) != scope or summary.get('scope_inputs') != scope):
        raise ValueError('Native selected input scope changed or omits a frozen input')
    workspace = run / 'native/ocr'
    if flavor == 'recovery' and fpath(prepared['workspace']) != workspace:
        raise ValueError('Recovery workspace is outside its isolated run')
    database = workspace / 'workbench.sqlite3'
    tables = read_database(database)
    snapshot_hashes = checked_snapshots(run, flavor, tables)
    versions = indexed(tables['versions'], 'id', 'native version')
    images = indexed(tables['images'], 'id', 'native image')
    results = indexed(tables['results'], 'id', 'native result')
    tasks = indexed(tables['tasks'], 'id', 'native task')
    batches = indexed(tables['screen_batches'], 'id', 'native batch')
    items = indexed(tables['screen_items'], ('input_version_id'), 'native batch input')
    if len(batches) != 1:
        raise ValueError('Native scoring requires one actual batch; child attempts need separate explicit reports')
    batch_id, batch = next(iter(batches.items()))
    policy = object_value(batch['policy'])
    compare_policy(frozen_policy, policy)
    if flavor == 'agent':
        receipts = summary.get('native_batches', [])
        if len(receipts) != 1 or receipts[0]['batch_id'] != batch_id or receipts[0]['policy'] != policy:
            raise ValueError('Actual native batch differs from the Agent receipt')
        if receipts[0].get('resolved_policy_sha256') != fingerprint(policy):
            raise ValueError('Actual resolved native policy hash differs')
    elif summary.get('batch_id') != batch_id or summary.get('resolved_policy') != policy:
        raise ValueError('Actual recovered batch policy/identity differs')
    final_rows = tables['screen_final_tasks']
    rows, used_versions, detail_hashes = [], set(), {}
    for ident in selected:
        sample, imported = samples[ident], mapped[ident]
        original = fpath(sample['image'])
        if digest(original) != sample['image_sha256']:
            raise ValueError('Frozen source image changed: ' + ident)
        copy = fpath(imported.get('input_copy', imported.get('normalized_input')))
        source_hash = imported.get('image_sha256', imported.get('source_image_sha256'))
        if source_hash != sample['image_sha256'] or digest(copy) != source_hash or not copy.is_relative_to(run / 'inputs'):
            raise ValueError('Prepared input copy/source binding changed: ' + ident)
        detail = None
        version_id = imported.get('input_version_id')
        if flavor == 'agent':
            detail_path = run / 'native-receipts' / (ident + '.json')
            if detail_path.is_file():
                detail = read_json(detail_path)
                detail_hashes[str(detail_path)] = digest(detail_path)
                if detail.get('sample_id') != ident or detail.get('source_image_sha256') != source_hash:
                    raise ValueError('Native sample receipt refers to another original input')
                version_id = detail['input_version_id']
        if version_id is None:
            rows.append({'id': ident, 'native_status': 'missing', 'scoring_status': 'missing',
                'input_sha256': source_hash, 'reason': 'No native sample/version receipt',
                'selected_candidate': None, 'edited_view': None, 'human_accepted': False})
            continue
        if version_id in used_versions:
            raise ValueError('Different samples share one native input identity')
        used_versions.add(version_id)
        version = versions.get(version_id)
        item = items.get(version_id)
        if version is None or item is None:
            raise ValueError('Mapped native version/batch input is missing')
        image = images.get(version['image_id'])
        if (image is None or image['active_version'] != version_id or image['id'] != item['image_id']
                or item['batch_id'] != batch_id or image['project_id'] != batch['project_id']
                or item['source_hash'] != version['sha256']):
            raise ValueError('Native source version changed or belongs to another input/batch')
        if imported.get('image_id') and imported['image_id'] != image['id']:
            raise ValueError('Native imported image identity changed')
        native_original = fpath(workspace / image['original_path'])
        native_image = fpath(workspace / version['path'])
        if not native_original.is_relative_to(workspace) or not native_image.is_relative_to(workspace):
            raise ValueError('Native input path escaped the isolated workspace')
        if digest(native_original) != image['sha256'] or image['sha256'] != source_hash or digest(native_image) != version['sha256']:
            raise ValueError('Native original/version bytes changed')
        visible = normalized_pixels(original)
        if (normalized_pixels(native_image) != visible
                or (version['width'], version['height']) != (visible['width'], visible['height'])):
            raise ValueError('Native import pixels/dimensions differ from frozen visible input')
        if any(sample.get(key, visible[key]) != visible[key] for key in ('width', 'height')):
            raise ValueError('Frozen input dimensions differ from visible pixels')
        native_status = item['status']
        status = 'success' if native_status in {'completed', 'needs_review'} else 'failed' if native_status in {'failed', 'cancelled'} else 'running'
        row = {'id': ident, 'input_sha256': source_hash, 'input_version_id': version_id,
            'native_version_sha256': version['sha256'], 'normalized_visible_pixels': visible,
            'native_status': native_status, 'scoring_status': status,
            'review_state': item['review_state'], 'human_accepted': item['review_state'] == 'accepted',
            'issues': json.loads(item['issues']), 'error': item.get('error'),
            'selected_candidate': None, 'edited_view': None, 'result_id': item.get('result_id')}
        if detail and any(detail.get(key) != item.get(key) for key in ('image_id', 'status', 'result_id', 'result_revision', 'review_state')):
            raise ValueError('Native detail changed after its captured input/result receipt')
        if item.get('result_id'):
            result = results.get(item['result_id'])
            if result is None or result['revision'] != item['result_revision']:
                raise ValueError('Selected native result revision changed')
            task = tasks.get(result['task_id'])
            if (task is None or task['kind'] != 'screen_final' or task['status'] != 'succeeded'
                    or task['result_id'] != result['id'] or task['image_id'] != image['id']
                    or task.get('input_version_id', task.get('version_id')) != version_id
                    or sum(entry['task_id'] == task['id'] and entry['input_version_id'] == version_id
                           and entry['batch_id'] == batch_id for entry in final_rows) != 1):
                raise ValueError('Selected native result has no unique final task/batch identity')
            raw, edited = object_value(result['original']), object_value(result['edited'])
            screen = raw.get('screen', {})
            if (raw.get('origin') != 'screen_batch' or raw.get('project_image_version') != version_id
                    or screen.get('batch_id') != batch_id or screen.get('input_sha256') != version['sha256']
                    or screen.get('strategy_version') != policy['strategy_version']):
                raise ValueError('Native final result source/strategy identity differs')
            candidates = json.loads(item['candidates'])
            selection = screen.get('selection', {})
            chosen = [candidate for candidate in candidates if candidate['id'] == selection.get('selected_id')]
            if len(chosen) != 1 or chosen[0]['engine'] != raw.get('engine'):
                raise ValueError('Native final selected candidate identity is missing/ambiguous')
            if not selection.get('modifications') and any(raw.get(key) != chosen[0]['raw'].get(key) for key in ('text', 'tables')):
                raise ValueError('Native final content differs from its selected actual candidate')
            if native_status == 'completed' and item['review_state'] == 'automatic' and not selection.get('automatic_delivery'):
                raise ValueError('Native automatic delivery flag and final status differ')
            if detail:
                captured = detail.get('result') or {}
                if any(captured.get(key) != result[key] for key in ('id', 'task_id', 'revision')) or captured.get('original') != raw or captured.get('edited') != edited:
                    raise ValueError('Native original/edited content changed after detail receipt')
            sources = screen.get('sources', [])
            if not sources:
                raise ValueError('Native selected result has no actual OCR sources')
            links = {entry['task_id']: entry for entry in tables.get('screen_tasks', [])}
            for entry in sources:
                source_result, source_task = results.get(entry['result_id']), tasks.get(entry['task_id'])
                link = links.get(entry['task_id'])
                if (source_result is None or source_task is None or link is None
                        or source_result['task_id'] != source_task['id'] or source_task['result_id'] != source_result['id']
                        or source_task['status'] != 'succeeded' or source_result['revision'] != entry['revision']
                        or link['batch_id'] != batch_id or link['input_version_id'] != version_id
                        or link['prepared_id'] != entry['prepared_id']):
                    raise ValueError('Native selected OCR source/task/crop binding differs')
                observed_raw = object_value(source_result['original'])
                if (observed_raw.get('project_image_version') != version_id
                        or source_task['engine'] != entry['engine']
                        or source_task.get('input_version_id', source_task.get('version_id')) != version_id):
                    raise ValueError('Native OCR source version/model identity differs')
                if link.get('metadata'):
                    metadata = object_value(link['metadata'])
                    if observed_raw.get('screen_input') != metadata or entry.get('prepared_metadata') != metadata:
                        raise ValueError('Native OCR consumed preprocessing metadata differs')
                    crop = fpath(workspace / link['prepared_path'])
                    if not crop.is_relative_to(workspace) or not crop.is_file() or fpath(metadata['path']) != crop:
                        raise ValueError('Native OCR consumed crop path changed')
            final_entry = next(entry for entry in final_rows if entry['task_id'] == task['id'])
            original_digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            if final_entry.get('content_hash') != original_digest:
                raise ValueError('Native final content hash differs from its original result')
            quality = {'automatic_delivery': bool(selection.get('automatic_delivery')) and native_status == 'completed',
                       'review_required': bool(selection.get('review_required')), 'issues': row['issues']}
            def score_view(view):
                output = deepcopy(view)
                # Metadata is visibly delivered on the business-notes sheet.
                # Join only actual observed metadata for context scoring; never
                # add truth text or modify any OCR cell value/sign/digit/span.
                output['text'] = '\n'.join([str(output.get('text', '')),
                    *(str(part.get('text', '')) for part in screen.get('business_metadata', []))])
                output['quality'] = quality
                return output
            unedited_view = {'text': raw.get('text', ''), 'tables': raw.get('tables', [])}
            current_edit = {'text': edited.get('text', ''), 'tables': edited.get('tables', [])}
            row.update(result_revision=result['revision'], manual_edits_present=current_edit != unedited_view,
                manual_revision_touched=result['revision'] != 0,
                selected_candidate=score_view(raw), edited_view=score_view(edited),
                selected_candidate_id=selection['selected_id'], source_receipts=sources,
                result_original_sha256=fingerprint(raw), result_edited_sha256=fingerprint(edited),
                selection_sha256=fingerprint(selection), stored_result_task=task,
                automatic_text_modifications=selection.get('modifications', []))
        elif status == 'success':
            raise ValueError('Native success has no final selected result')
        rows.append(row)
    if not set(items) <= used_versions:
        raise ValueError('Native batch contains an unbound or duplicate input')
    return {'format': 'actual-native-offline-receipt-index-v1',
        'created': datetime.now(timezone.utc).isoformat(), 'native_flavor': flavor,
        'native_run': str(run), 'prepared_sha256': digest(prepared_path),
        'inputs_sha256': digest(inputs_path), 'selected_ids': selected, 'scope_inputs': len(rows),
        'truth_opened': False, 'formal_run_created': False, 'gpu_calls': 0, 'controller_calls': 0,
        'native_source_inventory_sha256': prepared['native_source_inventory_sha256'],
        'bundle_engines_sha256': prepared['bundle_engines_sha256'],
        'frozen_policy_sha256': policy_hash, 'actual_policy': policy,
        'actual_policy_sha256': fingerprint(policy), 'batch_id': batch_id,
        'native_database': str(database), 'native_table_identity_sha256': table_identity(tables),
        'captured_snapshot_sha256': snapshot_hashes, 'detail_receipt_sha256': detail_hashes,
        'native_summary': {'path': str(summary_path), 'sha256': digest(summary_path),
            'status': summary['status'], 'wall_seconds': summary.get('wall_seconds'),
            'wall_scope': summary.get('wall_scope', summary.get('restart_type')),
            'agent_terminal_wall_seconds': summary.get('agent_terminal_wall_seconds'),
            'delivery_completeness': summary.get('delivery_completeness'),
            'model_rounds': summary.get('model_rounds'), 'tool_calls': summary.get('tool_calls')},
        'rows': rows}


def load_scorer(tools_root):
    tools_root = fpath(tools_root)
    sys.path.insert(0, str(tools_root))
    existing = sys.modules.get('run')
    if existing is not None and fpath(existing.__file__) != tools_root / 'run.py':
        raise ValueError('Offline scorer imported another benchmark run module')
    spec = importlib.util.spec_from_file_location('native_independent_offline_score', tools_root / 'score.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if fpath(sys.modules['run'].__file__) != tools_root / 'run.py':
        raise ValueError('Offline scorer helper source differs')
    return module


def report(native_run, inputs_path, truth_path, tools_root, output):
    output, truth_path, tools_root = fpath(output), fpath(truth_path), fpath(tools_root)
    receipt_path = output.with_suffix('.native-index.json')
    if output.exists() or receipt_path.exists():
        raise ValueError('Use fresh evidence filenames; existing native reports are immutable')
    receipt = load_native_index(native_run, inputs_path)
    receipt.update(scorer_sha256=digest(tools_root / 'score.py'),
                   scorer_helper_sha256=digest(tools_root / 'run.py'),
                   adapter_sha256=digest(__file__))
    # The first truth-file read is strictly after the actual selection index.
    write_new(receipt_path, receipt)
    truths = indexed(read_json(truth_path)['samples'], 'id', 'offline truth')
    inputs = indexed(read_json(inputs_path)['samples'], 'id', 'input sample')
    if set(truths) != set(inputs):
        raise ValueError('Offline truth does not match the complete frozen input manifest')
    for ident in receipt['selected_ids']:
        if truths[ident].get('image_sha256') and truths[ident]['image_sha256'] != inputs[ident]['image_sha256']:
            raise ValueError('Offline truth image identity differs')
    scorer = load_scorer(tools_root)
    selected_rows, edited_rows = [], []
    for row in receipt['rows']:
        selected_rows.append(scorer.score_sample(truths[row['id']], row['selected_candidate'], row['scoring_status']))
        edited_rows.append(scorer.score_sample(truths[row['id']], row['edited_view'], row['scoring_status']))
    if table_identity(read_database(receipt['native_database'])) != receipt['native_table_identity_sha256']:
        raise ValueError('Native source/result revisions changed during offline scoring')
    native_counts = Counter(row['native_status'] for row in receipt['rows'])
    def quality(rows):
        return {'summary': scorer.summarize(rows), 'cluster_interval': scorer.clustered_interval(rows),
            'source_grouped': {str(key): scorer.summarize([row for row in rows if row['source_id'] == key])
                for key in sorted({row['source_id'] for row in rows}, key=str)},
            'grouped': {str(key): scorer.summarize([row for row in rows if row['group_id'] == key])
                for key in sorted({row['group_id'] for row in rows}, key=str)},
            'cohort_level': {str(cohort) + '/' + str(level): scorer.summarize([
                row for row in rows if row['cohort'] == cohort and row['level'] == level])
                for cohort, level in sorted({(row['cohort'], row['level']) for row in rows}, key=str)},
            'records': rows}
    result = {'format': 'actual-native-independent-offline-report-v1',
        'scope': 'Frozen simulated regression, actual native outputs; not real smartphone or independent blind generalization',
        'scope_inputs': len(receipt['rows']), 'selected_ids': receipt['selected_ids'],
        'full_input_manifest': len(receipt['rows']) == len(inputs),
        'native_index': {'path': str(receipt_path), 'sha256': digest(receipt_path)},
        'truth_sha256': digest(truth_path), 'native_counts': dict(native_counts),
        'candidate_quality': quality(selected_rows), 'edited_export_view_quality': quality(edited_rows),
        'human_accepted_images': sum(row['human_accepted'] for row in receipt['rows']),
        'manual_edited_images': sum(row.get('manual_edits_present', False) for row in receipt['rows']),
        'manual_revision_touched_images': sum(row.get('manual_revision_touched', False) for row in receipt['rows']),
        'actual_native_wall': receipt['native_summary'],
        'timing_interpretation': 'Only actual harness wall is reported; task/OCR/context/cache/recovery sums are never substituted for batch wall',
        'denominator': 'Every explicitly selected input retained; failed/cancelled score failed, missing score missing, pending/paused score running. Candidate and edited view are separate; neither implies human acceptance.',
        'metadata_interpretation': 'Actual saved business metadata joins the scored context transcript because it is exported on a separate sheet. OCR cell text/spans are unchanged.',
        'formal_run_created': False, 'gpu_calls': 0, 'controller_calls': 0}
    write_new(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('native-run', 'inputs', 'truth', 'benchmark-tools', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    args = parser.parse_args()
    output = fpath(args.output)
    temporary = output.parent / 'native-report-temp'
    temporary.mkdir(parents=True, exist_ok=True)
    for key in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR'):
        os.environ[key] = str(temporary)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    result = report(args.native_run, args.inputs, args.truth, args.benchmark_tools, output)
    print(json.dumps({'output': str(output), 'scope_inputs': result['scope_inputs'],
        'native_counts': result['native_counts'], 'human_accepted_images': result['human_accepted_images'],
        'formal_run_created': False, 'gpu_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
