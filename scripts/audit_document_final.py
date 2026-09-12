"""Replay saved model artifacts, time cached reads, and record final code lineage."""
import hashlib
import inspect
import json
from pathlib import Path
import statistics
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from ocr_workbench.geometry import conservative_mapping, geometry_view
from ocr_workbench.store import Store

base = root / 'build/document-workflow'
out = root / 'audit/document-workflow-20260913'
manifest = json.loads((base / 'geometry-holdout/frozen/manifest.json').read_text('utf-8'))
records = []
for sample in manifest['samples']:
    folder = base / 'geometry-evaluation/geometry' / sample['id']
    prior = json.loads((folder / 'evaluation.json').read_text('utf-8'))
    raw = json.loads((folder / 'geometry.json').read_text('utf-8'))
    mappings = conservative_mapping(sample['fixed_edit'], raw, sample['width'], sample['height'])
    identical = mappings == prior.get('mappings') if prior['status'] == 'success' else None
    assert identical is not False, sample['id']
    if prior['status'] != 'success':
        assert all(m['level'] == 'image' and m['polygon'] is None for m in mappings)
    records.append({'id': sample['id'], 'prior_status': prior['status'], 'identical_mappings': identical,
                    'raw_sha256': hashlib.sha256((folder / 'geometry.json').read_bytes()).hexdigest(),
                    'levels': {level: sum(m['level'] == level for m in mappings) for level in ('cell','region','image')}})
report = {'passed': True, 'scope': 'No inference rerun. Replay final mapping code on unchanged saved Paddle artifacts. 99 successful outputs must be identical; malformed HTML safely degrades to image without claiming measured precision.',
          'original_run_lock': json.loads((base / 'geometry-evaluation/run-lock.json').read_text('utf-8')),
          'final_geometry_sha256': hashlib.sha256((root / 'src/ocr_workbench/geometry.py').read_bytes()).hexdigest(),
          'mapping_function_sha256': hashlib.sha256(inspect.getsource(conservative_mapping).encode()).hexdigest(),
          'records': records}
(out / 'final-mapping-replay.json').write_text(json.dumps(report, indent=2), 'utf-8')

def summary(values):
    ordered = sorted(values)
    return {'n':len(values), 'median_ms': statistics.median(values),
            'p90_ms':ordered[round((len(values)-1)*.9)], 'p95_ms':ordered[round((len(values)-1)*.95)]}

ui = out / 'ui-07'
seed = json.loads((ui / 'seed.json').read_text('utf-8'))
store = Store(ui / 'workspace')
target = {'kind':'cell','table':0,'row':1,'column':1}
geometry_view(store, seed['result'], target)
values = []
for _ in range(200):
    start = time.perf_counter()
    assert geometry_view(store, seed['result'], target)['evidence']
    values.append((time.perf_counter()-start)*1000)
performance = {'scope':'Warm persisted geometry_view incl. SQLite and decoding, no GPU. UI paired click/DOM timings are separate, automated and not human correction time.',
               'backend_cached':summary(values),
               'ui_cached_pairs':summary(json.loads((ui / 'ui-location-timing.json').read_text('utf-8'))['milliseconds']),
               'ppocr_cold':json.loads((base / 'geometry-evaluation/ppocr-timing.json').read_text('utf-8')),
               'geometry_cold':json.loads((base / 'geometry-evaluation/geometry-timing.json').read_text('utf-8')),
               'human_review':{'median':None,'p90':None,'time_saved':None,'status':'not measured'}}
(out / 'final-performance.json').write_text(json.dumps(performance, indent=2), 'utf-8')
print(json.dumps({'replay_passed':True,'performance':performance}, indent=2))
