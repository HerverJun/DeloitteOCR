"""Explicit PaddleX 3.7.0 and RapidTable 3.0.2 geometry adapters.

The final Paddle list is page xyxy; raw detector/SLANeXt lists are crop xyxy/
quad8 respectively. RapidTable SLANet+ returns image quad8 with inclusive
[row_start,row_end,col_start,col_end]. Never infer a format from list length.
"""
from ocr_workbench.coordinates import IDENTITY, box_polygon, polygon
from ocr_workbench.geometry_contract import PredictedCell, checked_polygon, fingerprint
from ocr_workbench.tables import parse_tables


def translated_box(box, dx, dy):
    x0,y0,x1,y1=box
    return [x0+dx,y0+dy,x1+dx,y1+dy]


def quad8(value):
    if len(value)!=8 or any(type(v) not in (int,float) for v in value):raise ValueError('invalid_polygon')
    return [[value[i],value[i+1]] for i in (0,2,4,6)]


def paddle_lineage(item):
    """Record exact stage identities and complete operation dependencies.

    No IoU-based attribution. Unchanged values establish identity; modified
    outputs preserve the whole operation's input dependencies and remain
    unverified for complete-cell use. Raw OCR supplements are text extents.
    """
    dx,dy=item['table_box'][:2];direct={};records=[]
    for i,entry in enumerate(item.get('raw',{}).get('det',{}).get('boxes',[])):
        key=f'det:{i}';box=translated_box(entry['coordinate'],dx,dy)
        direct.setdefault(tuple(box),[]).append(key)
        records.append({'id':key,'stage':'direct_detection','box':box,'original_box':entry['coordinate'],'parents':[], 'score':entry.get('score')})
    previous=[r['id'] for r in records]
    for si,step in enumerate(item.get('steps',[])):
        ids=[]
        for i,box in enumerate(step.get('output',[])):
            if not isinstance(box,list) or len(box)!=4:continue
            key=f'step:{si}:{i}';ids.append(key)
            page=translated_box(box,dx,dy);parents=direct.get(tuple(page),[])
            records.append({'id':key,'stage':step['operation'],'box':page,
                'parents':parents if len(parents)==1 else previous[:],
                'relationship':'unchanged_identity' if len(parents)==1 else 'operation_dependency_not_cell_identity'})
        previous=ids
    for i,box in enumerate(item.get('final',{}).get('cell_box_list',[])):
        parents=direct.get(tuple(box),[])
        records.append({'id':f'final:{i}','stage':'final','box':box,
            'parents':parents if len(parents)==1 else previous[:],
            'relationship':'unchanged_identity' if len(parents)==1 else 'unverified_postprocessing',
            'geometry_origin':'direct_detection' if len(parents)==1 else 'derived_from_cells',
            'full_cell_verified_lineage':len(parents)==1})
    return records


def _parse(html):
    try:
        parsed=parse_tables(html)
        if len(parsed)!=1:return None
        return parsed[0]
    except (ValueError,TypeError):return None


def _cell(cell, *, id, provider, version, table_id, poly, original, transform, origin, derivation, width, height, reasons=()):
    issues=list(reasons)
    try:poly=checked_polygon(poly,width,height)
    except (ValueError,TypeError):poly=None;issues.append('invalid_polygon')
    r,c,rs,cs=(cell[k] for k in ('row','column','row_span','column_span'))
    if any(type(v) is not int for v in (r,c,rs,cs)) or min(r,c)<0 or min(rs,cs)<1:
        raise ValueError('span_conflict')
    return PredictedCell(id,provider,version,table_id,id.rsplit(':',1)[-1],r,r+rs,c,c+cs,cell.get('text',''),poly,origin,
        original,transform,{'original':[r,r+rs,c,c+cs],'current':[r,r+rs,c,c+cs],'compression_applied':False},derivation,issues)


def paddle_tables(prediction,width,height):
    result=[];version=fingerprint(prediction.get('model_revisions',{}))
    for index,item in enumerate(prediction.get('tables',[])):
        table_id=str(item.get('table_id') or 'paddle:'+fingerprint({'region':item.get('region_id'),'box':item.get('table_box'),'raw':item.get('raw',{}),'final':item.get('final',{})})[:24])
        issues=[];cells=[]
        try:region=checked_polygon(box_polygon(item['table_box']),width,height)
        except (ValueError,TypeError,KeyError):region=None;issues.append('invalid_polygon')
        parsed=_parse(item.get('final',{}).get('pred_html',''));boxes=item.get('final',{}).get('cell_box_list',[])
        raw_structure=item.get('raw',{}).get('table_stru',{})
        lineage=item.get('lineage') or paddle_lineage(item)
        final_lineage={r['id']:r for r in lineage if r['stage']=='final'}
        if parsed and len(parsed['cells'])==len(boxes):
            for ci,(cell,box) in enumerate(zip(parsed['cells'],boxes)):
                origin_record=final_lineage.get(f'final:{ci}',{})
                verified=origin_record.get('full_cell_verified_lineage',False)
                try:poly=box_polygon(box)
                except (ValueError,TypeError):poly=None
                cells.append(_cell(cell,id=f'{table_id}:final:{ci}',provider='paddle-table-v2',version=version,table_id=table_id,
                    poly=poly,original=poly,transform=IDENTITY[:],origin='direct_detection' if verified else 'derived_from_cells',
                    derivation=[origin_record],width=width,height=height,reasons=() if verified else ('unverified_lineage',)))
        else:
            issues.append('boxes_slots_out_of_sync' if parsed else 'malformed_structure')
            # Independent original structure can survive damaged postprocessing.
            parsed=_parse(''.join(raw_structure.get('structure',[])))
            raw_boxes=raw_structure.get('bbox',[])
            if parsed and len(parsed['cells'])==len(raw_boxes):
                dx,dy=item['table_box'][:2];transform=[1,0,dx,0,1,dy,0,0,1]
                for ci,(cell,box) in enumerate(zip(parsed['cells'],raw_boxes)):
                    try:original=quad8(box);poly=polygon(transform,original)
                    except (ValueError,TypeError):original=poly=None
                    cells.append(_cell(cell,id=f'{table_id}:structure:{ci}',provider='paddle-table-v2',version=version,table_id=table_id,
                        poly=poly,original=original,transform=transform,origin='structure_prediction',
                        derivation=[{'operation':'raw_structure_alternative','source':'raw.table_stru','cell_index':ci}],width=width,height=height))
        result.append({'id':table_id,'provider':'paddle-table-v2','region_id':item.get('region_id'),'polygon':region,
            'cells':cells,'structure':parsed,'reason_codes':issues,'original_index':index,'lineage':lineage})
    return result


def rapid_tables(prediction,width,height):
    # This entry point is only for the pinned SLANet+ quad8/inclusive contract.
    if prediction.get('box_format','quad8')!='quad8' or prediction.get('span_format','inclusive-r0-r1-c0-c1')!='inclusive-r0-r1-c0-c1':raise ValueError('Unsupported RapidTable coordinate contract')
    parsed=_parse(prediction.get('html',''));boxes=prediction.get('cell_bboxes',[]);logic=prediction.get('logic_points',[])
    table_id='rapid:'+fingerprint({'html':prediction.get('html'),'boxes':boxes})[:24];cells=[];issues=[]
    if parsed is None:issues.append('malformed_structure')
    elif len(parsed['cells'])!=len(boxes) or len(boxes)!=len(logic):issues.append('boxes_slots_out_of_sync')
    else:
        for ci,(cell,box,span) in enumerate(zip(parsed['cells'],boxes,logic)):
            if len(span)!=4 or any(type(v) is not int for v in span):issues.append('span_conflict');continue
            r0,r1,c0,c1=span
            if (r0,r1+1,c0,c1+1)!=(cell['row'],cell['row']+cell['row_span'],cell['column'],cell['column']+cell['column_span']):issues.append('span_conflict');continue
            try:poly=quad8(box)
            except (ValueError,TypeError):poly=None
            cells.append(_cell(cell,id=f'{table_id}:{ci}',provider='rapidtable-3.0.2',version=prediction.get('model_sha256','slanet-plus-d57a942a'),
                table_id=table_id,poly=poly,original=poly,transform=IDENTITY[:],origin='structure_prediction',
                derivation=[{'operation':'slanet-plus-structure','original_logic_inclusive':span}],width=width,height=height))
    return [{'id':table_id,'provider':'rapidtable-3.0.2','region_id':prediction.get('region_id'),
        'polygon':box_polygon([0,0,width,height]),'cells':cells,'structure':parsed,'reason_codes':issues,'original_index':0}]


def adapt_prediction(prediction,width,height):
    if 'pdfplumber_tables' in prediction:
        output=[]
        for index, table in enumerate(prediction['pdfplumber_tables']):
            tid=table['id']
            cells=[_cell(c,id=f'{tid}:{c["id"]}',provider='pdfplumber',version=prediction['tool_version'],
                table_id=tid,poly=c['polygon'],original=box_polygon(c['original_box']),transform=table['transform'],
                origin='structure_prediction',derivation=[{'operation':'pdfplumber.find_tables','settings_sha256':prediction['settings_sha256'],
                    'original_table':index,'original_cell':c['id']}],width=width,height=height) for c in table['cells']]
            output.append({'id':tid,'provider':'pdfplumber','region_id':None,'polygon':checked_polygon(table['polygon'],width,height),
                'cells':cells,'structure':table,'reason_codes':[],'original_index':index})
        return output
    if 'candidate_tables' in prediction:
        return candidate_tables(prediction, width, height)
    if 'tf_table_cells' in prediction:
        return tableformer_tables(prediction,width,height)
    if 'cell_bboxes' in prediction:
        return rapid_tables(prediction,width,height)
    output=[]
    for index,item in enumerate(prediction.get('tables',[])):
        try:
            tables=paddle_tables({**prediction,'tables':[item]},width,height)
            for table in tables:table['original_index']=index
            output.extend(tables)
        except (ValueError,KeyError,TypeError):
            output.append({'id':f'invalid-paddle-table:{index}','provider':'paddle-table-v2','region_id':None,
                'polygon':None,'cells':[],'structure':None,'reason_codes':['invalid_polygon'],'original_index':index})
    return output


def candidate_tables(prediction, width, height):
    """Adapt isolated crop predictions without losing their original coordinates."""
    from dataclasses import replace
    output = []
    if prediction.get('coordinate_contract') != 'crop-pixels-to-image-affine-v1':
        raise ValueError('Unsupported candidate coordinate contract')
    for index, item in enumerate(prediction['candidate_tables']):
        box = item['table_box']
        region = checked_polygon(box_polygon(box), width, height)
        transform = [1, 0, box[0], 0, 1, box[1], 0, 0, 1]
        if item.get('crop_to_image') != transform:
            raise ValueError('Candidate crop transform mismatch')
        raw = item['prediction']
        if prediction.get('component') == 'tableformer-raw' and raw.get('source_semantics') != 'raw_structure':
            raise ValueError('TableFormer raw requires original structure boxes')
        tables = tableformer_tables(raw, box[2] - box[0], box[3] - box[1]) if 'tf_table_cells' in raw else rapid_tables(raw, box[2] - box[0], box[3] - box[1])
        for table in tables:
            tid = table['id'] + ':' + fingerprint({'region': item.get('region_id'), 'box': box, 'index': index})[:16]
            cells = []
            for cell in table['cells']:
                poly = checked_polygon(polygon(transform, cell.cell_polygon), width, height) if cell.cell_polygon else None
                cells.append(replace(cell, id=tid + ':' + cell.original_cell_id, table_id=tid,
                    cell_polygon=poly, transform=transform,
                    derivation=cell.derivation + [{'operation': 'crop_to_image', 'upstream_index': index,
                        'transform': transform, 'source_sha256': prediction.get('source_sha256'),
                        'upstream_artifact': prediction.get('upstream_artifact')}]))
            output.append({**table, 'id': tid, 'cells': cells, 'region_id': item.get('region_id'),
                           'polygon': region, 'original_index': index})
    return output


def tableformer_tables(prediction,width,height):
    """Experimental TFPredictor raw cells, before matching replaces their bbox."""
    table_id='tableformer:'+fingerprint(prediction['tf_table_cells'])[:24];cells=[]
    raw=prediction.get('source_semantics')=='raw_structure'
    seen = set()
    structure_cells = []
    for original in prediction['tf_table_cells']:
        if str(original['cell_id']) in seen:
            raise ValueError('Duplicate TableFormer original cell ID')
        seen.add(str(original['cell_id']))
        cell={'row':original['row_id'],'column':original['column_id'],'row_span':original.get('rowspan_val',1),
              'column_span':original.get('colspan_val',1),'text':''}
        if original.get('label') in ('ched','rhed','srow'):
            cell.update(is_header=True,header_role={'ched':'column','rhed':'row','srow':'section'}[original['label']])
        structure_cells.append(cell)
        try:poly=box_polygon(original['bbox'])
        except (ValueError,TypeError):poly=None
        cells.append(_cell(cell,id=f"{table_id}:{original['cell_id']}",provider='tableformer-accurate',version=prediction['model_sha256'],table_id=table_id,
            poly=poly,original=poly,transform=IDENTITY[:],origin='structure_prediction' if raw else 'text_extent',
            derivation=[{'operation':'tableformer_before_matching' if raw else 'tableformer_matching_postprocess',
                         'original_cell_id':original['cell_id'],'compression_applied':False}],width=width,height=height,
            reasons=() if raw else ('text_extent_only',)))
    structure = {'rows':max((c.row_end for c in cells),default=0),'columns':max((c.column_end for c in cells),default=0),'cells':structure_cells}
    return [{'id':table_id,'provider':'tableformer-accurate','region_id':None,'polygon':box_polygon([0,0,width,height]),
             'cells':cells,'structure':structure,'reason_codes':[],'original_index':0}]
