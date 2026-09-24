"""Strict shared structure invariants; diagnostics never invent text or geometry."""
from math import isfinite

from ocr_workbench.editing import validate_edit
from ocr_workbench.geometry_contract import signed_area

VERSION = 'complex-table-v1'


def validate_grid(table, *, tokens=None, require_complete=True):
    validate_edit({'text':'','tables':[table]})
    covered=sum(c['row_span']*c['column_span'] for c in table['cells'])
    if require_complete and covered != table['rows']*table['columns']:
        raise ValueError('结构网格存在未覆盖槽位')
    ids=[c['id'] for c in table['cells'] if 'id' in c]
    if len(ids)!=len(set(ids)):
        raise ValueError('单元格 ID 重复')
    refs=[t for c in table['cells'] for t in c.get('structure_source',{}).get('token_ids',[])]
    if any(not isinstance(t,str) or not t for t in refs) or len(refs)!=len(set(refs)):
        raise ValueError('文字片段重复归属或 ID 无效')
    if tokens is not None:
        pool={t['id']:t for t in tokens}
        if len(pool)!=len(tokens) or set(refs)-pool.keys():
            raise ValueError('文字池存在重复或未知 ID')
    return {'covered_slots':covered,'cells':len(table['cells']),'token_ids':refs}


def polygon(value, width, height, *, required=False):
    if value is None and not required:return
    if (not isinstance(value,list) or not 3<=len(value)<=32 or
        any(not isinstance(p,(list,tuple)) or len(p)!=2 or
            any(type(v) not in (int,float) or not isfinite(v) for v in p) for p in value)):
        raise ValueError('坐标多边形无效')
    if any(not 0<=x<=width or not 0<=y<=height for x,y in value) or abs(signed_area(value))<=0:
        raise ValueError('坐标超出图像或面积为零')


def validate_annotation(table, width, height):
    validate_grid(table)
    if not isinstance(table.get('image_version'),str) or not table.get('image_sha256'):
        raise ValueError('缺少图像身份')
    transform=table.get('page_to_crop')
    if (not isinstance(transform,list) or len(transform)!=3 or any(not isinstance(r,list) or len(r)!=3 for r in transform)
            or any(type(v) not in (int,float) or not isfinite(v) for row in transform for v in row)):
        raise ValueError('坐标变换无效')
    a,b,c=transform
    det=a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(det)<1e-12:raise ValueError('坐标变换不可逆')
    polygon(table.get('polygon'),width,height,required=True)
    ids={c.get('id') for c in table['cells']}
    if None in ids or any(not isinstance(i,str) or not i for i in ids):raise ValueError('缺少参考格 ID')
    for cell in table['cells']:
        if cell.get('text_state') not in {'sourced','verified_blank','missing','illegible','unknown'}:
            raise ValueError('未知文字状态')
        if cell['text_state']=='verified_blank' and cell['text']:
            raise ValueError('真实空白不能含文字')
        polygon(cell.get('cell_polygon'),width,height,required=True)
        polygon(cell.get('text_polygon'),width,height)
        if not isinstance(cell.get('header_ids'),list) or set(cell['header_ids'])-ids:
            raise ValueError('表头引用无效')
    return {'valid':True,'cells':len(table['cells']),'human_signoff':False}


def impact(current, proposed):
    before={(ti,c['row'],c['column']):c for ti,t in enumerate(current) for c in t['cells']}
    changed=[]
    for ti,table in enumerate(proposed):
        for c in table['cells']:
            old=before.get((ti,c['row'],c['column']))
            if old is None or any(old.get(k)!=c.get(k) for k in ('text','row_span','column_span','is_header')):
                changed.append({'table':ti,'row':c['row'],'column':c['column'],'before':old['text'] if old else None,
                    'after':c['text'],'span':[c['row_span'],c['column_span']]})
    return {'cells':changed,'rows':sorted({c['row'] for c in changed}),
        'columns':sorted({c['column'] for c in changed}),
        'amount_cells':sum(any(ch.isdigit() for ch in c['after']) for c in changed),
        'manual_conflicts_require_resolution':True}
