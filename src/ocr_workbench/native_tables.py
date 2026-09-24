"""Create a separate structural preview using only already extracted PDF text."""
from copy import deepcopy
from ocr_workbench.coordinates import bounds, box_polygon, validate_polygon
from ocr_workbench.editing import tables_html
from ocr_workbench.tables import parse_tables


def native_source_blocks(blocks, image_version):
    """Give each native word a stable identity before candidate region filtering."""
    from ocr_workbench.geometry_contract import fingerprint
    blocks=deepcopy(blocks)
    native=[b for b in blocks if b.get('source')=='pdf-native' and b.get('polygon') and b.get('text_range')]
    source_result='pdf-native:'+fingerprint({'image_version':image_version,
        'units':[{k:b.get(k) for k in ('text','polygon','text_range','native_index')} for b in native]})
    for index,block in enumerate(native):block['id']=f'{source_result}:token:{index}'
    return blocks,source_result


def native_table_preview(raw, prediction, width, height):
    from ocr_workbench.pdf_worker import _intersect, _area
    from ocr_workbench.geometry_contract import tokens_from_blocks, as_json
    from ocr_workbench.geometry_providers import adapt_prediction
    from ocr_workbench.table_matching import assign_tokens, policy_for_algorithm
    raw = deepcopy(raw)
    image_version = raw.get('project_image_version', 'pdf-native')
    # Namespace the complete source page before selecting any candidate region.
    # Subset-relative indices would assign the same ID to different words in
    # different candidate tables on one page.
    raw['blocks'],source_result=native_source_blocks(raw.get('blocks', []),image_version)
    native = [b for b in raw['blocks'] if b.get('source') == 'pdf-native' and b.get('polygon') and b.get('text_range')]
    proposals = []
    adapted = adapt_prediction(prediction,width,height)
    preview_tables = prediction.get('tables', [])
    if 'candidate_tables' in prediction or 'tf_table_cells' in prediction or 'pdfplumber_tables' in prediction:
        preview_tables = []
        for table in adapted:
            cells = table['cells']
            if not cells or any(c.reason_codes or not c.cell_polygon for c in cells):
                preview_tables.append({'final': {'pred_html': '', 'cell_box_list': []}})
                continue
            structure = {'rows': max(c.row_end for c in cells), 'columns': max(c.column_end for c in cells),
                'cells': [{'row': c.row_start, 'column': c.column_start, 'row_span': c.row_end - c.row_start,
                           'column_span': c.column_end - c.column_start, 'text': '', 'confidence': None} for c in cells]}
            headers = {(c['row'],c['column']):c for c in (table.get('structure') or {}).get('cells',[])}
            for cell in structure['cells']:
                if headers.get((cell['row'],cell['column']),{}).get('is_header'):
                    cell['is_header'] = True
            occupied = [(r, col) for c in cells for r in range(c.row_start, c.row_end) for col in range(c.column_start, c.column_end)]
            if len(occupied) != len(set(occupied)) or len(occupied) != structure['rows'] * structure['columns']:
                preview_tables.append({'final': {'pred_html': '', 'cell_box_list': []}})
                continue
            # Keep the original prediction order aligned with boxes and tokens.
            preview_tables.append({'structure': structure, 'table_box': bounds(table['polygon']),
                'final': {'cell_box_list': [bounds(c.cell_polygon) for c in cells]}})
    for table_index, table in enumerate(preview_tables):
        try:
            parsed = [deepcopy(table['structure'])] if 'structure' in table else parse_tables(table['final']['pred_html'])
        except ValueError:
            continue
        boxes = table['final']['cell_box_list']
        if len(parsed) != 1 or len(parsed[0]['cells']) != len(boxes): continue
        structure = parsed[0]
        groups = [[] for _ in boxes]
        region = table['table_box']
        structure['region_polygon']=box_polygon(region)
        contained = [b for b in native if _intersect(bounds(b['polygon']),region) >= .8*_area(bounds(b['polygon']))]
        if not contained: continue
        tokens,rejected = tokens_from_blocks(contained,source_result=source_result,image_version=image_version,width=width,height=height,engine='pdf-native')
        candidate_cells = adapted[table_index]['cells']
        if len(candidate_cells) != len(boxes): continue
        assignments,owned,error = assign_tokens(tokens,candidate_cells,policy_for_algorithm('local-v3'))
        if rejected or error or len(assignments)!=len(contained) or any(a.adopted_cell_id is None for a in assignments): continue
        by_token = {t.id:b for t,b in zip(tokens,contained)}
        for i,candidate in enumerate(candidate_cells):
            groups[i] = [by_token[t.id] for t in owned.get(candidate.id,[])]
        structure['_native_matching_v2'] = {'tokens':[as_json(t) for t in tokens], 'assignments':[as_json(a) for a in assignments],
            'original_cells':[as_json(c) for c in candidate_cells]}
        for cell, group, candidate in zip(structure['cells'], groups, candidate_cells):
            group.sort(key=lambda b:b['text_range'][0])
            cell['text'] = ' '.join(b['text'] for b in group)
            cell['_native_units'] = deepcopy(group)
            cell['structure_source'] = {'text_source_result': source_result, 'image_version': image_version,
                'token_ids': [b['id'] for b in group], 'range_semantics': 'full_cell',
                'cell_polygon': deepcopy(candidate.cell_polygon), 'geometry_origin': candidate.geometry_origin,
                'text_state': 'sourced' if group else 'unverified_empty'}
            # These offsets and separators are recorded while the adopted text
            # is assembled. They are not inferred later from matching strings.
            offset = 0
            fragments = []
            for index, block in enumerate(group):
                separator = ' ' if index else ''
                offset += len(separator)
                fragments.append({'token_id': block['id'], 'text': block['text'],
                    'start': offset, 'end': offset + len(block['text']), 'separator_before': separator,
                    'polygon': deepcopy(block['polygon'])})
                offset += len(block['text'])
            cell['native_content'] = {'version': 'native-adopted-fragments-v1',
                'text_source_result': source_result, 'image_version': image_version,
                'literal': cell['text'], 'fragments': fragments}
            cell['confidence'] = None
            cell.pop('polygon',None)
        a, z = min(b['text_range'][0] for b in contained),max(b['text_range'][1] for b in contained)
        # A mixed page can interleave OCR blocks with native words in reading
        # order. Replace only the exact native ranges, preserving every other
        # source and its pending conflict; never delete their enclosing span.
        ranges = sorted(b['text_range'] for b in contained)
        if any(not 0 <= start < end <= len(raw['text']) for start,end in ranges): continue
        if any(left[1] > right[0] for left,right in zip(ranges,ranges[1:])): continue
        if any(raw['text'][b['text_range'][0]:b['text_range'][1]] != b['text'] for b in contained): continue
        if any(b not in contained and b.get('text_range') and any(b['text_range'][0]<end and b['text_range'][1]>start for start,end in ranges) for b in raw.get('blocks', [])):
            continue
        if any(a < end and z > start for start,end,_,_ in proposals): continue
        proposals.append((a,z,structure,contained))
    if not proposals: return None
    result = deepcopy(raw)
    text = raw['text']
    replacements = []
    for a,z,structure,units in proposals:
        for start,end in sorted(b['text_range'] for b in units):
            replacements.append((start,end,tables_html([structure]) if start==a else ''))
    for a,z,replacement in sorted(replacements,reverse=True):
        text = text[:a]+replacement+text[z:]
    result['text'],result['tables'] = text,parse_tables(text)
    # HTML parsing preserves display content but cannot carry source metadata.
    # Reattach by the exact inserted HTML offset, not text equality or table order.
    for a, z, structure, units in proposals:
        rendered_start = a + sum(len(value) - (end-start) for start,end,value in replacements if end <= a)
        matches = [t for t in result['tables'] if t.get('source', {}).get('start') == rendered_start]
        if len(matches) != 1:
            raise ValueError('Native table provenance cannot be attached to its exact text insertion')
        matches[0]['region_polygon']=deepcopy(structure['region_polygon'])
        original_cells = {(c['row'], c['column']): c for c in structure['cells']}
        for cell in matches[0]['cells']:
            source_cell = original_cells[(cell['row'], cell['column'])]
            if any(cell[k] != source_cell[k] for k in ('text', 'row_span', 'column_span')):
                raise ValueError('Native table literal or structure changed during serialization')
            for key in ('structure_source', 'native_content'):
                cell[key] = deepcopy(source_cell[key])
    result['document']['structure_preview'] = True
    result['document']['structure_text_source'] = 'pdf-native'
    result['document']['structure_contributes_votes'] = False
    # Ranges for unaffected native text change around generated HTML. Table
    # native words retain their polygons separately for export without duplicates.
    all_contained = [b for _,_,_,blocks in proposals for b in blocks]
    result['document']['table_native_units'] = deepcopy(all_contained)
    result['document']['native_geometry_contract_version'] = 2
    result['document']['native_matching_evidence'] = [table['_native_matching_v2'] for _,_,table,_ in sorted(proposals,key=lambda p:p[0])]
    result['document']['table_native_cells'] = {
        f"{ti}:{cell['row']}:{cell['column']}": {'text': cell['text'], 'units': cell['_native_units']}
        for ti,(_,_,table,_) in enumerate(sorted(proposals,key=lambda p:p[0])) for cell in table['cells']}
    kept = []
    for block in raw.get('blocks', []):
        if block in all_contained: continue
        copy = deepcopy(block)
        if copy.get('text_range'):
            a,z = copy['text_range']
            shift = sum(len(value)-(end-start) for start,end,value in replacements if end <= a)
            copy['text_range']=[a+shift,z+shift]
        kept.append(copy)
    result['blocks']=kept
    result['engine_info']={'name':'原生表格结构预览（实验性）'}
    return result
