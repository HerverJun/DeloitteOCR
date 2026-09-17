"""Offline scoring of full pages with official, pinned Microsoft GriTS.

Inference inputs and independently reviewed annotations remain separate. This
module cannot select a provider from labels. Missing output stays in denominators.
"""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from ocr_workbench.fusion_alignment import complete_structure, topology
from ocr_workbench.geometry_contract import polygon_iou


def official_grits(reference, predicted):
    import numpy as np
    from ocr_workbench._vendor.grits import grits
    root = ROOT/'src/ocr_workbench/_vendor/grits'
    lock = json.loads((root/'upstream-lock.json').read_text('utf-8'))
    if any(hashlib.sha256((root/name).read_bytes()).hexdigest() != expected for name,expected in lock['files'].items()):
        raise ValueError('Pinned GriTS source/license changed')
    def convert(table):
        return [{'row_nums':list(range(c['row'],c['row']+c['row_span'])),
                 'column_nums':list(range(c['column'],c['column']+c['column_span'])),
                 'cell_text':c['text'],'bbox':c.get('full_grid_box')} for c in table['cells']]
    if not complete_structure(reference) or not complete_structure(predicted):
        return {'topology':0.,'content':0.,'location':None,'reason':'incomplete_grid'}
    # Upstream GriTS has a quartic reward allocation; refuse excessive inputs.
    if reference['rows']*reference['columns']*predicted['rows']*predicted['columns'] > 4_000_000:
        return {'topology':None,'content':None,'location':None,'reason':'scoring_budget_exceeded'}
    a,b = convert(reference),convert(predicted)
    def box_grid(values):
        # PyMuPDF 1.26 accepts Python coordinate sequences, not ndarray scalars.
        # Keep a 2D array of original lists; official scoring code is unchanged.
        grid = np.empty((len(values),len(values[0])),dtype=object)
        for r,row in enumerate(values):
            for c,value in enumerate(row):
                grid[r,c] = value
        return grid
    result = {'topology':float(grits.grits_top(box_grid(grits.cells_to_relspan_grid(a)),box_grid(grits.cells_to_relspan_grid(b)))[0]),
              'content':float(grits.grits_con(np.asarray(grits.cells_to_grid(a,'cell_text'),dtype=object),np.asarray(grits.cells_to_grid(b,'cell_text'),dtype=object))[0]),'location':None}
    if all(c['bbox'] is not None for c in a+b):
        result['location'] = float(grits.grits_loc(box_grid(grits.cells_to_grid(a)),box_grid(grits.cells_to_grid(b)))[0])
    return result


def error_set(reference, predicted):
    errors = set()
    a = {(c['row'],c['column']):c for c in reference['cells']}
    b = {(c['row'],c['column']):c for c in predicted['cells']} if predicted else {}
    for pos,c in a.items():
        p = b.get(pos)
        if p is None:
            errors.add((*pos,'missing_cell')); continue
        for key in ('row_span','column_span','text','is_header'):
            if c.get(key,False) != p.get(key,False):
                errors.add((*pos,key))
    for pos in b.keys()-a.keys():
        errors.add((*pos,'extra_cell'))
    for key in ('rows','columns'):
        if predicted is None or reference[key] != predicted[key]:
            errors.add((-1,-1,key))
    if reference.get('header_relations') is not None and reference['header_relations'] != (predicted or {}).get('header_relations'):
        errors.add((-1,-1,'header_relations'))
    return errors


def score_page(reference, output):
    expected = reference['tables']
    predicted = output.get('tables',[]) if output.get('status','success') == 'success' else []
    candidates = sorted([(polygon_iou(r['polygon'],p['polygon']),ri,pi) for ri,r in enumerate(expected)
                         for pi,p in enumerate(predicted) if r.get('polygon') and p.get('polygon')],reverse=True)
    matched,used = {},set()
    for overlap,ri,pi in candidates:
        if overlap >= .5 and ri not in matched and pi not in used:
            matched[ri] = pi; used.add(pi)
    ledger, tables = [],[]
    for ri,reference_table in enumerate(expected):
        candidate = predicted[matched[ri]] if ri in matched else None
        errors = error_set(reference_table,candidate)
        scores = official_grits(reference_table,candidate) if candidate else {'topology':0.,'content':0.,'location':None}
        tables.append({'reference_table':ri,'predicted_table':matched.get(ri),'usable':not errors,'structure_exact':bool(candidate and topology(reference_table)==topology(candidate)),
            'grits':scores,'correction_cells':len({(r,c) for r,c,k in errors if r >= 0}),
            'structure_operations':sum(k in ('row_span','column_span','is_header','rows','columns','extra_cell','missing_cell','header_relations') for r,c,k in errors)})
        by_pos = {(c['row'],c['column']):c for c in candidate['cells']} if candidate else {}
        for ci,cell in enumerate(reference_table['cells']):
            pred = by_pos.get((cell['row'],cell['column']))
            if output.get('status') == 'failed':
                primary = 'engineering'
            elif candidate is None:
                primary = 'detection'
            elif pred is None or any(pred[k]!=cell[k] for k in ('row_span','column_span')):
                primary = 'structure'
            elif pred['text'] != cell['text']:
                primary = 'text_anchor'
            else:
                primary = 'accepted'
            ledger.append({'page_id':reference['id'],'reference_table':ri,'reference_cell':ci,'primary_failure':primary,
                'system_rejection':output.get('rejection_reasons',[]),'merged':cell['row_span']*cell['column_span']>1,
                'merged_structure_correct':bool(pred and all(pred[k]==cell[k] for k in ('row_span','column_span')))})
    return {'id':reference['id'],'document_id':reference['document_id'],'group_id':reference.get('group_id',reference['document_id']),
        'reference_tables':len(expected),'reference_cells':len(ledger),'detected_tables':len(matched),'extra_tables':len(predicted)-len(used),
        'usable_tables':sum(t['usable'] for t in tables),'correction_cells':sum(t['correction_cells'] for t in tables),
        'structure_operations':sum(t['structure_operations'] for t in tables),'tables':tables,'ledger':ledger,
        'document_page_deliverable':all(t['usable'] for t in tables) and len(predicted)==len(expected) and output.get('status')!='failed'}


def paired_interval(baseline, candidate, *, seed=20260916, repeats=2000):
    # One mean per template/document group; cells are never independent samples.
    if len(baseline) != len(candidate):
        raise ValueError('Paired scoring inputs have different lengths')
    groups = defaultdict(list)
    for before,after in zip(baseline,candidate):
        if before['id'] != after['id'] or before['group_id'] != after['group_id']:
            raise ValueError('Paired scoring inputs differ')
        if before['reference_tables']:
            groups[before['group_id']].append((after['usable_tables']-before['usable_tables'])/before['reference_tables'])
    values = [statistics.mean(v) for v in groups.values()]
    if len(values)<2:
        return {'groups':len(values),'difference':statistics.mean(values) if values else None,'ci95':None}
    rng = random.Random(seed)
    draws = sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(repeats))
    return {'groups':len(values),'difference':statistics.mean(values),'ci95':[draws[int(.025*repeats)],draws[int(.975*repeats)]]}


def validate_annotations(annotations):
    ids = set()
    for page in annotations['pages']:
        if page['id'] in ids:
            raise ValueError('Duplicate reference page')
        ids.add(page['id'])
        review = page.get('annotation_review',{})
        if len(set(review.get('reviewer_ids',[]))) < 2 or not review.get('image_verified') or review.get('unresolved_ambiguities',1):
            raise ValueError('Independent image verification and two reviewers are required')
        for table in page['tables']:
            if not complete_structure(table):
                raise ValueError('Incomplete reference topology')
            if any(c.get('full_grid_box') for c in table['cells']) and table.get('geometry_semantics') != 'full_grid':
                raise ValueError('Text extents cannot be full-grid annotations')
    return True


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--annotations',type=Path,required=True)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    if a.output.exists():
        raise ValueError('Do not overwrite a quality result')
    annotations = json.loads(a.annotations.read_text('utf-8'))
    validate_annotations(annotations)
    tracks = []
    for path in (a.baseline,a.candidate):
        inputs = json.loads(path.read_text('utf-8'))
        if inputs.get('used_reference_crops'):
            raise ValueError('Full-page product scoring forbids reference crops')
        outputs = {row['id']:row for row in inputs['pages']}
        tracks.append([score_page(page,outputs.get(page['id'],{'status':'failed','error':'missing_artifact'})) for page in annotations['pages']])
    result = {'annotation_sha256':hashlib.sha256(a.annotations.read_bytes()).hexdigest(),
        'matching_rule':'greedy one-to-one page-region IoU >= 0.5, fixed before scoring',
        'geometry_CANW':'not scored here; use the separate full_grid geometry evaluation contract',
        'baseline':tracks[0],'candidate':tracks[1],'paired_usable_table_difference':paired_interval(*tracks),'human_review_time':'not_measured',
        'failure_totals':[dict(Counter(c['primary_failure'] for p in track for c in p['ledger'])) for track in tracks]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
