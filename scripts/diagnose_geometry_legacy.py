"""M0: read-only historical gate and candidate-oracle audit; never changes mapping."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ocr_workbench.geometry import conservative_mapping, iou, topology
from ocr_workbench.tables import parse_tables


def matching_upper_bound(edges):
    """Maximum cardinality bipartite matching, not greedy best-IoU counting."""
    owner = {}
    def visit(target, seen):
        for candidate in edges[target]:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in owner or visit(owner[candidate], seen):
                owner[candidate] = target
                return True
        return False
    for target in range(len(edges)):
        visit(target, set())
    return len(owner)


def oracle(targets, boxes):
    valid = [b for b in boxes if len(b) == 4 and b[2] > b[0] and b[3] > b[1]]
    edges = [[j for j, b in enumerate(valid) if iou(t['box'], b) >= .5] for t in targets]
    return {'candidates': len(valid), 'invalid_candidates': len(boxes)-len(valid),
            'any_candidate': sum(bool(e) for e in edges), 'one_to_one': matching_upper_bound(edges)}


def audit(sample, prediction, saved):
    n = len(sample['targets'])
    raw = {'direct_detection': [], 'structure_prediction': [], 'postprocessed': [], 'text_extent': []}
    gates = Counter()
    pairs = []
    for item in prediction.get('tables', []):
        px, py = item['table_box'][:2]
        for box in item.get('raw', {}).get('det', {}).get('boxes', []):
            b = box['coordinate']; raw['direct_detection'].append([b[0]+px,b[1]+py,b[2]+px,b[3]+py])
        # PaddleX extract_results('table_stru') uses quadrilaterals in crop pixels.
        for poly in item.get('raw', {}).get('table_stru', {}).get('bbox', []):
            if len(poly) == 8 and all(isinstance(v, (int,float)) for v in poly):
                raw['structure_prediction'].append([min(poly[::2])+px,min(poly[1::2])+py,max(poly[::2])+px,max(poly[1::2])+py])
        raw['postprocessed'].extend(item.get('final', {}).get('cell_box_list', []))
        try:
            tables = parse_tables(item['final']['pred_html'])
        except (ValueError, KeyError):
            gates['malformed_structure'] += 1
            continue
        if len(tables) == 1:
            pairs.append((item, tables[0]))
    for b in prediction.get('ocr_blocks', []):
        p = b.get('polygon')
        if p: raw['text_extent'].append([min(x for x,y in p),min(y for x,y in p),max(x for x,y in p),max(y for x,y in p)])
    expected = sample['fixed_edit']['tables'][0]
    norm = lambda value: ''.join(value.split())
    counts = Counter(norm(c['text']) for c in expected['cells'] if norm(c['text']))
    candidates=[]
    for item, table in pairs:
        shared=sum((counts & Counter(norm(c['text']) for c in table['cells'] if norm(c['text']))).values())/max(1,sum(counts.values()))
        topo=topology(expected)==topology(table)
        gates['topology_pass' if topo else 'topology_fail'] += n
        gates['text_shared_pass' if shared >= .6 else 'text_shared_fail'] += n
        gates['slots_pass' if len(item['final']['cell_box_list']) == len(table['cells']) else 'slots_fail'] += n
        candidates.append((topo,shared))
    reasons=[]
    if saved.get('status') != 'success': primary='inference_failed'
    elif not prediction.get('tables'): primary='no_table_candidates'
    elif not pairs: primary='malformed_structure'
    elif len(pairs)>1: primary='table_identity_ambiguous'
    elif not candidates[0][0]: primary='topology_mismatch'
    elif candidates[0][1]<.6: primary='text_coverage_insufficient'
    else: primary=None
    mappings=conservative_mapping(sample['fixed_edit'],prediction,sample['width'],sample['height']) if prediction else []
    for m in mappings:
        if m['target']['kind']=='cell': reasons.append(primary or ('accepted' if m['level']=='cell' else m['reason']))
    if not reasons: reasons=[primary or 'inference_failed']*n
    return {'id': sample['id'], 'targets': n, 'primary_reasons': dict(Counter(reasons)),
            'gates_independent_not_additive': dict(gates), 'oracle_diagnostic_only': {k:oracle(sample['targets'],v) for k,v in raw.items()},
            'legacy_replay_identical': saved.get('status')!='success' or mappings==saved.get('mappings')}


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--predictions',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists(): raise SystemExit('Refuse to overwrite a diagnostic report')
    manifest=json.loads(a.manifest.read_text('utf-8'));rows=[]
    for sample in manifest['samples']:
        folder=a.predictions/sample['id'];artifact=folder/'geometry.json'
        saved=json.loads((folder/'evaluation.json').read_text('utf-8'))
        rows.append(audit(sample,json.loads(artifact.read_text('utf-8')) if artifact.exists() else {},saved))
    totals=Counter();gates=Counter();upper={}
    for row in rows:
        totals.update(row['primary_reasons']);gates.update(row['gates_independent_not_additive'])
        for kind, counts in row['oracle_diagnostic_only'].items():upper.setdefault(kind,Counter()).update(counts)
    report={'protocol':'m0-legacy-diagnostics-v2','manifest_sha256':hashlib.sha256(a.manifest.read_bytes()).hexdigest(),'targets':sum(r['targets'] for r in rows),'tables':len(rows),'primary_reasons':dict(totals),'independent_gates':dict(gates),'oracle_diagnostic_only':upper,'all_legacy_replays_identical':all(r['legacy_replay_identical'] for r in rows),'rows':rows}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='rows'},indent=2))


if __name__=='__main__': main()
