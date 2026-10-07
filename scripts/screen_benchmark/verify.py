"""Validate frozen inputs, labels, masks, transform math and group isolation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parent))
from generate import digest, points, corners

def verify(root):
    root=Path(root);registry=json.loads((root/'source-registry.json').read_text('utf-8'));sources={s['id']:s for s in registry['sources']}
    groups={split:{s['group_id'] for s in sources.values() if s['split']==split} for split in ['development','validation','test']}
    if any(groups[a]&groups[b] for a,b in [('development','validation'),('development','test'),('validation','test')]):raise ValueError('Source group leakage')
    for s in sources.values():
        if digest(s['path'])!=s['source_sha256']:raise ValueError('Source image changed')
        for path_key,hash_key in [('semantic_source','semantic_sha256'),('source_manifest','source_manifest_sha256')]:
            if path_key in s and digest(s[path_key])!=s[hash_key]:raise ValueError('Source semantics or manifest changed')
    report={'scope':'frozen simulated regression; all raw sources remain read-only','families':registry['families'],'sets':{}}
    required={'pilot':72,'validation':60,'chinese-validation':8,'final':240,'challenge':24}
    for name,count in required.items():
        folder=root/name
        if not folder.exists():continue
        freeze=json.loads((folder/'freeze.json').read_text('utf-8'));inputs=json.loads((folder/'inputs.json').read_text('utf-8'))['samples'];truth=json.loads((folder/'truth.json').read_text('utf-8'))['samples']
        if len(inputs)!=count or len(truth)!=count or freeze['count']!=count:raise ValueError(f'Wrong {name} count')
        if digest(folder/'inputs.json')!=freeze['inputs_sha256'] or digest(folder/'truth.json')!=freeze['truth_sha256']:raise ValueError('Frozen manifest changed')
        ids=set();characters=critical=visible=0
        for i,t in zip(inputs,truth):
            if i['id']!=t['id'] or i['id'] in ids:raise ValueError('Input/truth identity mismatch')
            ids.add(i['id']);source=sources[t['source_id']]
            if source['group_id']!=t['group_id'] or source['source_sha256']!=t['source_sha256']:raise ValueError('Wrong ancestry')
            if digest(i['image'])!=i['image_sha256']:raise ValueError('Frozen simulated input changed')
            for path,hashkey in [('occlusion_mask','occlusion_mask_sha256'),('visibility_mask','visibility_mask_sha256')]:
                if digest(t[path])!=t[hashkey]:raise ValueError('Mask changed')
                with Image.open(t[path]) as mask:
                    if mask.size!=(i['width'],i['height']):raise ValueError('Mask/image dimensions differ')
            matrix=np.asarray(t['source_to_image']);inverse=np.linalg.inv(matrix)
            for table in t['tables']:
                occupied=set()
                for cell in table['cells']:
                    expected=points(matrix,corners(cell['box']))
                    if not np.allclose(expected,cell['polygon'],atol=.001):raise ValueError('Cell mapping differs')
                    recovered=points(inverse,cell['polygon'])
                    if not np.allclose(recovered,corners(cell['box']),atol=.01):raise ValueError('Cell transform cannot round-trip')
                    if cell['visibility']=='visible' and any(x<0 or y<0 or x>i['width'] or y>i['height'] for x,y in cell['polygon']):raise ValueError('Visible cell outside image')
                    for r in range(cell['row'],cell['row']+cell['row_span']):
                        for c in range(cell['column'],cell['column']+cell['column_span']):
                            if (r,c) in occupied:raise ValueError('Ground-truth overlapping spans')
                            occupied.add((r,c))
                    characters+=len(cell['text']);critical+=any(ch.isdigit() for ch in cell['text']);visible+=cell['visibility']=='visible'
                if len(occupied)!=table['rows']*table['columns']:raise ValueError('Ground-truth structure incomplete')
        report['sets'][name]={**freeze,'hashes_transforms_masks_verified':True,'reference_characters':characters,'critical_cells':critical,'visible_cells':visible}
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.resolve().drive.upper()!='F:':raise ValueError('F-only output')
    report=verify(a.dataset);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
