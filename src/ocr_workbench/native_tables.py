"""Create a separate structural preview using only already extracted PDF text."""
from copy import deepcopy
from ocr_workbench.coordinates import bounds, box_polygon, validate_polygon
from ocr_workbench.editing import tables_html
from ocr_workbench.tables import parse_tables


def native_table_preview(raw, prediction, width, height):
    from ocr_workbench.pdf_worker import _intersect, _area
    native = [b for b in raw.get('blocks', []) if b.get('source') == 'pdf-native' and b.get('polygon') and b.get('text_range')]
    proposals = []
    for table in prediction.get('tables', []):
        try:
            parsed = parse_tables(table['final']['pred_html'])
        except ValueError:
            continue
        boxes = table['final']['cell_box_list']
        if len(parsed) != 1 or len(parsed[0]['cells']) != len(boxes): continue
        structure = parsed[0]
        groups = [[] for _ in boxes]
        region = table['table_box']
        contained = [b for b in native if _intersect(bounds(b['polygon']),region) >= .8*_area(bounds(b['polygon']))]
        if not contained: continue
        valid = True
        for block in contained:
            bbox = bounds(block['polygon'])
            candidates = [i for i,box in enumerate(boxes) if _intersect(bbox,box) >= .8*_area(bbox)]
            if len(candidates) != 1:
                valid = False; break
            groups[candidates[0]].append(block)
        if not valid: continue
        for cell, group in zip(structure['cells'], groups):
            group.sort(key=lambda b:b['text_range'][0])
            cell['text'] = ' '.join(b['text'] for b in group)
            cell['_native_units'] = deepcopy(group)
            cell['confidence'] = None
            cell.pop('polygon',None)
        a, z = min(b['text_range'][0] for b in contained),max(b['text_range'][1] for b in contained)
        if any(b not in contained and b.get('text_range') and b['text_range'][0] < z and b['text_range'][1] > a for b in raw.get('blocks', [])):
            continue
        if any(a < end and z > start for start,end,_,_ in proposals): continue
        proposals.append((a,z,structure,contained))
    if not proposals: return None
    result = deepcopy(raw)
    text = raw['text']
    for a,z,structure,_ in sorted(proposals,reverse=True,key=lambda p:p[0]):
        text = text[:a]+tables_html([structure])+text[z:]
    result['text'],result['tables'] = text,parse_tables(text)
    result['document']['structure_preview'] = True
    result['document']['structure_text_source'] = 'pdf-native'
    result['document']['structure_contributes_votes'] = False
    # Ranges for unaffected native text change around generated HTML. Table
    # native words retain their polygons separately for export without duplicates.
    all_contained = [b for _,_,_,blocks in proposals for b in blocks]
    result['document']['table_native_units'] = deepcopy(all_contained)
    result['document']['table_native_cells'] = {
        f"{ti}:{cell['row']}:{cell['column']}": {'text': cell['text'], 'units': cell['_native_units']}
        for ti,(_,_,table,_) in enumerate(sorted(proposals,key=lambda p:p[0])) for cell in table['cells']}
    kept = []
    for block in raw.get('blocks', []):
        if block in all_contained: continue
        copy = deepcopy(block)
        if copy.get('text_range'):
            a,z = copy['text_range']
            shift = sum(len(tables_html([table]))-(end-start) for start,end,table,_ in proposals if end <= a)
            copy['text_range']=[a+shift,z+shift]
        kept.append(copy)
    result['blocks']=kept
    result['engine_info']={'name':'原生表格结构预览（实验性）'}
    return result
