"""Frozen historical RapidTable comparator, extracted without changing gates."""
from collections import Counter
from ocr_workbench.geometry import topology
from ocr_workbench.coordinates import box_polygon, validate_polygon
from ocr_workbench.tables import parse_tables


def legacy_rapid_mapping(edit,prediction,width,height):
    tables=parse_tables(prediction['html']);boxes=prediction['cell_bboxes']
    expected=edit['tables'][0];pred=tables[0] if len(tables)==1 else None
    norm=lambda value:''.join(value.split())
    counts=Counter(norm(c['text']) for c in expected['cells'] if norm(c['text']))
    shared=sum((counts & Counter(norm(c['text']) for c in pred['cells'] if norm(c['text']))).values())/max(1,sum(counts.values())) if pred else 0
    chosen=pred is not None and topology(expected)==topology(pred) and shared>=.6 and len(boxes)==len(pred['cells'])
    mappings=[]
    for k,cell in enumerate(expected['cells']):
        item={'target':{'kind':'cell','table':0,'row':cell['row'],'column':cell['column']},'level':'region',
              'polygon':box_polygon([0,0,width,height]),'reason':'structure_or_table_identity_ambiguous'}
        if chosen:
            value=norm(cell['text']);agreed=norm(pred['cells'][k]['text'])==value
            row_anchor=any(c['row']==cell['row'] and c['column']!=cell['column'] and norm(c['text']) and counts[norm(c['text'])]==1 and norm(c['text'])==norm(pred['cells'][j]['text']) for j,c in enumerate(expected['cells']))
            col_anchor=any(c['column']==cell['column'] and c['row']!=cell['row'] and norm(c['text']) and counts[norm(c['text'])]==1 and norm(c['text'])==norm(pred['cells'][j]['text']) for j,c in enumerate(expected['cells']))
            b=boxes[k];poly=box_polygon(b) if len(b)==4 else [b[j:j+2] for j in range(0,len(b),2)]
            try:poly=validate_polygon(poly,width,height)
            except ValueError:poly=None
            if poly and agreed and (bool(value) and counts[value]==1 or row_anchor and col_anchor):
                item.update(level='cell',polygon=poly,reason='unique_structure_and_text',geometry_origin='structure_prediction')
            else:item['reason']='text_or_geometry_ambiguous'
        mappings.append(item)
    return mappings
