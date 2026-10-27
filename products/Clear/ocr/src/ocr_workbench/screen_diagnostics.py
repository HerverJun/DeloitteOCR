"""Read-only, source-bound screen OCR diagnostics and candidate comparisons.

No model, reference label, arithmetic repair, uniform-grid interpolation or
human acceptance is performed here. Missing evidence is explicitly unavailable.
Only observed cell boundaries with a verified mapping may create a cell highlight.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import unicodedata

from ocr_workbench.coordinates import bounds, box_polygon

VERSION = 'screen-diagnostics-v2'
RELIABLE_CELL_ORIGINS = {'direct_detection','verified_cell','native_cell'}


def observed_box(item):
    if not isinstance(item,dict):return None
    source=item.get('structure_source') or {}
    value=item.get('box',item.get('bbox',item.get('coordinate')))
    poly=item.get('polygon') or item.get('region_polygon') or source.get('cell_polygon')
    try:
        if value is None and poly:value=bounds(poly)
        if len(value)==4 and all(math.isfinite(float(v)) for v in value) and value[2]>value[0] and value[3]>value[1]:
            return list(map(float,value))
    except (TypeError,ValueError,IndexError):pass
    return None


def _area(box):return (box[2]-box[0])*(box[3]-box[1])


def _intersection(a,b):
    return max(0.,min(a[2],b[2])-max(a[0],b[0]))*max(0.,min(a[3],b[3])-max(a[1],b[1]))


def reliable_cell_box(cell, image_info=None):
    if not isinstance(cell,dict):return None
    source=cell.get('structure_source') or {}
    origin=cell.get('geometry_origin',source.get('geometry_origin'))
    if origin not in RELIABLE_CELL_ORIGINS:return None
    if source.get('range_semantics') not in (None,'full_cell'):return None
    box=observed_box(cell)
    info=image_info or {}
    if box and (box[0]<0 or box[1]<0 or info.get('width') and box[2]>info['width']
                or info.get('height') and box[3]>info['height']):return None
    return box


def _strong(region):
    score=region.get('score',region.get('confidence'))
    return region.get('grid_reliable') is True or (type(score) in (int,float) and math.isfinite(score)
            and score>=.8 and 'layout_table' in (region.get('sources') or []))


def _regions(info):
    return [{**region,'table_box':box} for region in info.get('business_regions',[]) or []
            if isinstance(region,dict) and (box:=observed_box({'box':region.get('table_box',region.get('box'))}))]


def _region_matches(tables, regions):
    """Unique spatial bindings, or the single-table/single-region identity only."""
    possible=[]
    for region in regions:
        rb=region['table_box']
        matches=[i for i,table in enumerate(tables) if (tb:=observed_box(table))
                 and _intersection(tb,rb)/min(_area(tb),_area(rb))>=.65]
        if not matches and len(tables)==len(regions)==1 and isinstance(tables[0],dict) and not observed_box(tables[0]):
            matches=[0]
        possible.append(matches)
    return [(matches[0],'observed_table_geometry' if observed_box(tables[matches[0]]) else 'single_table_single_region')
            if len(matches)==1 and sum(matches[0] in others for others in possible)==1 else (None,'no_unique_table_region')
            for matches in possible]


def _lines(region, axis):
    grid=region.get('observed_grid') or {}
    if region.get('grid_reliable') is not True or grid.get('coordinate_space')!='exif_normalized_original':return None
    values=grid.get('horizontal_lines' if axis=='row' else 'vertical_lines')
    if not isinstance(values,list) or not 2<=len(values)<=10001:return None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):return None
    if any(b<=a for a,b in zip(values,values[1:])):return None
    return values


def _edge_index(value, lines, tolerance):
    matches=[index for index,line in enumerate(lines) if abs(line-value)<=tolerance]
    return matches[0] if len(matches)==1 else None


def _comparable_table(table):
    if not isinstance(table,dict):return False
    rows,columns=table.get('rows'),table.get('columns')
    if type(rows) is not int or type(columns) is not int or min(rows,columns)<1:return False
    cells=table.get('cells')
    if not isinstance(cells,list):return False
    for cell in cells:
        if not isinstance(cell,dict) or not isinstance(cell.get('text'),str):return False
        values=[cell.get('row'),cell.get('column'),cell.get('row_span',1),cell.get('column_span',1)]
        if any(type(value) is not int for value in values):return False
        row,column,rs,cs=values
        if min(row,column)<0 or min(rs,cs)<1 or row+rs>rows or column+cs>columns:return False
    return True


def _digest(value):
    try:
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
    except (TypeError,ValueError):return None


def _literal(value):
    # Spaces are presentation differences; every visible glyph remains distinct.
    return ''.join(unicodedata.normalize('NFC',str(value)).split())


def _pixel_cell_partition(image, ys, xs):
    """Observe every segment before deriving rectangular connected cells.

    Missing interior borders join adjacent slots. Uncertain/broken edges,
    incomplete exterior borders and nonrectangular joins reject the whole grid.
    The candidate's declared merges never participate in this observation.
    """
    import cv2
    import numpy as np
    rows,columns=len(ys)-1,len(xs)-1
    min_width=min(b-a for a,b in zip(xs,xs[1:]));min_height=min(b-a for a,b in zip(ys,ys[1:]))
    band=min(4,max(2,math.floor(min(min_width,min_height)/8)))
    if min_width<20 or min_height<20:return None,'grid_segments_too_small'
    grey=cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2GRAY)
    ink=cv2.adaptiveThreshold(grey,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,31,12)
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(15,round(min_width*.4))),np.uint8))
    vertical=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(15,round(min_height*.5)),1),np.uint8))
    def segment(mask,line,a,b,is_horizontal):
        start=math.ceil(a+band+1);end=math.floor(b-band-1)
        lo=max(0,math.floor(line-band));hi=math.ceil(line+band)+1
        strip=mask[lo:hi,start:end] if is_horizontal else mask[start:end,lo:hi]
        return float(np.mean(np.any(strip>0,axis=0 if is_horizontal else 1))) if strip.size else None
    h=[[segment(horizontal,y,a,b,True) for a,b in zip(xs,xs[1:])] for y in ys]
    v=[[segment(vertical,x,a,b,False) for x in xs] for a,b in zip(ys,ys[1:])]
    samples=[value for line in h+v for value in line]
    if any(value is None or .12<value<.85 for value in samples):return None,'uncertain_grid_segment'
    if any(value<.85 for value in h[0]+h[-1]+[line[0] for line in v]+[line[-1] for line in v]):
        return None,'incomplete_grid_exterior'
    parent=list(range(rows*columns))
    def find(value):
        while parent[value]!=value:
            parent[value]=parent[parent[value]];value=parent[value]
        return value
    def join(a,b):parent[find(a)]=find(b)
    for row in range(rows):
        for column in range(columns):
            slot=row*columns+column
            if column+1<columns and v[row][column+1]<=.12:join(slot,slot+1)
            if row+1<rows and h[row+1][column]<=.12:join(slot,slot+columns)
    groups={}
    for slot in range(rows*columns):groups.setdefault(find(slot),[]).append(divmod(slot,columns))
    cells=[]
    for slots in groups.values():
        r0,c0=min(r for r,c in slots),min(c for r,c in slots)
        r1,c1=max(r for r,c in slots)+1,max(c for r,c in slots)+1
        if len(slots)!=(r1-r0)*(c1-c0):return None,'nonrectangular_observed_cell'
        interior=[h[r][c] for r in range(r0+1,r1) for c in range(c0,c1)]
        interior += [v[r][c] for r in range(r0,r1) for c in range(c0+1,c1)]
        if any(value>.12 for value in interior):return None,'inconsistent_observed_merge'
        cells.append({'row':r0,'column':c0,'row_span':r1-r0,'column_span':c1-c0,
                      'box':[xs[c0],ys[r0],xs[c1],ys[r1]]})
    return {'cells':sorted(cells,key=lambda item:(item['row'],item['column'])),
            'horizontal_segment_coverage':h,'vertical_segment_coverage':v,
            'segment_presence_threshold':.85,'segment_absence_threshold':.12},None


def _grid_source(raw, info, text_evidence):
    """Read only the hash-bound, EXIF-normalized original image."""
    path=Path(str(info.get('input_path') or ''))
    candidate_sha=raw.get('screen',{}).get('input_sha256')
    prepared_sha=info.get('input_sha256')
    evidence_version=text_evidence.get('provenance',{}).get('image_version') if isinstance(text_evidence,dict) else None
    if raw.get('project_image_version') and evidence_version!=raw['project_image_version']:
        return None,'auxiliary_image_version_unbound_or_conflicting'
    if not candidate_sha or candidate_sha!=prepared_sha:
        return None,'missing_or_conflicting_source_binding'
    try:
        from ocr_workbench.paths import require_path
        path=require_path(path, '屏拍原图', role='read')
        from PIL import Image
        from ocr_workbench.image_utils import normalized_rgb
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=prepared_sha:return None,'source_pixels_changed'
        with Image.open(io.BytesIO(data)) as opened:
            if opened.width*opened.height>24000000:return None,'grid_localization_work_limit'
            image=normalized_rgb(opened)
        if image.size!=(info.get('width'),info.get('height')):return None,'source_dimensions_disagree'
        return image,None
    except (OSError,ValueError,ImportError):return None,'source_pixels_unavailable'


def _observe_grid_cells(raw, image_info=None, text_evidence=None, *, source=None):
    """Bind complete observed cell topology using independent text anchors.

    Actual source bytes, line segments and high-confidence unique literals must
    agree with the candidate's topology. Matching dimensions alone are never a
    cell location. No candidate value, box or selection is changed.
    """
    raw,info=raw or {},image_info or {}
    tables=raw.get('tables',[]) or []
    if not isinstance(tables,list):tables=[]
    regions=_regions(info);bindings=_region_matches(tables,regions)
    if text_evidence is None:
        text_evidence=raw.get('screen',{}).get('business_metadata_evidence',{}).get('auxiliary_evidence',{})
    readings=text_evidence.get('text',text_evidence.get('blocks',[])) if isinstance(text_evidence,dict) else text_evidence or []
    if not isinstance(readings,list):readings=[]
    result={'version':'screen-grid-localization-v1','tables':[],'cell_count':0,
            'binding':{'input_sha256':info.get('input_sha256'),'table_snapshot_sha256':_digest(tables),
                       'auxiliary_text_sha256':_digest(readings),'observed_regions_sha256':_digest(regions)},
            'reference_input_used':False,'values_modified':False,'human_review':'not_performed'}
    if isinstance(text_evidence,dict) and text_evidence.get('provenance',{}).get('contains_reference_input') is True:
        raise ValueError('格位证据不能消费参考答案')
    eligible=[]
    for region,(ti,basis) in zip(regions,bindings):
        row={'table':ti,'region_id':region.get('id'),'mapping_basis':basis,'status':'unavailable','cells':[]}
        result['tables'].append(row)
        ys,xs=_lines(region,'row'),_lines(region,'column')
        if ti is None or not ys or not xs:
            row['reason']='no_unique_complete_observed_grid';continue
        grid=region.get('observed_grid') or {};table=tables[ti]
        if any(result['binding'][key] is None for key in ('table_snapshot_sha256','auxiliary_text_sha256','observed_regions_sha256')):
            row['reason']='invalid_observation_snapshot';continue
        if grid.get('source')!='pixel_grid' or grid.get('range_semantics')!='observed_line_positions':
            row['reason']='unsupported_grid_provenance';continue
        if (not _comparable_table(table) or table['rows']!=len(ys)-1 or table['columns']!=len(xs)-1
                or region.get('observed_grid_rows')!=len(ys)-1 or region.get('observed_grid_columns')!=len(xs)-1):
            row['reason']='candidate_grid_dimensions_disagree';continue
        if table['rows']*table['columns']>4096:
            row['reason']='grid_localization_work_limit';continue
        rb=region['table_box'];gb=[xs[0],ys[0],xs[-1],ys[-1]]
        if (gb[0]<0 or gb[1]<0 or type(info.get('width')) not in (int,float) or type(info.get('height')) not in (int,float)
                or gb[2]>info['width'] or gb[3]>info['height']):
            row['reason']='grid_exterior_outside_source';continue
        if _intersection(gb,rb)/max(_area(gb),_area(rb))<.9:
            row['reason']='grid_exterior_not_bound_to_region';continue
        eligible.append((row,region,table,ys,xs))
    if not eligible:return result
    image,source_reason=source if source is not None else _grid_source(raw,info,text_evidence)
    for row,region,table,ys,xs in eligible:
        if source_reason:
            row['reason']=source_reason;continue
        try:observed,reason=_pixel_cell_partition(image,ys,xs)
        except ImportError:observed,reason=None,'pixel_observer_unavailable'
        if observed is None:
            row['reason']=reason;continue
        row['pixel_observation']=observed
        topology=lambda cell:(cell['row'],cell['column'],cell.get('row_span',1),cell.get('column_span',1))
        actual={topology(cell):cell for cell in observed['cells']}
        declared=[topology(cell) for cell in table['cells']]
        if len(set(declared))!=len(declared) or set(declared)!=set(actual):
            row['reason']='candidate_observed_cell_partition_disagrees';continue
        visible=[(index,item) for index,item in enumerate(readings) if isinstance(item,dict)
            and str(item.get('text','')).strip() and item.get('kind') not in {'toolbar','taskbar','formula_bar','ruler','window_border','scrollbar','ui'}
            and (box:=observed_box(item)) and _intersection(box,region['table_box'])/_area(box)>=.9]
        literal_counts=Counter(_literal(cell['text']) for cell in table['cells'])
        observation_counts=Counter(_literal(item['text']) for _,item in visible)
        unique={_literal(cell['text']):topology(cell) for cell in table['cells']
                if literal_counts[_literal(cell['text'])]==1 and len(_literal(cell['text']))>=2}
        anchors=[];anchor_cells=set();conflicts=[]
        for index,item in visible:
            confidence=item.get('confidence',item.get('score'));literal=_literal(item['text'])
            if (not str(item.get('source','')).startswith('PP-OCR') or type(confidence) not in (int,float)
                    or not math.isfinite(confidence) or confidence<.9 or observation_counts[literal]!=1 or literal not in unique):continue
            box=observed_box(item)
            owners=[key for key,cell in actual.items() if _intersection(box,cell['box'])/_area(box)>=.9]
            if len(owners)!=1:continue
            if owners[0]!=unique[literal]:
                conflicts.append({'evidence_index':index,'text':item['text'],'observed_slot':list(owners[0]),'candidate_slot':list(unique[literal])})
            else:
                anchors.append({'evidence_index':index,'text':item['text'],'box':box,'confidence':confidence,
                                'source':item['source'],'slot':list(owners[0])})
                anchor_cells.add(owners[0])
        row.update(anchors=anchors,anchor_conflicts=conflicts)
        if conflicts:
            row['reason']='independent_text_anchor_position_conflict';continue
        row_anchors=[sum(key[0]<=index<key[0]+key[2] for key in anchor_cells) for index in range(table['rows'])]
        column_anchors=[sum(key[1]<=index<key[1]+key[3] for key in anchor_cells) for index in range(table['columns'])]
        row.update(row_anchor_counts=row_anchors,column_anchor_counts=column_anchors)
        if len(anchor_cells)<max(4,math.ceil(len(actual)*.5)) or min(row_anchors)<2 or min(column_anchors)<2:
            row['reason']='insufficient_independent_row_column_anchors';continue
        geometries=[]
        for cell in table['cells']:
            expected=actual[topology(cell)]['box'];existing=reliable_cell_box(cell,info)
            if existing and _intersection(existing,expected)/max(_area(existing),_area(expected))<.9:
                row['reason']='existing_cell_geometry_conflicts_with_grid';break
            geometries.append({**actual[topology(cell)],'polygon':box_polygon(expected),
                'geometry_origin':'observed_grid_with_text_anchors','range_semantics':'full_cell',
                'basis':'source_pixels_observed_partition_and_independent_row_column_anchors'})
        else:
            row.update(status='verified',reason=None,cells=geometries,
                       cell_boxes={':'.join(str(cell[key]) for key in ('row','column','row_span','column_span')):cell['box'] for cell in geometries})
            result['cell_count']+=len(geometries)
    return result


def observe_screen_grid_cells(raw, image_info=None, text_evidence=None):
    """Observe source cells, with a source-recomputed perspective fallback.

    The private pixel observer also serves the rectified plane. Only this public
    entry point loads evidence; callers cannot supply a replacement image.
    """
    raw,info=raw or {},image_info or {}
    if text_evidence is None:
        text_evidence=raw.get('screen',{}).get('business_metadata_evidence',{}).get('auxiliary_evidence',{})
    direct=_observe_grid_cells(raw,info,text_evidence)
    if direct['cell_count'] or not any(item.get('kind')=='perspective'
            for item in info.get('prepared_images',[]) if isinstance(item,dict)):
        return direct
    from ocr_workbench.screen_rectified_grid import observe_rectified_grid
    return observe_rectified_grid(raw,info,text_evidence,direct)


def localized_grid_cell(localization, table_index, cell):
    if not localization or not isinstance(cell,dict):return None
    tables=[table for table in localization.get('tables',[]) if table.get('status')=='verified' and table.get('table')==table_index]
    key=lambda value:tuple(value.get(name,1) for name in ('row','column','row_span','column_span'))
    cells=[value for value in tables[0].get('cells',[]) if key(value)==key(cell)] if len(tables)==1 else []
    return cells[0] if len(cells)==1 else None


def localized_grid_box(localization, table_index, cell):
    """Read a freshly computed proof; a logical index alone remains insufficient."""
    if not localization or not isinstance(cell,dict):return None
    tables=[table for table in localization.get('tables',[]) if table.get('status')=='verified' and table.get('table')==table_index]
    key=':'.join(str(cell.get(name,1)) for name in ('row','column','row_span','column_span'))
    box=tables[0].get('cell_boxes',{}).get(key) if len(tables)==1 else None
    return list(box) if box else None


def diagnose_screen_structure(raw, image_info=None, text_evidence=None, *, structure_evidence=None, grid_localization=None):
    """Check counts and spans against independent observations, never fill cells."""
    raw,info=raw or {},image_info or {}
    tables=raw.get('tables',[]) or []
    if not isinstance(tables,list):tables=[]
    regions=_regions(info);bindings=_region_matches(tables,regions)
    issues=[]
    checks={'observed_columns':'unavailable','observed_spans':'unavailable',
            'cell_alignment':'unavailable','independent_structure':'unavailable','ui_ownership':'unavailable'}
    readings=text_evidence.get('text',text_evidence.get('blocks',[])) if isinstance(text_evidence,dict) else text_evidence or []
    if not isinstance(readings,list):readings=[]
    grid_localization=grid_localization if grid_localization is not None else observe_screen_grid_cells(raw,info,text_evidence)
    checks['observed_cell_mapping']='verified' if grid_localization.get('cell_count') else 'unavailable'
    excluded=[item for item in info.get('excluded_regions',[]) or [] if isinstance(item,dict) and observed_box(item)]
    for region,(ti,basis) in zip(regions,bindings):
        rb=region['table_box']
        if not tables and _strong(region):
            issues.append({'code':'empty_table_with_region_evidence','severity':'high','upgrade_kind':'structure',
                'message':'独立布局或可见网格支持业务表区，但本候选没有表格；不能直接判为真实无表。',
                'region_id':region.get('id'),'box':rb,'mapping_basis':basis,
                'evidence_source':list(region.get('sources') or []),'evidence_kind':'observed_business_region'})
        if ti is None:continue
        table=tables[ti]
        visible=[index for index,item in enumerate(readings) if isinstance(item,dict) and str(item.get('text','')).strip()
                 and item.get('kind') not in {'toolbar','taskbar','formula_bar','ruler','window_border','scrollbar','ui'}
                 and (box:=observed_box(item)) and _intersection(box,rb)/_area(box)>=.65]
        if _strong(region) and len(visible)>=2 and not any(str(cell.get('text','')).strip()
                for cell in table.get('cells',[]) if isinstance(cell,dict)):
            issues.append({'code':'empty_table_text_with_visible_evidence','severity':'high','upgrade_kind':'text',
                'message':'独立文字证据显示表区有可见内容，但候选表格全空；保留未知，不补猜原值。',
                'table':ti,'region_id':region.get('id'),'box':rb,'evidence_source':'independent_auxiliary_text',
                'evidence_kind':'observed_text_in_empty_table','evidence_indices':visible,'mapping_basis':basis})
        for axis in ('rows','columns'):
            observed=region.get('observed_grid_'+axis)
            returned=table.get(axis)
            if region.get('grid_reliable') is not True or type(observed) is not int or observed<1 or type(returned) is not int:continue
            if axis=='columns':checks['observed_columns']='checked'
            if observed!=returned:
                issues.append({'code':('missing_observed_' if observed>returned else 'extra_observed_')+axis,
                    'severity':'high','upgrade_kind':'structure','message':'可靠可见网格的行列数与候选不同；保留候选，核对结构。',
                    'table':ti,'region_id':region.get('id'),'box':rb,'observed':observed,'returned':returned,
                    'mapping_basis':basis,'evidence_source':'pixel_grid','evidence_kind':'observed_grid_count'})
        row_lines,column_lines=_lines(region,'row'),_lines(region,'column')
        if not row_lines or not column_lines:continue
        for cell in table.get('cells',[]) or []:
            cb=reliable_cell_box(cell,info) or localized_grid_box(grid_localization,ti,cell)
            if not cb:continue
            row_tolerance=max(2.,(rb[3]-rb[1])*.003)
            column_tolerance=max(2.,(rb[2]-rb[0])*.003)
            rs,re=_edge_index(cb[1],row_lines,row_tolerance),_edge_index(cb[3],row_lines,row_tolerance)
            cs,ce=_edge_index(cb[0],column_lines,column_tolerance),_edge_index(cb[2],column_lines,column_tolerance)
            if any(value is None for value in (rs,re,cs,ce)) or re<=rs or ce<=cs:continue
            checks['observed_spans']='checked';checks['cell_alignment']='checked'
            declared=(cell.get('row'),cell.get('column'),cell.get('row_span',1),cell.get('column_span',1))
            observed=(rs,cs,re-rs,ce-cs)
            # Shift indices are meaningful only when complete axis counts agree.
            same_grid=table.get('rows')==len(row_lines)-1 and table.get('columns')==len(column_lines)-1
            span_difference=declared[2:]!=observed[2:]
            index_difference=same_grid and declared[:2]!=observed[:2]
            if span_difference or index_difference:
                issues.append({'code':'observed_span_disagreement' if span_difference else 'observed_cell_misalignment',
                    'severity':'high','upgrade_kind':'structure','message':'实际单元格边界与候选合并跨度或格位不一致，不自动移动或补值。',
                    'table':ti,'row':cell.get('row'),'column':cell.get('column'),'region_id':region.get('id'),
                    'box':cb,'current_span':list(declared),'observed_span':list(observed),
                    'evidence_source':'pixel_grid','evidence_kind':'observed_lines_and_full_cell_geometry',
                    'mapping_basis':'observed_full_cell_edges'})
    for ti,table in enumerate(tables):
        if not isinstance(table,dict):continue
        for cell in table.get('cells',[]) or []:
            cb=reliable_cell_box(cell,info)
            if not cb:continue
            if excluded:checks['ui_ownership']='checked'
            ui_matches=[item for item in excluded if _intersection(cb,observed_box(item))/_area(cb)>=.65]
            if ui_matches:
                issues.append({'code':'cell_overlaps_observed_ui','severity':'high','upgrade_kind':'structure',
                    'message':'候选单元格位置主要落在已观测界面区域；核对表区，不删除原值。',
                    'table':ti,'row':cell.get('row'),'column':cell.get('column'),'box':cb,
                    'evidence_source':[item.get('source') for item in ui_matches],
                    'evidence_kind':'observed_ui_and_full_cell_geometry','ui_kinds':[item.get('kind') for item in ui_matches]})
    if structure_evidence:
        current_version=raw.get('project_image_version')
        proposed_version=structure_evidence.get('image_version')
        if current_version and proposed_version and current_version!=proposed_version:
            issues.append({'code':'structure_image_version_mismatch','severity':'high','upgrade_kind':'structure',
                           'message':'独立结构候选属于另一图像版本，不能用于当前格位核验。'})
            checks['independent_structure']='rejected_version_mismatch'
        elif structure_evidence.get('provenance',{}).get('contains_reference_input') is True:
            raise ValueError('诊断不能消费参考答案')
        else:
            from ocr_workbench.structure_diagnostics import differences,identify_tables
            proposed=[candidate for candidate in structure_evidence.get('tables',[]) if isinstance(candidate,dict) and _comparable_table(candidate.get('skeleton'))]
            current=[(index,table) for index,table in enumerate(tables) if _comparable_table(table)]
            if len(proposed)!=len(structure_evidence.get('tables',[])):
                issues.append({'code':'invalid_independent_structure','severity':'high','upgrade_kind':'structure',
                               'message':'独立结构候选有无效行列或格位，保留原证据，不能当作可靠结构。'})
            matches=identify_tables([table for _,table in current],proposed)
            checks['independent_structure']='checked' if any(matches) else 'unavailable_no_unique_table_identity'
            for (ti,table),match in zip(current,matches):
                if not match:continue
                _,index,anchors=match;candidate=proposed[index]
                for difference in differences(table,candidate['skeleton']):
                    issues.append({'code':'independent_structure_'+difference['kind'],'severity':'high','upgrade_kind':'structure',
                        'message':'独立结构候选与当前行列或合并分区不一致；这是比较证据，不是正确答案。',
                        'table':ti,'row':difference.get('row'),'column':difference.get('column'),
                        'current':difference.get('current'),'alternative':difference.get('candidate'),
                        'candidate_table_id':candidate.get('id'),'evidence_source':candidate.get('provider'),
                        'evidence_kind':'independent_structure_candidate','mapping_basis':anchors})
    return {'version':VERSION,'issues':issues,'checks':checks,'human_review':'not_performed','values_modified':False}


def _location(issue,tables,info,grid_localization=None):
    ti,row,column=issue.get('table'),issue.get('row'),issue.get('column')
    table=tables[ti] if type(ti) is int and 0<=ti<len(tables) and isinstance(tables[ti],dict) else None
    targets=[cell for cell in table.get('cells',[]) if isinstance(cell,dict) and cell.get('row')==row and cell.get('column')==column] if table and row is not None and column is not None else []
    cell=targets[0] if len(targets)==1 else None
    box=reliable_cell_box(cell,info)
    grid_box=localized_grid_box(grid_localization,ti,cell)
    if not box:box=grid_box
    if box and any(other is not cell and (other_box:=reliable_cell_box(other,info))
                   and _intersection(box,other_box)/min(_area(box),_area(other_box))>=.8
                   for other in table.get('cells',[]) if isinstance(other,dict)):
        box=None
    geometry=localized_grid_cell(grid_localization,ti,cell) if grid_box else None
    if box:return {'level':'cell','table':ti,'row':row,'column':column,'box':box,
                   'polygon':deepcopy(geometry['polygon']) if geometry else box_polygon(box),'range_semantics':'full_cell',
                   'basis':geometry['basis'] if geometry else 'source_pixels_observed_partition_and_independent_row_column_anchors' if grid_box else 'observed_full_cell_geometry'}
    regions=[region for region in _regions(info) if region.get('id')==issue.get('region_id')]
    box=observed_box(table) if table else None
    if not box and len(regions)==1:box=regions[0]['table_box']
    if not box and table and len(tables)==len(_regions(info))==1:box=_regions(info)[0]['table_box']
    if table is not None or regions:
        return {'level':'table','table':ti if table else None,'region_id':issue.get('region_id'),
                'box':box,'polygon':box_polygon(box) if box else None,'range_semantics':'table_region',
                'basis':'observed_table_region' if box else 'candidate_table_without_source_geometry',
                'unlocalized_cell':{'row':row,'column':column} if row is not None and column is not None else None}
    return {'level':'image','box':None,'range_semantics':'image','basis':'no_reliable_local_geometry'}


def _actions(issue,location,remaining_seconds,capabilities):
    kind=issue.get('upgrade_kind')
    options=[('review_original','核对原图',0,False)]
    if issue.get('code') in {'unreadable_source','image_edge_cutoff','image_low_detail'}:
        options.append(('request_better_source','补充清晰、完整照片',0,False))
    elif kind=='structure':
        options.append(('structure_compare','比较已有结构候选',0,True))
        options.append(('precision_subbatch','对所选图片提高精度',180,True))
    elif kind in {'text','visual'}:
        if location['level']=='cell':options.append(('local_reread','在可靠格位独立局部复识',60,True))
        options.append(('precision_subbatch','对所选图片提高精度',180,True))
    output=[]
    for action,label,reserve,compute in options:
        available=True if not compute else (capabilities or {}).get(action)
        # Existing-candidate comparison is read-only CPU work. A precision
        # child reserves only the available allowance; 180 is its ceiling.
        minimum=10 if action=='precision_subbatch' else reserve
        budget_ok=action=='structure_compare' or remaining_seconds is None or remaining_seconds>=minimum
        if action=='precision_subbatch' and remaining_seconds is not None:
            reserve=min(reserve,max(0,remaining_seconds))
        output.append({'action':action,'label':label,'tool_required':compute,'capability_available':available,
                       'enabled':available is True and budget_ok,'budget_seconds':reserve,'budget_kind':'maximum_reservation',
                       'reason':None if available is True and budget_ok else 'remaining_budget_insufficient' if not budget_ok else 'capability_not_verified',
                       'automatic_apply':False})
    return output


def build_screen_diagnostics(raw, image_info=None, text_evidence=None, *, assessment=None,
                             structure_evidence=None, remaining_seconds=None, capabilities=None):
    """Create bounded-by-caller review items; no source issue is dropped."""
    if remaining_seconds is not None and (type(remaining_seconds) not in (int,float) or not math.isfinite(remaining_seconds) or remaining_seconds<0):
        raise ValueError('剩余预算必须是非负有限秒数')
    raw,info=raw or {},image_info or {}
    if assessment is None:
        from ocr_workbench.screen_quality import assess_screen_result
        assessment=assess_screen_result(raw,info,text_evidence)
        localization=assessment['grid_localization']
    else:
        # A persisted assessment cannot certify unchanged source bytes, values
        # or anchors. Re-observe before using its logical issue coordinates.
        localization=observe_screen_grid_cells(raw,info,text_evidence)
    observed=diagnose_screen_structure(raw,info,text_evidence,structure_evidence=structure_evidence,grid_localization=localization)
    issues=deepcopy(assessment.get('issues',[]));seen={(issue.get('code'),issue.get('table'),issue.get('row'),issue.get('column'),issue.get('region_id')) for issue in issues}
    for issue in observed['issues']:
        key=(issue.get('code'),issue.get('table'),issue.get('row'),issue.get('column'),issue.get('region_id'))
        if key not in seen:issues.append(issue);seen.add(key)
    tables=raw.get('tables',[]) if isinstance(raw.get('tables',[]),list) else []
    items=[];groups={}
    text=text_evidence.get('text',text_evidence.get('blocks',[])) if isinstance(text_evidence,dict) else text_evidence or []
    if not isinstance(text,list):text=[]
    for index,issue in enumerate(issues):
        location=_location(issue,tables,info,localization)
        evidence=[]
        ei=issue.get('evidence_index')
        if type(ei) is int and 0<=ei<len(text):evidence.append({'source_index':ei,'observation':deepcopy(text[ei])})
        if issue.get('evidence_kind'):evidence.append({'kind':issue['evidence_kind'],'source':issue.get('evidence_source'),
            'box':deepcopy(issue.get('box')),'current':issue.get('current_span',issue.get('current')),
            'alternative':issue.get('observed_span',issue.get('alternative')),'mapping_basis':issue.get('mapping_basis')})
        record={'id':f'issue-{index+1}','code':issue.get('code'),'severity':issue.get('severity','medium'),
                'reason':issue.get('message',issue.get('reason','需要复核')),'location':location,
                'evidence':evidence,'details':deepcopy(issue),'human_review':'not_performed',
                'applicable_actions':_actions(issue,location,remaining_seconds,capabilities),'automatic_apply':False}
        items.append(record)
        key=(location['level'],location.get('table'),location.get('region_id'),location.get('row'),location.get('column'))
        group=groups.setdefault(key,{'location':location,'issue_ids':[],'codes':[]})
        group['issue_ids'].append(record['id']);group['codes'].append(record['code'])
    return {'version':VERSION,'items':items,'groups':list(groups.values()),'checks':{**assessment.get('checks',{}),**observed['checks']},
            'summary':{'issues':len(items),'groups':len(groups),'cell':sum(item['location']['level']=='cell' for item in items),
                       'table':sum(item['location']['level']=='table' for item in items),'image':sum(item['location']['level']=='image' for item in items)},
            'budget':{'remaining_seconds':remaining_seconds,'estimated_inference_time':None},
            'binding':{'image_version':raw.get('project_image_version'),'input_sha256':raw.get('screen',{}).get('input_sha256'),
                       'candidate_id':raw.get('screen_selection',{}).get('candidate_id'),
                       'table_snapshot_sha256':hashlib.sha256(json.dumps(tables,ensure_ascii=False,sort_keys=True,
                           separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()},
            'grid_localization':localization,
            'review_required':any(item['severity'] in {'high','medium'} for item in items),
            'human_review':'not_performed','values_modified':False,'reference_input_used':False}


def compare_screen_candidates(current, alternative, image_info=None, text_evidence=None, *,
                              current_id='current', alternative_id='alternative', remaining_seconds=None, capabilities=None):
    """Compare real candidates without replacing the user's selected result."""
    from ocr_workbench.screen_quality import select_screen_candidates
    if current_id==alternative_id:raise ValueError('候选身份必须不同')
    for candidate in (current,alternative):
        if candidate.get('provenance',{}).get('contains_reference_input') is True:raise ValueError('比较不能消费参考答案')
    version_a,version_b=current.get('project_image_version'),alternative.get('project_image_version')
    if version_a and version_b and version_a!=version_b:raise ValueError('候选属于不同图像版本')
    selected=select_screen_candidates([{'id':current_id,'result':current},{'id':alternative_id,'result':alternative}],
                                      image_info,text_evidence,allow_text_fixes=False)
    # The existing ranker may recommend the alternative, but selection is not
    # applied. Report all disagreements, with original candidate identities.
    assessment=selected['assessment_by_id'][current_id]
    conflicts=deepcopy(selected['conflicts'])
    for conflict in conflicts:
        if conflict.get('selected_id')!=current_id:
            if 'value' in conflict and 'alternative' in conflict:
                conflict['value'],conflict['alternative']=conflict['alternative'],conflict['value']
            conflict.update(selected_id=current_id,candidate_id=alternative_id)
    conflict_issues=[{**conflict,'message':conflict.get('message','两个实际候选不同；请依据原图选择，不以票数决定。'),
                     'upgrade_kind':'structure' if 'structure' in conflict['code'] or 'count' in conflict['code'] else 'text'}
                    for conflict in conflicts]
    diagnostics=build_screen_diagnostics(current,image_info,text_evidence,
        assessment={**assessment,'issues':[*(assessment.get('issues',[])),*conflict_issues]},
        remaining_seconds=remaining_seconds,capabilities=capabilities)
    return {'version':VERSION,'current_id':current_id,'alternative_id':alternative_id,
            'recommended_candidate_id':selected['selected_id'],'selected_candidate_id':current_id,
            'candidate_assessments':selected['assessments'],'conflicts':conflicts,'diagnostics':diagnostics,
            'selection_applied':False,'values_modified':False,'human_review':'not_performed','reference_input_used':False}
