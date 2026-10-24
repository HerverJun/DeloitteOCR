"""CPU-only preparation of bounded, independent Paddle full-cell visual probes.

Consumes saved actual geometry, never a primary OCR candidate or offline truth.
It neither starts a model nor attaches independently detected cells to Hunyuan.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from PIL import Image
from ocr_workbench.geometry_providers import paddle_tables
from ocr_workbench.screen_evidence import build_screen_visual_snapshot

VERSION = 'independent-paddle-full-cell-visual-inputs-v1'
AMOUNT_HEADER = re.compile(r'金额|数量|单价|余额|预算|税额|税率|执行|amount|quantity|price|balance|rate', re.I)
IDENTITY_HEADER = re.compile(r'序号|编号|账号|账户|证件|流水|日期|时间|identifier|\b(?:id|code|account|reference|date|time)\b', re.I)
NUMBER = re.compile(r'^[￥¥$€£]?[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[%％]?$|^[（(]\d+(?:\.\d+)?[）)]$')


def fpath(value):
    path = Path(value).resolve()
    if path.drive.upper() != 'F:':
        raise ValueError('Visual probe inputs and outputs must stay on F:')
    return path


def digest(path):
    return hashlib.sha256(fpath(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(fpath(path).read_text('utf-8'))


def plain(value):
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def independent_candidate(tables):
    """Keep Paddle's own topology/text and verified exact detector lineage."""
    output = []
    for table in tables:
        cells = []
        for cell in table['cells']:
            converted = {'id': cell['id'], 'text': cell['text'],
                'row': cell['row_start'], 'column': cell['column_start'],
                'row_span': cell['row_end']-cell['row_start'],
                'column_span': cell['column_end']-cell['column_start'],
                'polygon': deepcopy(cell['cell_polygon']),
                'geometry_origin': cell['geometry_origin'],
                'independent_geometry_trace': deepcopy(cell)}
            cells.append(converted)
        output.append({'id': table['id'], 'region_id': table['region_id'],
            'rows': max((c['row_end'] for c in table['cells']), default=0),
            'columns': max((c['column_end'] for c in table['cells']), default=0),
            'polygon': deepcopy(table['polygon']), 'cells': cells,
            'detector_lineage': deepcopy(table.get('lineage', []))})
    return {'status': 'success', 'engine': 'paddle-independent-geometry',
            'coordinate_space': 'exif_normalized_original', 'text': '', 'tables': output}


def full_cell_supported(cell, table):
    trace = cell['independent_geometry_trace']
    if trace['geometry_origin'] != 'direct_detection' or trace.get('reason_codes') or not trace.get('cell_polygon'):
        return False
    derivation = trace.get('derivation', [])
    if len(derivation) != 1:
        return False
    step = derivation[0]
    if (step.get('full_cell_verified_lineage') is not True
            or step.get('relationship') != 'unchanged_identity' or len(step.get('parents', [])) != 1):
        return False
    parent = next((r for r in table['detector_lineage'] if r['id'] == step['parents'][0]), None)
    return bool(parent and parent.get('stage') == 'direct_detection'
                and parent.get('box') == step.get('box') and type(parent.get('score')) in {int, float}
                and parent['score'] >= .8)


def choose_issues(raw, limit=2):
    """Observed numeric formatting/header cues only; prefer varied probe kinds."""
    eligible = []
    for ti, table in enumerate(raw['tables']):
        for cell in table['cells']:
            value = str(cell['text']).strip()
            if not full_cell_supported(cell, table) or not NUMBER.fullmatch(value):
                continue
            if re.fullmatch(r'0\d{3,}|\d{16,}', value):
                continue  # identifiers do not establish an amount target
            header = ' '.join(str(c['text']) for c in table['cells']
                              if c['column'] == cell['column'] and c['row'] < cell['row'])
            if IDENTITY_HEADER.search(header):
                continue
            core = value.lstrip('￥¥$€£')
            kind = ('negative' if core.startswith(('-', '−', '(', '（')) else
                    'percent' if value.endswith(('%', '％')) else
                    'decimal_amount' if '.' in value or value.startswith(('￥', '¥', '$', '€', '£')) else
                    'header_quantity_or_amount' if AMOUNT_HEADER.search(header) else None)
            if kind is not None:
                eligible.append({'table': ti, 'row': cell['row'], 'column': cell['column'],
                    'code': 'independent_actual_' + kind + '_probe', 'probe_kind': kind,
                    'independent_cell_id': cell['id']})
    order = {'negative': 0, 'percent': 1, 'decimal_amount': 2, 'header_quantity_or_amount': 3}
    eligible.sort(key=lambda r: (order[r['probe_kind']], r['table'], r['row'], r['column']))
    chosen, seen = [], set()
    for row in eligible:
        if row['probe_kind'] not in seen:
            chosen.append(row)
            seen.add(row['probe_kind'])
            if len(chosen) == limit:
                return chosen
    for row in eligible:
        if row not in chosen:
            chosen.append(row)
            if len(chosen) == limit:
                break
    return chosen


def prepare(experiment, output, max_targets=2):
    if type(max_targets) is not int or not 1 <= max_targets <= 2:
        raise ValueError('Use at most two actual cells per image')
    experiment, output = fpath(experiment), fpath(output)
    receipt_path = experiment / 'receipt.json'
    receipt = read(receipt_path)
    rows = receipt['rows']
    if not 1 <= len(rows) <= 4 or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Use one to four distinct actual experiment images')
    adapter = ROOT / 'src/ocr_workbench/geometry_providers.py'
    if digest(adapter) != receipt['geometry_worker']['files']['geometry_providers.py']:
        raise ValueError('Actual geometry adapter source changed')
    prepared = []
    for row in rows:
        directory = fpath(experiment / row['directory'])
        if not directory.is_relative_to(experiment):
            raise ValueError('Actual experiment directory escaped its run')
        if not any(s.get('stage') == 'geometry-paddle' and s.get('id') == row['id']
                   and s.get('status') == 'succeeded' for s in receipt['stages']):
            raise ValueError('No successful actual geometry receipt')
        image = fpath(row['normalized_image'])
        if digest(row['image']) != row['image_sha256'] or digest(image) != row['normalized_sha256']:
            raise ValueError('Actual input image changed')
        request_path = directory / 'geometry-paddle/actual-input-request.json'
        request = read(request_path)
        context_path = directory / 'context/context.json'
        context = read(context_path)
        manifest_path = directory / 'context/prepared/screen-preparation.json'
        manifest = read(manifest_path)
        if (request['image_sha256'] != digest(image) or request['image_version'] != digest(image)
                or request['actual_context_sha256'] != digest(context_path)
                or request['preparation_manifest_sha256'] != digest(manifest_path)):
            raise ValueError('Actual geometry input/context/preparation binding changed')
        if (request.get('ocr_blocks', []) != context.get('ocr_blocks', [])
                or request.get('ocr_source') != context.get('ocr_source')):
            raise ValueError('Geometry OCR evidence differs from the actual bound context')
        with Image.open(image) as pixels:
            if pixels.size != (manifest['width'], manifest['height']):
                raise ValueError('Actual source dimensions changed')
        regions = {r['id']: r for r in manifest['business_regions']}
        for region in request['regions']:
            source = regions.get(region['id'])
            if not source or region['box'] != source.get('table_box', source['box']):
                raise ValueError('Geometry did not consume the actual business region')
        observed_path = directory / 'geometry-paddle/observed.json'
        adapted_path = directory / 'geometry-paddle/adapted-geometry.json'
        observed = read(observed_path)
        if observed.get('status') != 'success' or observed.get('component') != 'paddle-table-v2':
            raise ValueError('Not a successful actual Paddle geometry result')
        adapted = read(adapted_path)
        reconstructed = plain(paddle_tables(observed, manifest['width'], manifest['height']))
        if adapted != reconstructed:
            raise ValueError('Saved adapted geometry differs from actual detector/structure lineage')
        expected_regions = {r['id']: r['box'] for r in request['regions']}
        if any(t.get('region_id') not in expected_regions or t.get('table_box') != expected_regions[t['region_id']]
               for t in observed.get('tables', [])):
            raise ValueError('Actual geometry table lost its requested region binding')
        raw = independent_candidate(adapted)
        issues = choose_issues(raw, max_targets)
        snapshot = build_screen_visual_snapshot(raw, manifest, issues, image, max_targets=max_targets)
        cell_by_id = {c['id']: c for t in raw['tables'] for c in t['cells']}
        issue_by_position = {(i['table'], i['row'], i['column']): i for i in issues}
        for target in snapshot['source_targets']:
            chosen = issue_by_position[(target['table'], target['row'], target['column'])]
            target['independent_cell_id'] = chosen['independent_cell_id']
            target['geometry_level'] = 'independent_full_cell'
            target['geometry_trace'] = cell_by_id[chosen['independent_cell_id']]['independent_geometry_trace']
        provenance = {name: {'path': str(path), 'sha256': digest(path)} for name, path in
            [('experiment_receipt', receipt_path), ('source_image', fpath(row['image'])), ('normalized_image', image),
             ('actual_request', request_path), ('actual_context', context_path), ('preparation_manifest', manifest_path),
             ('actual_geometry', observed_path), ('adapted_geometry', adapted_path)]}
        snapshot.update(scope='independent_actual_paddle_full_cells; not attached to any Hunyuan result',
                        truth_read=False, provenance=provenance)
        prepared.append({'id': row['id'], 'image': str(image), 'directory': row['directory'],
            'targets': len(snapshot['targets']), 'status': 'prepared' if snapshot['targets'] else 'skipped',
            'snapshot': snapshot, 'independent_candidate': raw})
    output.mkdir(parents=True, exist_ok=False)
    inputs = []
    for row in prepared:
        folder = output / row['directory']
        folder.mkdir()
        for name, value in [('input-snapshot.json', row['snapshot']), ('independent-candidate.json', row['independent_candidate'])]:
            (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        inputs.append({key: row[key] for key in ('id', 'image', 'directory', 'targets', 'status')})
        inputs[-1].update(snapshot=str(folder / 'input-snapshot.json'), snapshot_sha256=digest(folder / 'input-snapshot.json'))
    result = {'version': VERSION, 'mode': 'prepared_only', 'truth_read': False,
        'historical_ocr_candidates_opened': False, 'gpu_started': False,
        'driver_sha256': digest(Path(__file__)), 'geometry_adapter_sha256': digest(adapter),
        'source_receipt_sha256': digest(receipt_path), 'max_targets_per_image': max_targets,
        'scope': 'Independent geometry candidate visual proposals only; no main OCR correction or accuracy claim',
        'items': inputs}
    (output / 'prepared.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-targets', type=int, default=2)
    args = parser.parse_args()
    result = prepare(args.experiment, args.output, args.max_targets)
    print(json.dumps({'prepared': str(args.output / 'prepared.json'),
        'images': len(result['items']), 'targets': sum(r['targets'] for r in result['items']), 'gpu_started': False}))


if __name__ == '__main__':
    main()
