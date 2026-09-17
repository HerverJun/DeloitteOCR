"""Freeze NEW canonical document groups for the expanded TableFormer experiment.

Uses the same public sources and qualification rules as geometry v2. Excludes
all 320 previously prepared candidates, not just the previously scored splits.
No model is imported. Labels stay separate from inference input.
"""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pyarrow.parquet as pq
import requests
from PIL import Image

from freeze_geometry_v2 import ROOT, REVISION, digest, phash, write
sys.path.insert(0, str(ROOT / 'src'))
from ocr_workbench.tables import parse_tables
from ocr_workbench.editing import tables_html

SEED = 'tableformer-expanded-20260914'


def near_duplicate(feature, prior):
    h, values, shape, _ = feature
    for old_h, old_values, old_shape, old_id in prior:
        distance = (h ^ old_h).bit_count()
        similarity = len(values & old_values) / max(1, len(values | old_values))
        if distance <= 6 or (shape == old_shape and similarity >= .9):
            return {'prior': old_id, 'phash_distance': distance, 'text_jaccard': similarity}
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--candidate-count', type=int, default=160)
    p.add_argument('--test-count', type=int, default=120)
    a = p.parse_args()
    if a.test_count < 120 or a.candidate_count < a.test_count:
        raise ValueError('At least 120 new test documents are required')
    if (a.output / 'split-lock.json').exists():
        raise ValueError('Already frozen; historical outputs are immutable')
    a.output.mkdir(parents=True, exist_ok=True)
    prior_dataset = ROOT / 'build/table-matching-v2/dataset'
    historical_path = ROOT / 'build/document-workflow/geometry-holdout/frozen/manifest.json'
    historical = json.loads(historical_path.read_text('utf-8'))
    shard = ROOT / 'build/document-workflow/geometry-holdout/PubTables-1M_OTSL.parquet'
    source = json.loads(shard.with_suffix('.download.json').read_text('utf-8'))
    if digest(shard.read_bytes()) != source['sha256']:
        raise ValueError('Source parquet changed')
    table = pq.read_table(shard)
    indices = {Path(n).stem: i for i, n in enumerate(table['filename'].to_pylist())}
    excluded = set(historical.get('excluded_prior_document_ids', []))
    features = []
    prior_hashes = {}
    for s in historical['samples']:
        excluded.add(s['original_document_id'])
        features.append((phash((historical_path.parent / s['image']).read_bytes()),
                         {''.join(c['text'].split()) for c in s['targets'] if c['text'].strip()},
                         (s['table']['rows'], s['table']['columns']), s['id']))
    for file in sorted((prior_dataset / 'canonical').glob('*.json')):
        canonical = json.loads(file.read_text('utf-8'))
        key = canonical['structure_id']; excluded.add(key.split('_table_')[0])
        prior_hashes[str(file.resolve())] = digest(file.read_bytes())
        if key not in indices:
            raise ValueError('Cannot audit previously prepared canonical table')
        row = table.slice(indices[key], 1).to_pylist()[0]
        values = {''.join(''.join(c['tokens']).split()) for c in row['cells'][0] if c['tokens']}
        features.append((phash(row['image']['bytes']), values, (row['rows'], row['cols']), key))
    protocol = {'version': 1, 'seed': SEED, 'test_documents': a.test_count,
                'candidate_pool': a.candidate_count, 'prior_prepared_canonical_sha256': prior_hashes,
                'historical_manifest_sha256': digest(historical_path.read_bytes()),
                'prior_split_lock_sha256': digest((prior_dataset / 'split-lock.json').read_bytes()),
                'excluded_documents': sorted(excluded), 'source_revision': REVISION,
                'source_shard_sha256': source['sha256'],
                'selection': 'First qualified unseen canonical documents in pinned archive order; seeded hash selects test from pool',
                'qualification': 'Official test split, complete cell geometry/spans, 50-250 cells; conversion error <=2px; roundoff <=1px',
                'near_duplicate_rules': {'phash_distance_max': 6, 'same_shape_text_jaccard_min': .9},
                'scope': 'PubTables rendered crops; fixed reference structure and identical real PP-OCR inputs; not native/scanned/Chinese end-to-end',
                'development_validation': 'Reuse v2 development 80 and validation 60; do not retune the matcher',
                'test_inference': 'Requires a new selection seal AFTER expanded development and validation reports'}
    protocol_path = a.output / 'protocol.json'
    if protocol_path.exists() and json.loads(protocol_path.read_text('utf-8')) != protocol:
        raise ValueError('Preparation protocol changed')
    write(protocol_path, protocol)
    selected = []; seen = set(excluded); rejected = Counter(); duplicates = []
    cache = a.output / 'canonical'; cache.mkdir(exist_ok=True)
    order = a.output / 'canonical-order.jsonl'

    def consider(canonical):
        key = canonical['structure_id']; doc = key.split('_table_')[0]
        if doc in seen or key not in indices or canonical['split'] != 'test' or canonical['exclude_for_structure']:
            return False
        try:
            row = table.slice(indices[key], 1).to_pylist()[0]
            structures = parse_tables('<table>' + ''.join(row['html_restored']) + '</table>')
            if len(structures) != 1: raise ValueError('multiple_structures')
            structure = structures[0]; cells = structure['cells']; labels = row['cells'][0]
            originals = sorted(canonical['cells'], key=lambda c: (min(c['row_nums']), min(c['column_nums'])))
            if not 50 <= len(cells) <= 250 or len(cells) != len(labels) or len(cells) != len(originals):
                raise ValueError('cell_count')
            content = row['image']['bytes']
            with Image.open(io.BytesIO(content)) as im: width, height = im.size
            pb, crop = canonical['pdf_table_bbox'], row['table_bbox']
            sx, sy = (crop[2]-crop[0])/(pb[2]-pb[0]), (crop[3]-crop[1])/(pb[3]-pb[1])
            transform = [sx, 0, crop[0]-sx*pb[0], 0, sy, crop[1]-sy*pb[1], 0, 0, 1]
            targets = []; max_error = 0
            for c, original, label in zip(cells, originals, labels):
                rs, cs = original['row_nums'], original['column_nums']
                if rs != list(range(min(rs), max(rs)+1)) or cs != list(range(min(cs), max(cs)+1)):
                    raise ValueError('noncontiguous_span')
                if (c['row'], c['column'], c['row_span'], c['column_span']) != (min(rs), min(cs), len(rs), len(cs)):
                    raise ValueError('span_mismatch')
                c['text'] = ''.join(label['tokens']); c['confidence'] = None
                if '\ufffd' in c['text']: raise ValueError('invalid_transcription')
                b = original['pdf_bbox']; box = label['bbox'][:4]
                mapped = [sx*b[0]+transform[2], sy*b[1]+transform[5], sx*b[2]+transform[2], sy*b[3]+transform[5]]
                error = max(abs(x-y) for x, y in zip(box, mapped)); max_error = max(max_error, error)
                if error > 2: raise ValueError('coordinate_mismatch')
                if box[0] < -1 or box[1] < -1 or box[2] > width+1 or box[3] > height+1: raise ValueError('out_of_bounds')
                box = [max(0,box[0]), max(0,box[1]), min(width,box[2]), min(height,box[3])]
                if box[0] >= box[2] or box[1] >= box[3]: raise ValueError('invalid_box')
                targets.append({**{k:c[k] for k in ('row','column','row_span','column_span','text')}, 'box':box,
                                'canonical_pdf_bbox': b})
            counts = Counter(''.join(c['text'].split()) for c in cells)
            values = {v for v in counts if v}; h = phash(content)
            feature = (h, values, (structure['rows'], structure['columns']), key)
            duplicate = near_duplicate(feature, features)
            if duplicate:
                duplicates.append({'id':key, **duplicate}); raise ValueError('near_duplicate')
            for t in targets:
                t['tags'] = [tag for tag, condition in [('merged',t['row_span']>1 or t['column_span']>1),
                    ('empty',not t['text'].strip()),('repeated',bool(t['text'].strip()) and counts[''.join(t['text'].split())]>1)] if condition]
            sample = {'id':key, 'cohort':'pubtables-1m', 'original_document_id':doc, 'group_id':doc,
                      'width':width, 'height':height, 'image':'images/'+row['filename'], 'sha256':digest(content),
                      'original_filename':row['filename'], 'fixed_edit':{'text':tables_html([structure]),'tables':[structure]},
                      'input_kind':'rendered-table-crop', 'table_region_source':'input-table-crop',
                      'targets':targets, 'tags':sorted({tag for t in targets for tag in t['tags']}),
                      'canonical_geometry_max_pixel_difference':max_error, 'coordinate_transform':transform}
            selected.append(sample); seen.add(doc); features.append(feature)
            (a.output / 'images').mkdir(exist_ok=True)
            (a.output / sample['image']).write_bytes(content)
            write(cache / (key+'.json'), canonical)
            if len(selected) % 10 == 0: print('qualified',len(selected),'rejected',sum(rejected.values()),flush=True)
            return True
        except (ValueError, KeyError, ZeroDivisionError) as e:
            rejected[str(e)] += 1
            return False

    if order.exists():
        for line in order.read_text('utf-8').splitlines():
            consider(json.loads((cache / (json.loads(line)+'.json')).read_text('utf-8')))
    if len(selected) < a.candidate_count:
        session = requests.Session(); session.trust_env = False
        url = f'https://hf-mirror.com/datasets/bsmock/pubtables-1m/resolve/{REVISION}/PubTables-1M-PDF_Annotations.tar.gz'
        with session.get(url, stream=True, timeout=(30,90)) as response:
            response.raise_for_status()
            with tarfile.open(fileobj=response.raw, mode='r|gz') as archive:
                for member in archive:
                    if not member.isfile(): continue
                    doc = Path(member.name).name.split('_tables')[0]
                    if doc in seen: continue
                    for canonical in json.load(archive.extractfile(member)):
                        if consider(canonical):
                            with order.open('a',encoding='utf-8') as f: f.write(json.dumps(canonical['structure_id'])+'\n')
                            break
                    if len(selected) >= a.candidate_count: break
    if len(selected) < a.candidate_count: raise ValueError('Insufficient qualified candidate pool; no seal written')
    selected.sort(key=lambda s: digest((SEED+':'+s['id']).encode()))
    chosen = selected[:a.test_count]
    inputs = []; annotations = []
    for row in chosen:
        inp = {k:v for k,v in row.items() if k not in ('targets','tags','coordinate_transform','canonical_geometry_max_pixel_difference')}
        inp['image'] = '../'+inp['image']; inputs.append(inp)
        annotations.append({k:v for k,v in row.items() if k not in ('fixed_edit','image')})
    write(a.output/'inputs/test.json', {'protocol_version':2,'split':'test','track':'control','samples':inputs})
    write(a.output/'sealed/test.annotations.json', {'protocol_version':2,'split':'test','samples':annotations})
    summary = {'tables':len(chosen),'documents':len({s['group_id'] for s in chosen}),
               'targets':sum(len(s['targets']) for s in chosen),
               'target_tags':dict(Counter(t for s in chosen for c in s['targets'] for t in c['tags']))}
    write(a.output/'qualification.json', {'summary':summary,'rejected':dict(rejected),'near_duplicates':duplicates,
          'excluded_prior_documents':len(excluded),'prior_canonical_candidates':len(prior_hashes),
          'source':source,'scope':protocol['scope'],'unused_new_candidates':len(selected)-len(chosen)})
    files = ['inputs/test.json','sealed/test.annotations.json','protocol.json','qualification.json']
    write(a.output/'split-lock.json', {'version':2,'seed':SEED,'new_test':True,
          'files':{f:digest((a.output/f).read_bytes()) for f in files},'summary':{'test':summary}})
    print(json.dumps(summary),flush=True)


if __name__ == '__main__': main()
