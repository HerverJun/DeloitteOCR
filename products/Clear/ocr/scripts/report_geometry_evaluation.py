"""Compute frozen end-to-end mapping metrics without changing any model output."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import sys
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.geometry import iou
from ocr_workbench.coordinates import bounds
base=root/'build/document-workflow'
manifest_path=base/'geometry-holdout/frozen/manifest.json'
manifest=json.loads(manifest_path.read_text('utf-8'))
output=base/'geometry-evaluation'

def percentiles(values):
    values=sorted(values)
    def p(f):return values[min(len(values)-1,int((len(values)-1)*f+.5))] if values else None
    return {'n':len(values),'median':statistics.median(values) if values else None,'p90':p(.9),'p95':p(.95)}

rows=[]
for sample in manifest['samples']:
    for engine in ('existing-region','geometry','rapidtable'):
        if engine=='existing-region': prediction={'status':'success','mappings':[],'seconds':0}
        else: prediction=json.loads((output/engine/sample['id']/'evaluation.json').read_text('utf-8'))
        mapping={(m['target']['row'],m['target']['column']):m for m in prediction.get('mappings',[]) if m['target']['kind']=='cell'}
        counts=Counter(targets=len(sample['targets']),offered=0,correct=0,wrong=0,no_location=0,region_fallback=0,low_iou_correct_identity=0)
        details=[]
        for k,target in enumerate(sample['targets']):
            m=mapping.get((target['row'],target['column']))
            if engine=='existing-region' or m and m['level']=='region': counts['region_fallback']+=1
            elif not m or m['level']!='cell' or not m.get('polygon'):counts['no_location']+=1
            else:
                counts['offered']+=1
                box=bounds(m['polygon']);overlaps=[iou(box,t['box']) for t in sample['targets']]
                best=max(range(len(overlaps)),key=lambda i:overlaps[i])
                # Identity is the unique maximum-IoU target. A box aimed at any
                # other row/column is a wrong-cell output, even below .5 IoU.
                wrong=best!=k and overlaps[best]>overlaps[k]+1e-9
                correct=not wrong and overlaps[k]>=.5
                counts['wrong']+=int(wrong);counts['correct']+=int(correct)
                counts['low_iou_correct_identity']+=int(not wrong and not correct)
                details.append({'row':target['row'],'column':target['column'],'iou':overlaps[k],
                    'best_row':sample['targets'][best]['row'],'best_column':sample['targets'][best]['column'],'wrong_cell':wrong,'correct':correct})
        rows.append({'id':sample['id'],'engine':engine,'cohort':sample['cohort'],'tags':sample['tags'],
            'counts':dict(counts),'status':prediction['status'],'seconds':prediction.get('seconds'),
            'inference_seconds':prediction.get('inference_seconds'),'details':details})

groups={}
for name in ('all','pubtables-1m','fintabnet','merged','unmerged','borderless-visual-fintabnet'):
    groups[name]={}
    for engine in ('existing-region','geometry','rapidtable'):
        chosen=[r for r in rows if r['engine']==engine and (name=='all' or r['cohort']==name or name in r['tags'] or name=='unmerged' and 'merged' not in r['tags'] or name=='borderless-visual-fintabnet' and r['cohort']=='fintabnet')]
        counts=Counter()
        for r in chosen:counts.update(r['counts'])
        total=counts['targets'];offered=counts['offered']
        groups[name][engine]={'tables':len(chosen),'counts':dict(counts),'precise_coverage':counts['correct']/total if total else None,
            'wrong_cell_rate':counts['wrong']/offered if offered else None,
            'no_cell_location_rate':(total-offered)/total if total else None,
            'no_location_rate':counts['no_location']/total if total else None,
            'region_fallback_rate':counts['region_fallback']/total if total else None,
            'all_cells_downgraded_tables':sum(r['counts']['offered']==0 for r in chosen),
            'failures':sum(r['status']!='success' for r in chosen),
            'inference_seconds':percentiles([r['inference_seconds'] for r in chosen if r['inference_seconds'] is not None]),
            'wall_seconds':percentiles([r['seconds'] for r in chosen if r['seconds'] is not None])}
report={'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    'definitions':{'success':'correct logical target and full-cell-box IoU >= 0.5',
    'wrong':'another GT row/column has strictly higher IoU than intended target; denominator=offered cell locations',
    'region_baseline':'each supplied table crop has a region; no cell coordinates are fabricated',
    'rapid':'same production topology/text gate; SLANet+ structure prediction boxes, no detector-support requirement',
    'borderless-visual-fintabnet':'all 40 FinTabNet crops visually inspected after freezing; no full cell grid (horizontal rules and shaded rows allowed). Additional descriptive tags only, no reselection or model tuning.',
    'empty_denominator':'null, not 0%','timings':'Paddle GPU and Rapid CPU are different execution backends; no direct hardware speed claim'},
    'groups':groups,'rows':rows}
report['production_acceptance']=groups['all']['geometry']['precise_coverage']>=.9 and groups['all']['geometry']['wrong_cell_rate'] is not None and groups['all']['geometry']['wrong_cell_rate']<=.01
(output/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({'groups':groups,'production_acceptance':report['production_acceptance']},ensure_ascii=False,indent=2))
