import json
from pathlib import Path
import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
root=Path(__file__).resolve().parents[1];base=root/'build/document-workflow'
source=base/'geometry-holdout/wtw';out=base/'wtw-evaluation'
manifest=json.loads((source/'download-manifest.json').read_text('utf-8'));lock=json.loads((out/'run-lock.json').read_text('utf-8'))
rows=[]
for n,s in enumerate(manifest['samples'],1):
    image=cv2.imread(str(source/s['image']));height,width=image.shape[:2]
    targets=[np.asarray([float(v) for v in line.split()[1:]],dtype=np.float32).reshape(4,2)*[width,height] for line in (source/s['label']).read_text().splitlines() if line.strip()]
    raw=json.loads((out/'geometry'/f'{n:02d}'/'geometry.json').read_text('utf-8'))
    boxes=[]
    for table in raw['tables']:
        x,y=table['table_box'][:2]
        for b in table.get('raw',{}).get('det',{}).get('boxes',[]):
            if b.get('score',0)>=.5:
                x0,y0,x1,y1=b['coordinate'];boxes.append(np.array([[x0+x,y0+y],[x1+x,y0+y],[x1+x,y1+y],[x0+x,y1+y]],dtype=np.float32))
    overlaps=np.zeros((len(targets),len(boxes)))
    for i,t in enumerate(targets):
        t=cv2.convexHull(np.asarray(t,dtype=np.float32));area=cv2.contourArea(t)
        for j,b in enumerate(boxes):
            intersection,_=cv2.intersectConvexConvex(t,b);union=area+cv2.contourArea(b)-intersection
            overlaps[i,j]=intersection/union if union>0 else 0
    pairs=list(zip(*linear_sum_assignment(-overlaps))) if boxes else []
    correct=sum(int(overlaps[i,j]>=.5) for i,j in pairs)
    rows.append({'number':n,'image':s['image'],'targets':len(targets),'predicted_cells':len(boxes),'matched_iou_05':correct,
        'photo':n in lock['photo_sample_numbers'],'chinese':n in lock['chinese_sample_numbers'],
        'ocr_status':json.loads((out/'ppocr'/f'{n:02d}'/'evaluation.json').read_text())['status'],
        'best_assignment_ious':[float(overlaps[i,j]) for i,j in pairs]})
groups={}
for name in ('all','photo','chinese'):
    chosen=[r for r in rows if name=='all' or r[name]];targets=sum(r['targets'] for r in chosen);pred=sum(r['predicted_cells'] for r in chosen);matched=sum(r['matched_iou_05'] for r in chosen)
    groups[name]={'images':len(chosen),'targets':targets,'predicted_cells':pred,'matched_iou_05':matched,'detection_recall':matched/targets,'detection_precision':matched/pred if pred else None,
        'wrong_logical_cell_rate':None,'logical_mapping_coverage':None}
report={'scope':'SUPPLEMENT ONLY: detector polygon recall/precision, no fixed-text logical correspondence score. Does not count toward primary 100 tables. Raw direct detector score >= .5, Hungarian maximum-IoU one-to-one matching, polygon IoU >= .5.',
    'limitations':'Mirror lacks cell text and row/column/span; cannot measure wrong-row/column rate. Images include multiple tables, rotations and repeated templates. One blank spreadsheet returned no OCR text; geometry still evaluated with empty OCR input.',
    'groups':groups,'rows':rows}
(out/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(groups,indent=2))
