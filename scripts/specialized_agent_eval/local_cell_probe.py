"""Development-only, source-bound cell reread probe; never changes native results.

Preparation replays complete pixel/topology/anchor proofs before cropping. The
model phase deliberately uses the configured EngineAdapter and its normal GLM
prompt. Labels, workbook edits, candidate selection and human acceptance are not
inputs or outputs of this helper. An empty crop recognition remains evidence.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import importlib
import math
import io
import os
from pathlib import Path
import re
import sys
import time

from common import digest, fingerprint, fpath, frozen_json, indexed, read_json
from native_diagnostics import diagnostic_source

FORMAT = 'development-source-bound-local-cell-probe-v1'
GEOMETRY_BASIS = 'source_pixels_observed_partition_and_independent_row_column_anchors'


def no_reference(value):
    if isinstance(value, dict):
        if value.get('contains_reference_input') is True or value.get('truth_read') is True:
            raise ValueError('Local probe cannot consume reference evidence')
        for child in value.values():
            no_reference(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            no_reference(child)


def check_bound_file(path, sha):
    path = fpath(path)
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha) or digest(path) != sha:
        raise ValueError('Source file digest differs from its frozen binding')
    return path


def table_cells(raw, table_index):
    cells = raw['tables'][table_index]['cells']
    result = {
        (cell['row'], cell['column'], cell.get('row_span', 1), cell.get('column_span', 1)): cell
        for cell in cells}
    if len(result) != len(cells):
        raise ValueError('Candidate has duplicate cell topology')
    return result


def validate_grid(raw, preparation, auxiliary, captured, module):
    no_reference((raw, preparation, auxiliary))
    path = check_bound_file(preparation['input_path'], preparation['input_sha256'])
    if raw['screen']['input_sha256'] != preparation['input_sha256']:
        raise ValueError('Candidate and source pixels are not the same input')
    replay = module.build_screen_diagnostics(raw, preparation, auxiliary)
    if captured != replay or replay.get('version') != 'screen-diagnostics-v2':
        raise ValueError('Full diagnostic proof differs from CPU source replay')
    grid = replay['grid_localization']
    if (grid.get('version') != 'screen-grid-localization-v1'
            or grid.get('reference_input_used') is not False or grid.get('values_modified') is not False
            or grid.get('human_review') != 'not_performed'
            or grid['binding']['input_sha256'] != digest(path)
            or grid['binding']['table_snapshot_sha256'] != fingerprint(raw['tables'])):
        raise ValueError('Grid input/table/evidence/qualification binding differs')
    result = {}
    for table in grid['tables']:
        if table['status'] != 'verified':
            continue
        ti = table['table']
        if ti in result or table.get('reason') is not None or table.get('anchor_conflicts'):
            raise ValueError('Grid has duplicate tables or conflicting anchors')
        cells = table_cells(raw, ti)
        geometries = {}
        for cell in table['cells']:
            key = tuple(cell[k] for k in ('row', 'column', 'row_span', 'column_span'))
            if key not in cells or key in geometries or cell.get('basis') != GEOMETRY_BASIS:
                raise ValueError('Verified cell is not uniquely bound to candidate topology')
            box = cell.get('box')
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (float, int) or not math.isfinite(v) for v in box)
                    or not (0 <= box[0] < box[2] <= preparation['width']
                            and 0 <= box[1] < box[3] <= preparation['height'])):
                raise ValueError('Cell rectangle is invalid or outside source pixels')
            if table['cell_boxes'].get(':'.join(map(str, key))) != box:
                raise ValueError('Cell lookup rectangle differs from complete proof')
            geometries[key] = deepcopy(cell)
        if set(cells) != set(geometries):
            raise ValueError('Verified proof does not cover exact candidate topology')
        result[ti] = {'proof': deepcopy(table), 'cells': geometries}
    if sum(len(row['cells']) for row in result.values()) != grid['cell_count']:
        raise ValueError('Verified cell denominator differs from full proof')
    return result


def source_inventory(summary):
    sources = summary.get('sources') or []
    if not sources or summary.get('source_bindings_unchanged') is not True:
        raise ValueError('Missing immutable diagnostic source inventory')
    roots, hashes = set(), {}
    for row in sources:
        frozen = check_bound_file(row['frozen'], row['sha256'])
        root = frozen.parents[2]
        relative = frozen.relative_to(root).as_posix()
        if not relative.startswith('src/ocr_workbench/') or relative in hashes:
            raise ValueError('Invalid or duplicate diagnostic source inventory')
        roots.add(root)
        hashes[relative] = row['sha256']
    if len(roots) != 1:
        raise ValueError('Diagnostic modules come from different frozen sources')
    return next(iter(roots)), hashes


def receipt_record(receipt_path, expected_sha, observations_cache):
    receipt_path = check_bound_file(receipt_path, expected_sha)
    receipt = read_json(receipt_path)
    if (receipt.get('format') != 'development-source-bound-grid-diagnostic-cpu-replay-v1'
            or receipt.get('truth_read') is not False or receipt.get('gpu_or_model_executed') is not False
            or not receipt.get('id', '').startswith('development-')):
        raise ValueError('Receipt is not a label-free development proof')
    path = check_bound_file(receipt['source_observations'], receipt['source_observations_sha256'])
    if path not in observations_cache:
        observations_cache[path] = indexed(read_json(path)['records'], 'id', 'development sample')
    record = observations_cache[path][receipt['id']]
    if fingerprint(record) != receipt['source_record_sha256'] or fingerprint(record['candidate']) != receipt['candidate_sha256']:
        raise ValueError('Actual native candidate changed since diagnostic replay')
    raw = {**deepcopy(record['candidate']['original']), **deepcopy(record['candidate']['edited'])}
    preparation = raw['screen']['prepared']
    auxiliary = raw['screen']['business_metadata_evidence']['auxiliary_evidence']
    if (fingerprint(preparation) != receipt['preparation_sha256']
            or fingerprint(auxiliary) != receipt['auxiliary_sha256']
            or preparation['input_sha256'] != record['image_sha256']
            or record['image_sha256'] != receipt['input_sha256']):
        raise ValueError('Native preparation/auxiliary evidence differs')
    if preparation['input_sha256'] != receipt['input_sha256'] or fpath(preparation['input_path']) != fpath(receipt['input_path']):
        raise ValueError('Receipt refers to a different source image')
    return receipt, raw, preparation, auxiliary


def render_crop(image, geometry, *, scale=3, border=32):
    from PIL import Image
    if type(scale) is not int or not 1 <= scale <= 4 or type(border) is not int or not 0 <= border <= 64:
        raise ValueError('Crop scale/border is outside development work bounds')
    box = geometry['box']
    rectangle = [math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3])]
    if not (0 <= rectangle[0] < rectangle[2] <= image.width and 0 <= rectangle[1] < rectangle[3] <= image.height):
        raise ValueError('Crop cannot include pixels outside source image')
    crop = image.crop(rectangle)
    scaled = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
    padded = Image.new('RGB', (scaled.width+2*border, scaled.height+2*border), 'white')
    padded.paste(scaled, (border, border))
    return padded, {'source_bbox': deepcopy(box), 'integer_including_bbox': rectangle,
            'source_crop_size': list(crop.size), 'scale': scale, 'resampler': 'Pillow.LANCZOS',
            'white_border_pixels': border, 'output_size': list(padded.size),
            'source_coordinate_space': 'exif_normalized_original',
            'source_to_crop_affine': [scale, 0, border-scale*rectangle[0], 0, scale, border-scale*rectangle[1]],
            'pixels_reconstructed_or_corrected': False}


def source_rgb(path):
    from PIL import Image, ImageOps
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            background = Image.new('RGBA', rgba.size, 'white')
            background.alpha_composite(rgba)
            return background.convert('RGB')
        return image.convert('RGB')


def crop_cell(image, geometry, output, *, scale=3, border=32):
    padded, transform = render_crop(image, geometry, scale=scale, border=border)
    output = fpath(output)
    if output.exists():
        raise ValueError('Crop output exists; use a new run')
    output.parent.mkdir(parents=True, exist_ok=True)
    padded.save(output)
    return {**transform, 'crop_path':str(output), 'crop_sha256':digest(output)}


def focus_categories(values, flagged):
    text = ' '.join(str(value) for value in values)
    categories = []
    if flagged:
        categories.append('diagnostic_disagreement')
    if len(set(map(str, values))) > 1:
        categories.append('native_candidate_literal_difference')
    if re.search(r'[Il1O0]', text) and re.search(r'[A-Za-z]{2}', text):
        categories.append('letter_digit_ambiguity')
    if re.search(r'[−–\-+×÷<>≤≥%β]|\\(?:beta|times|pm|leq|geq)', text):
        categories.append('numeric_or_mathematical_symbol')
    if re.search(r'[0-9]', text):
        categories.append('number_identifier_or_numeric_text')
    return categories or ['verified_cell_control']


def prepare(summary_path, inputs_path, engine_prepared_path, output):
    output = fpath(output)
    if output.exists():
        raise ValueError('Probe run exists; never overwrite prior preparation')
    summary_path, inputs_path, engine_prepared_path = map(fpath, (summary_path, inputs_path, engine_prepared_path))
    summary = read_json(summary_path)
    if summary.get('truth_read') is not False or summary.get('validation_or_holdout_read') is not False:
        raise ValueError('Preparation summary must exclude validation and holdout labels')
    source, files = source_inventory(summary)
    inputs = read_json(inputs_path)
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Inputs must exclude reference labels')
    samples = indexed(inputs['samples'], 'id', 'development image')
    if any(not key.startswith('development-') for key in samples):
        raise ValueError('Probe is limited to development inputs')
    engine_prepared = read_json(engine_prepared_path)
    engine_source = fpath(engine_prepared['native_source'])
    engine_files = engine_prepared['native_source_files']
    for relative, sha in engine_files.items():
        check_bound_file(engine_source/relative, sha)
    bundle = fpath(engine_prepared['bundle'])
    check_bound_file(bundle/'config/engines.json', engine_prepared['bundle_engines_sha256'])
    cache, tasks, rejected, readonly = {}, [], [], {}
    for path in (summary_path, inputs_path, engine_prepared_path):
        readonly[str(path)] = digest(path)
    # Validate every receipt and full proof before creating any output crops.
    verified = []
    with diagnostic_source(source, files) as module:
        for pair in summary['comparisons']:
            id = pair['id']
            if id not in samples:
                raise ValueError('Comparison scope is outside frozen development inputs')
            pair_path = fpath(pair['receipt_path'])
            comparison = read_json(pair_path)
            readonly[str(pair_path)] = digest(pair_path)
            if (comparison.get('truth_read') is not False or comparison.get('selection_applied') is not False
                    or comparison.get('values_modified') is not False or comparison.get('id') != id
                    or comparison.get('input_sha256') != samples[id]['image_sha256']):
                raise ValueError('Comparison is not a same-pixel, unchanged development candidate')
            routes = {}
            for route in ('hunyuan', 'glm'):
                receipt_path = fpath(comparison[f'{route}_diagnostic_receipt'])
                # Pair hash comes from the already source-bound immutable summary/receipt set.
                receipt, raw, prep, aux = receipt_record(receipt_path, digest(receipt_path), cache)
                if receipt['source_files_sha256'] != {row['source']:row['sha256'] for row in summary['sources']}:
                    raise ValueError('Grid receipt uses a different diagnostic source')
                if receipt['id'] != id or receipt['candidate_sha256'] != comparison[f'{route}_candidate_sha256']:
                    raise ValueError('Paired candidate differs from its actual route receipt')
                grid = validate_grid(raw, prep, aux, receipt['diagnostics'], module)
                for path in (receipt_path, fpath(receipt['source_observations']), fpath(prep['input_path'])):
                    readonly[str(path)] = digest(path)
                routes[route] = {'raw':raw, 'preparation':prep, 'grid':grid, 'receipt_path':str(receipt_path),
                                 'receipt_sha256':digest(receipt_path), 'candidate_sha256':receipt['candidate_sha256']}
            current, alternate = routes['hunyuan'], routes['glm']
            if current['preparation']['input_sha256'] != alternate['preparation']['input_sha256']:
                raise ValueError('Cross-route crop cannot use different source pixels')
            if not current['grid']:
                rejected.append({'id':id, 'reason':'no_verified_complete_source_pixel_topology_anchor_proof',
                    'native_input_unchanged':True})
                continue
            for ti, row in current['grid'].items():
                if ti not in alternate['grid'] or row['cells'] != alternate['grid'][ti]['cells']:
                    raise ValueError('Cross-route verified cell geometry differs')
            flagged = {(item['location']['table'], item['location']['row'], item['location']['column'])
                for item in comparison['comparison']['diagnostics']['items'] if item['location']['level']=='cell'}
            verified.append((id, routes, flagged))
    output.mkdir(parents=True, exist_ok=False)
    try:
        for id, routes, flagged in verified:
            current = routes['hunyuan']
            source_path = fpath(current['preparation']['input_path'])
            image = source_rgb(source_path)
            for ti, row in current['grid'].items():
                for key, geometry in sorted(row['cells'].items()):
                    position = (ti, key[0], key[1])
                    values = {route:table_cells(data['raw'], ti)[key]['text'] for route,data in routes.items()}
                    task_id = f'{id}__t{ti}_r{key[0]}_c{key[1]}'
                    transform = crop_cell(image, geometry, output/'crops'/f'{task_id}.png')
                    tasks.append({'id':task_id, 'sample_id':id, 'table_index':ti, 'row':key[0], 'column':key[1],
                        'row_span':key[2], 'column_span':key[3], 'source_path':str(source_path),
                        'source_sha256':digest(source_path), 'geometry':geometry, 'transform':transform,
                        'candidate_values':values, 'focus_categories':focus_categories(values.values(), position in flagged),
                        'route_evidence':{route:{k:data[k] for k in ('receipt_path','receipt_sha256','candidate_sha256')}
                                          for route,data in routes.items()},
                        'full_grid_proof_sha256':fingerprint(row['proof']), 'automatic_apply':False})
        check_readonly(readonly)
        manifest = {'format':FORMAT, 'stage':'prepared', 'tasks':tasks, 'task_count':len(tasks), 'rejected':rejected,
            'summary_path':str(summary_path), 'summary_sha256':digest(summary_path),
            'inputs_path':str(inputs_path), 'inputs_sha256':digest(inputs_path),
            'diagnostic_source':str(source), 'diagnostic_source_files':files,
            'engine_prepared_path':str(engine_prepared_path), 'engine_prepared_sha256':digest(engine_prepared_path),
            'engine_source':str(engine_source), 'engine_source_files':engine_files,
            'bundle':str(bundle), 'bundle_engines_sha256':engine_prepared['bundle_engines_sha256'],
            'readonly_bindings':readonly, 'development_only':True, 'truth_read':False,
            'validation_or_holdout_read':False, 'gpu_or_model_executed':False, 'candidate_selection_modified':False,
            'native_review_or_delivery_modified':False, 'local_reread_capability_enabled':False,
            'categories':dict(Counter(category for task in tasks for category in task['focus_categories'])),
            'scope':'All cells in the small development subset with complete verified source-pixel/topology/anchor proof. No automatic repair or human-review qualification.'}
        frozen_json(output/'prepared.json', manifest)
    except BaseException as error:
        frozen_json(output/'preparation-failed.json', {'format':FORMAT, 'error':str(error), 'completed_crops':len(tasks)})
        raise
    return manifest


def check_readonly(bindings):
    for path, sha in bindings.items():
        check_bound_file(path, sha)


def validate_prepared(manifest):
    if (manifest.get('format') != FORMAT or manifest.get('development_only') is not True
            or manifest.get('truth_read') is not False or manifest.get('validation_or_holdout_read') is not False
            or manifest.get('candidate_selection_modified') is not False
            or manifest.get('native_review_or_delivery_modified') is not False
            or manifest.get('local_reread_capability_enabled') is not False):
        raise ValueError('Probe manifest changed scope or qualification')
    if len(manifest['tasks']) != manifest['task_count'] or len({task['id'] for task in manifest['tasks']}) != manifest['task_count']:
        raise ValueError('Probe task denominator changed')
    check_readonly(manifest['readonly_bindings'])
    summary = read_json(check_bound_file(manifest['summary_path'], manifest['summary_sha256']))
    source, files = source_inventory(summary)
    if str(source) != manifest['diagnostic_source'] or files != manifest['diagnostic_source_files']:
        raise ValueError('Prepared probe changed diagnostic source')
    expected_count = sum(row['source_cell_count'] for row in summary['route_summaries'] if row['route']=='hunyuan')
    if manifest['task_count'] != expected_count:
        raise ValueError('Probe dropped verified cells from frozen development denominator')
    cache, routes, seen = {}, {}, set()
    with diagnostic_source(source, files) as module:
        for task in manifest['tasks']:
            if task.get('automatic_apply') is not False or not task['sample_id'].startswith('development-'):
                raise ValueError('Probe cannot automatically apply a cell or change data split')
            check_bound_file(task['source_path'], task['source_sha256'])
            check_bound_file(task['transform']['crop_path'], task['transform']['crop_sha256'])
            ti, key = task['table_index'], tuple(task[k] for k in ('row','column','row_span','column_span'))
            identity = (task['sample_id'], ti, key)
            if identity in seen:
                raise ValueError('Probe contains duplicate source cell identity')
            seen.add(identity)
            for route in ('hunyuan','glm'):
                evidence = task['route_evidence'][route]
                receipt_path = evidence['receipt_path']
                if (receipt_path, route) not in routes:
                    receipt, raw, prep, aux = receipt_record(receipt_path, evidence['receipt_sha256'], cache)
                    grid = validate_grid(raw, prep, aux, receipt['diagnostics'], module)
                    routes[(receipt_path,route)] = (receipt,raw,prep,grid)
                receipt, raw, prep, grid = routes[(receipt_path,route)]
                if (receipt['id'] != task['sample_id'] or receipt['candidate_sha256'] != evidence['candidate_sha256']
                        or prep['input_sha256'] != task['source_sha256'] or ti not in grid
                        or key not in grid[ti]['cells'] or grid[ti]['cells'][key] != task['geometry']
                        or table_cells(raw,ti)[key]['text'] != task['candidate_values'][route]):
                    raise ValueError('Prepared task differs from full actual source proof/candidate')
                if route=='hunyuan' and fingerprint(grid[ti]['proof']) != task['full_grid_proof_sha256']:
                    raise ValueError('Prepared task has a changed complete grid proof')
            image = source_rgb(task['source_path'])
            rendered, expected_transform = render_crop(image, task['geometry'])
            actual_transform = {k:v for k,v in task['transform'].items() if k not in ('crop_path','crop_sha256')}
            pixels = io.BytesIO()
            rendered.save(pixels, format='PNG')
            if actual_transform != expected_transform or Path(task['transform']['crop_path']).read_bytes() != pixels.getvalue():
                raise ValueError('Crop pixels or source-to-crop transform differs from verified source')
    for relative, sha in manifest['engine_source_files'].items():
        check_bound_file(fpath(manifest['engine_source'])/relative, sha)
    check_bound_file(fpath(manifest['bundle'])/'config/engines.json', manifest['bundle_engines_sha256'])


def run(prepared_path, output):
    """Caller must allocate a serial GPU slot; the adapter additionally locks it."""
    prepared_path, output = fpath(prepared_path), fpath(output)
    manifest = read_json(prepared_path)
    validate_prepared(manifest)
    if output.exists():
        raise ValueError('Recognition run exists; never replay or overwrite model evidence')
    for name in ('TEMP','TMP','LOCALAPPDATA','APPDATA','HF_HOME','TORCH_HOME','XDG_CACHE_HOME'):
        if not os.environ.get(name) or fpath(os.environ[name]).drive.upper() != 'F:':
            raise ValueError('Model subprocess cache/temp/application-data must be on F')
    output.mkdir(parents=True, exist_ok=False)
    frozen_json(output/'started.json', {'format':FORMAT, 'pid':os.getpid(), 'start_unix':time.time(),
        'prepared_path':str(prepared_path), 'prepared_sha256':digest(prepared_path), 'tasks':manifest['task_count'],
        'truth_read':False, 'gpu_serial_slot_required':True})
    source = fpath(manifest['engine_source'])
    previous = {name:value for name,value in sys.modules.items() if name=='ocr_workbench' or name.startswith('ocr_workbench.')}
    for name in previous:
        sys.modules.pop(name)
    sys.path.insert(0, str(source/'src'))
    adapter = None
    start = time.perf_counter()
    records, error = [], None
    try:
        # CPU prepare never imports or instantiates this adapter.
        adapter_type = importlib.import_module('ocr_workbench.adapter').EngineAdapter
        adapter = adapter_type(manifest['bundle'], 'glm', output/'sessions', worker_source=source/'src')
        load_start = time.perf_counter()
        adapter.load()
        frozen_json(output/'ready.json', {'load_wall_seconds':time.perf_counter()-load_start,
            'load_model_seconds':adapter.load_seconds, 'worker_pid':adapter.process.pid,
            'session_id':adapter.ipc.name})
        for task in manifest['tasks']:
            recognition_output = output/'raw'/task['id']
            check_bound_file(task['transform']['crop_path'], task['transform']['crop_sha256'])
            recognize_start = time.perf_counter()
            row = {'task_id':task['id'], 'crop_sha256':task['transform']['crop_sha256'],
                'source_sha256':task['source_sha256'], 'automatic_apply':False, 'human_review':'not_performed'}
            try:
                result = adapter.recognize(task['transform']['crop_path'], recognition_output, allow_empty=True)
                row.update(status='success', result=result, result_sha256=fingerprint(result))
            except Exception as failure:
                row.update(status='failed', error_type=type(failure).__name__, error=str(failure))
            row['recognition_wall_seconds'] = time.perf_counter()-recognize_start
            frozen_json(output/'receipts'/f'{task["id"]}.json', row)
            records.append(row)
            print(f'{len(records)}/{manifest["task_count"]} {task["id"]} {row["status"]}', flush=True)
            if row['status']=='failed':
                # Worker errors are preserved; never restart a dead worker implicitly.
                if adapter.process is None or adapter.process.poll() is not None:
                    raise RuntimeError('Local probe worker exited; remaining tasks were not run')
        validate_prepared(manifest)
    except BaseException as failure:
        error = {'type':type(failure).__name__, 'message':str(failure)}
    finally:
        if adapter is not None:
            adapter.unload()
        sys.path.pop(0)
        for name in list(sys.modules):
            if name=='ocr_workbench' or name.startswith('ocr_workbench.'):
                sys.modules.pop(name)
        sys.modules.update(previous)
    report = {'format':FORMAT, 'stage':'model_terminal', 'prepared_sha256':digest(prepared_path),
        'task_count':manifest['task_count'], 'attempted_tasks':len(records), 'records':records,
        'wall_seconds':time.perf_counter()-start, 'error':error, 'gpu_or_model_executed':True,
        'real_engine_adapter':True, 'engine':'glm', 'truth_read':False, 'validation_or_holdout_read':False,
        'candidate_selection_modified':False, 'native_review_or_delivery_modified':False,
        'local_reread_capability_enabled':False, 'human_review':'not_performed',
        'worker_alive_after_unload':bool(adapter and adapter.process and adapter.process.poll() is None)}
    frozen_json(output/'summary.json', report)
    if error:
        raise RuntimeError(error['message'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pre = sub.add_parser('prepare')
    pre.add_argument('--summary', required=True)
    pre.add_argument('--inputs', required=True)
    pre.add_argument('--engine-prepared', required=True)
    pre.add_argument('--output', required=True)
    execute = sub.add_parser('run')
    execute.add_argument('--prepared', required=True)
    execute.add_argument('--output', required=True)
    args = parser.parse_args()
    result = prepare(args.summary,args.inputs,args.engine_prepared,args.output) if args.command=='prepare' else run(args.prepared,args.output)
    print({'stage':result['stage'], 'task_count':result['task_count']}, flush=True)


if __name__=='__main__':
    main()
