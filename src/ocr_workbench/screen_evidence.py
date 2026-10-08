"""Inference-only context, local rereads and independent visual proposals.

GPU sessions are caller-owned so the queue can group all context tasks and reuse
PP-DocLayout/PP-OCR before switching to table transcription. Nothing here starts
a competing GPU worker, downloads a model, or consumes a reference label.
"""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from ocr_workbench.coordinates import IDENTITY, bounds, box_polygon, polygon

POLICY_VERSION = "screen-evidence-v4"
LABELS = {"table":"table", "doc_title":"title", "paragraph_title":"title", "figure_title":"caption",
          "table_title":"caption", "footnote":"note", "vision_footnote":"note",
          "footer":"note", "header":"note", "page_number":"page_number",
          "text":"text", "unit":"unit", "caption":"caption", "note":"note", "title":"title",
          "toolbar":"toolbar", "taskbar":"taskbar", "formula_bar":"formula_bar", "ruler":"ruler",
          "window_border":"window_border", "scrollbar":"scrollbar", "ui":"ui"}
UI_KINDS = {"toolbar","taskbar","formula_bar","ruler","window_border","scrollbar","ui"}


def _box(item):
    value = item.get("box",item.get("coordinate",item.get("bbox")))
    try:
        if value is None and item.get("polygon"):
            value = bounds(item["polygon"])
        if len(value) == 4 and all(math.isfinite(float(v)) for v in value) and value[0] < value[2] and value[1] < value[3]:
            return list(map(float,value))
    except (TypeError,ValueError):
        pass
    return None


def _area(box):
    return (box[2]-box[0])*(box[3]-box[1])


def _intersection(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def _mapped(item, transform, width, height):
    box = _box(item)
    if box is None:
        return None
    mapped_polygon = polygon(transform,item.get("polygon") or box_polygon(box))
    box = bounds(mapped_polygon)
    if box[2] <= 0 or box[3] <= 0 or box[0] >= width or box[1] >= height:
        return None
    clipped = [max(0,box[0]),max(0,box[1]),min(width,box[2]),min(height,box[3])]
    return {**deepcopy(item),"box":clipped,"polygon":mapped_polygon,
            "coordinate_space":"exif_normalized_original", "partially_clipped":clipped != box}


def classify_screen_ui(layout, texts, width, height):
    """Infer chrome only from multiple semantic anchors and external geometry.

    A business 'Excel', 'Save', number, or formula is insufficient. A detected
    table protects overlapping text. Broad strips require independent observed
    chrome categories and may never cross a business table's vertical extent.
    """
    layout,texts = deepcopy(layout),deepcopy(texts)
    tables = [_box(item) for item in layout if item.get('kind') == 'table' and _box(item)]
    protected=[]
    for item in layout:
        box=_box(item);confidence=item.get('confidence',item.get('score'))
        semantic=item.get('kind') in {'title','caption','unit','note'} or item.get('original_label') in {'footnote','vision_footnote','table_title','doc_title'}
        if box and semantic and isinstance(confidence,(int,float)) and confidence>=.85:
            protected.append(box)
    anchors=[]
    for item in texts:
        box=_box(item)
        if not box or any(_intersection(box,tb)/_area(box)>.15 for tb in tables+protected):
            continue
        value=re.sub(r'\s+','',str(item.get('text','')))
        category=None
        if re.search(r'\.(?:xlsx?|csv|ods).{0,12}(?:Excel|WPS)',value,re.I):
            category='window_border'
        elif sum(token in value for token in ('文件','开始','插入','页面布局','公式','数据','审阅','视图'))>=4:
            category='toolbar'
        elif sum(token in value for token in ('粘贴','剪切','复制','合并后居中','排序和筛选','自动求和','Calibri'))>=2:
            category='toolbar'
        elif re.search(r'fx[=:：]?=.*(?:\(|\d)',value,re.I):
            category='formula_bar'
        elif re.search(r'状态[:：]?就绪|status[:：]?ready',value,re.I):
            category='taskbar'
        if category:
            anchors.append((category,box,item))
    categories={kind for kind,_,_ in anchors}
    screen_confirmed='window_border' in categories and bool(categories&{'toolbar','formula_bar'}) or {'toolbar','formula_bar'}<=categories
    groups=[]
    if screen_confirmed:
        top=[box for kind,box,_ in anchors if kind!='taskbar' and box[3] <= min((tb[1] for tb in tables),default=height*.6)]
        if top:
            y0=min(b[1] for b in top);y1=max(b[3] for b in top)
            pad=max(3,min(14,float(np_median([b[3]-b[1] for b in top]))*.45))
            y1=min(y1+pad,min((tb[1] for tb in tables),default=height))
            groups.append({'kind':'toolbar','box':[0,max(0,y0-pad),width,y1],'orientation':'horizontal'})
        bottom=[box for kind,box,_ in anchors if kind=='taskbar' and box[1]>=max((tb[3] for tb in tables),default=height*.65)]
        if bottom:
            y0=min(b[1] for b in bottom)
            groups.append({'kind':'taskbar','box':[0,max(max((tb[3] for tb in tables),default=0),y0-8),width,height],'orientation':'horizontal'})
    for group in groups:
        if any(_intersection(group['box'],box)>0 for box in protected):
            # A trusted title/unit/note contradicts a broad strip. Preserve the
            # observed content and let region ambiguity select the original.
            continue
        group.update(polygon=box_polygon(group['box']),source='observed_screen_chrome',confidence=None,
                     inference_basis='multiple_application_anchors_outside_detected_tables')
        if not any(item.get('source')=='observed_screen_chrome' and _box(item)==group['box'] for item in layout):
            layout.append(group)
        for item in texts:
            box=_box(item)
            if box and _intersection(box,group['box'])/_area(box)>=.8 and not any(_intersection(box,tb)/_area(box)>.15 for tb in tables+protected):
                item.setdefault('business_original_kind',item.get('kind','text'))
                item['kind']=group['kind'];item['orientation']=group['orientation']
                item['classification_basis']=group['inference_basis']
    return layout,texts,screen_confirmed


def np_median(values):
    # Keep normalization usable without numpy in lightweight control processes.
    values=sorted(values);middle=len(values)//2
    return values[middle] if len(values)%2 else (values[middle-1]+values[middle])/2


def _anchored_business_titles(layout,texts):
    """Classify one observed heading above an independently read unit line.

    A table, a high-confidence exterior unit line and a unique exterior heading
    must agree spatially. This keeps the actual heading pixels/text; it neither
    invents a title nor restores a missing glyph. The inferred role still needs
    review. A bare nearby number, ruler token or conflicting semantic block is
    insufficient. Multiple possible table owners remain ambiguous.
    """
    # Re-evaluate our own derived role when the caller adds or changes evidence.
    # A formerly unique owner must not survive a newly ambiguous table context.
    for item in texts:
        if item.get('classification_basis')=='observed_title_unit_table_chain':
            item['kind']=item.get('business_original_kind','text')
            for key in ('classification_basis','context_table_box','semantic_role_review_required'):
                item.pop(key,None)
    tables=[item for item in layout if item.get('kind')=='table' and _box(item)
            and isinstance(item.get('confidence',item.get('score')),(int,float))
            and item.get('confidence',item.get('score'))>=.8]
    units=[item for item in texts if _box(item) and item.get('kind') not in UI_KINDS
           and re.match(r'^\s*(?:单位\s*[:：]|unit(?:s)?\s*[:：])',str(item.get('text','')),re.I)
           and isinstance(item.get('confidence'),(int,float)) and item['confidence']>=.9
           and not any(_intersection(_box(item),_box(table))/_area(_box(item))>.15 for table in tables)]
    bindings={}
    for table in tables:
        tb=_box(table);tw=tb[2]-tb[0]
        for unit in units:
            ub=_box(unit);uh=ub[3]-ub[1]
            overlap=max(0.,min(ub[2],tb[2])-max(ub[0],tb[0]))/min(tw,ub[2]-ub[0])
            if overlap<.8 or not 0<=tb[1]-ub[3]<=max(40,uh*2):continue
            possible=[]
            for index,item in enumerate(texts):
                box=_box(item);value=str(item.get('text',''))
                if (not box or item.get('kind') in UI_KINDS|{'note','caption','unit'} or
                        len(''.join(value.split()))<8 or not isinstance(item.get('confidence'),(int,float))
                        or item['confidence']<.9):continue
                if re.match(r'^\s*(?:单位\s*[:：]|表注\s*[:：]|注\s*[:：]|unit(?:s)?\s*[:：]|note\s*[:：])',value,re.I):continue
                h=box[3]-box[1];gap=ub[1]-box[3]
                horizontal=max(0.,min(box[2],tb[2])-max(box[0],tb[0]))/min(tw,box[2]-box[0])
                conflicting=any(part.get('kind') in UI_KINDS|{'note','caption','unit','table'} and _box(part)
                    and _intersection(box,_box(part))/_area(box)>=.65 for part in layout)
                if (not conflicting and -min(h,uh)*.25<=gap<=max(30,max(h,uh)*1.5)
                        and box[3]<=tb[1] and horizontal>=.8
                        and abs(box[0]-tb[0])<=max(25,tw*.1)
                        and box[2]-box[0]>=max(80,tw*.2)):
                    possible.append(index)
            if len(possible)==1:bindings.setdefault(possible[0],[]).append(tb)
    for index,owners in bindings.items():
        distinct={tuple(box) for box in owners}
        if len(distinct)!=1:continue
        item=texts[index]
        item.setdefault('business_original_kind',item.get('kind','text'))
        item.update(kind='title',classification_basis='observed_title_unit_table_chain',
                    context_table_box=list(next(iter(distinct))),semantic_role_review_required=True)
    return texts


def reconcile_screen_evidence(evidence, *, width, height):
    """Reclassify actual old/new evidence, preserving every observed reading."""
    evidence=deepcopy(evidence or {})
    layout=evidence.get('layout',evidence.get('regions',[]))
    texts=evidence.get('text',evidence.get('blocks',evidence.get('text_boxes',[])))
    if isinstance(layout,dict):layout=layout.get('regions',layout.get('blocks',[]))
    if isinstance(texts,dict):texts=texts.get('blocks',texts.get('text_boxes',[]))
    layout=[{**item,'box':box,'kind':LABELS.get(item.get('original_label',item.get('label')),item.get('kind','text'))}
            for item in layout if isinstance(item,dict) and (box:=_box(item))]
    texts=[{**item,'box':box} for item in texts if isinstance(item,dict) and (box:=_box(item))]
    layout,texts,confirmed=classify_screen_ui(layout,texts,width,height)
    texts=_anchored_business_titles(layout,texts)
    rulers=_sequential_rulers(layout,texts,screen_confirmed=confirmed)
    layout.extend(rulers)
    return {**evidence,'layout':layout,'text':texts,'screen_chrome_confirmed':confirmed,
            'region_reconciliation_policy':POLICY_VERSION}


def organize_screen_business_metadata(raw, preparation, evidence):
    """Separate independent business context from full auxiliary OCR evidence.

    Spatial ownership, rather than literal equality or UI keywords, determines
    whether a reading belongs in the default notes sheet. Every input reading
    remains in auxiliary_evidence and receives a disposition. The candidate's
    tables and cell values are never changed. Ambiguous titles/notes are retained
    with a review reason, not discarded or asserted to belong to a guessed table.
    """
    raw, preparation, original = raw or {}, preparation or {}, deepcopy(evidence or {})
    width, height = preparation.get('width'), preparation.get('height')
    if isinstance(width,(int,float)) and isinstance(height,(int,float)) and width > 0 and height > 0:
        reconciled = reconcile_screen_evidence(original,width=width,height=height)
    else:
        reconciled = deepcopy(original)
    layout = reconciled.get('layout',[]) or []
    texts = reconciled.get('text',reconciled.get('blocks',[])) or []
    if isinstance(texts,dict):
        texts = texts.get('blocks',texts.get('text_boxes',[]))
    if not isinstance(texts,list):texts=[]
    if not isinstance(layout,list):layout=[]
    original_texts = original.get('text',original.get('blocks',[])) or []
    if isinstance(original_texts,dict):original_texts=original_texts.get('blocks',original_texts.get('text_boxes',[]))
    if not isinstance(original_texts,list):original_texts=[]
    # Reconciliation filters invalid geometry, but the disposition ledger still
    # accounts for those original observations and the full original is retained.
    readings = []
    for index,item in enumerate(original_texts):
        if not isinstance(item,dict):
            readings.append(({'text':'','invalid_observation':True}, {'kind':'text','index':index}))
            continue
        matches = [value for value in texts if isinstance(value,dict) and value.get('text')==item.get('text') and _box(value)==_box(item)]
        readings.append((matches[0] if matches else deepcopy(item), {'kind':'text','index':index}))
    for index,item in enumerate(layout):
        if isinstance(item,dict) and str(item.get('text','')).strip():
            readings.append((item, {'kind':'layout','index':index}))
    regions = []
    for index,region in enumerate(preparation.get('business_regions',[]) or []):
        if not isinstance(region,dict):continue
        box = _box({'box':region.get('table_box',region.get('box'))})
        if box:
            regions.append({**region,'id':region.get('id',f'region-{index+1}'),'table_box':box})
    tables = raw.get('tables',[]) or []
    if not isinstance(tables,list):tables=[]
    ui = [item for item in layout + list(preparation.get('excluded_regions',[]) or [])
          if isinstance(item,dict) and item.get('kind') in UI_KINDS and _box(item)]
    semantic_kinds = {'title','caption','unit','note'}
    semantic = [item for item in layout if isinstance(item,dict) and item.get('kind') in semantic_kinds and _box(item)]
    items, decisions = [], []

    def table_owner(region):
        matches = [index for index,table in enumerate(tables) if isinstance(table,dict) and (box:=_box(table))
                   and _intersection(box,region['table_box'])/min(_area(box),_area(region['table_box'])) >= .75]
        if len(matches)==1:return matches[0],'observed_table_geometry'
        if len(tables)==len(regions)==1:return 0,'single_table_single_region'
        return None,'no_unique_output_table_mapping'

    def represented(value,owners):
        normalized = ''.join(value.split())
        for region in owners:
            table,_ = table_owner(region)
            if table is not None and any(normalized==''.join(str(cell.get('text','')).split())
                for cell in tables[table].get('cells',[]) if isinstance(cell,dict)):
                return True
        return False

    for reading,reference in readings:
        box, value = _box(reading), str(reading.get('text',''))
        decision = {'evidence_ref':reference,'text':value,'box':box,'source':reading.get('source'),
                    'human_review':'not_performed'}
        if not value.strip():
            decision.update(disposition='evidence_only',reason='invalid_observation' if reading.get('invalid_observation') else 'no_visible_text')
            decisions.append(decision);continue
        if not box:
            # No invented coordinates. Explicit business labels still survive.
            if reading.get('kind') in semantic_kinds:
                entry={'kind':reading['kind'],'text':value,'box':None,'source':reading.get('source'),
                       'confidence':reading.get('confidence'),'region_id':None,'table':None,
                       'ownership_basis':'missing_geometry','review_required':True,'human_confirmed':False,
                       'evidence_refs':[reference]}
                items.append(entry);decision.update(disposition='retained_context',reason='missing_geometry',item_index=len(items)-1)
            else:
                decision.update(disposition='evidence_only',reason='missing_geometry',review_required=True)
            decisions.append(decision);continue
        labels = [part for part in semantic if _intersection(box,_box(part))/_area(box) >= .65]
        strong_labels = [part for part in labels if isinstance(part.get('confidence',part.get('score')),(int,float))
                         and part.get('confidence',part.get('score')) >= .85]
        excluded = reading.get('kind') in UI_KINDS or any(_intersection(box,_box(part))/_area(box)>=.65 for part in ui)
        inside = [region for region in regions if _intersection(box,region['table_box'])/_area(box)>=.65]
        kind = labels[0]['kind'] if len({part['kind'] for part in labels})==1 else reading.get('kind','text')
        if excluded and not strong_labels:
            decision.update(disposition='excluded_ui',reason='observed_ui_geometry',classification_basis=reading.get('classification_basis'))
            decisions.append(decision);continue
        if inside and not strong_labels:
            decision.update(disposition='table_content',reason='inside_observed_table',region_ids=[region['id'] for region in inside])
            decisions.append(decision);continue
        if inside and strong_labels and represented(value,inside):
            decision.update(disposition='table_content',reason='business_label_already_in_owned_table',region_ids=[region['id'] for region in inside])
            decisions.append(decision);continue
        nearby = []
        # A title may be read in short fragments. Its observed semantic block,
        # rather than a fragment's left edge, determines whether it is shared by
        # two tables. The exported reading keeps its own original text box.
        ownership_box = _box(labels[0]) if len(labels)==1 else box
        for region in regions:
            tb=region['table_box'];tw,th=tb[2]-tb[0],tb[3]-tb[1]
            horizontal=max(0.,min(ownership_box[2],tb[2])-max(ownership_box[0],tb[0]))/min(tw,ownership_box[2]-ownership_box[0])
            gap=max(tb[1]-ownership_box[3],ownership_box[1]-tb[3],0.)
            max_gap=max(30,min((height or tb[3])*.08,th*.22))
            tagged=any(isinstance(part,dict) and _box(part) and part.get('kind') in semantic_kinds
                       and _intersection(box,_box(part))/_area(box)>=.65 for part in region.get('ancillary',[]))
            chain_box=reading.get('context_table_box')
            chain_owner=(reading.get('classification_basis')=='observed_title_unit_table_chain'
                and isinstance(chain_box,list) and _box({'box':chain_box})
                and _intersection(chain_box,tb)/max(_area(chain_box),_area(tb))>=.85)
            if (horizontal>.45 and gap<=max_gap) or tagged or region in inside or chain_owner:
                nearby.append(region)
        if not nearby and kind not in semantic_kinds:
            decision.update(disposition='evidence_only',reason='outside_business_context',review_required=False)
            decisions.append(decision);continue
        owner=nearby[0] if len(nearby)==1 else None
        table,basis=table_owner(owner) if owner else (None,'ambiguous_or_unassigned_region')
        ambiguous=excluded or not owner or len(labels)>1 and len({part['kind'] for part in labels})>1
        if kind not in semantic_kinds:
            # A unit label is only classified after it has an independent spatial
            # relation to a business table; numbers inside a grid are untouched.
            if re.match(r'^\s*(?:单位\s*[:：]|unit(?:s)?\s*[:：])',value,re.I) and owner and not inside:
                kind='unit'
            else:kind='text'
        # Adjacency establishes where a reading came from, not whether an
        # unclassified logo/header really is a business note.
        ambiguous = ambiguous or kind=='text' or reading.get('semantic_role_review_required') is True
        entry={'kind':kind,'text':value,'box':box,'source':reading.get('source'),
               'confidence':reading.get('confidence',reading.get('score')),'region_id':owner['id'] if owner else None,
               'region_ids':[region['id'] for region in nearby],'table':table,'ownership_basis':basis,
               'review_required':bool(ambiguous),'human_confirmed':False,'evidence_refs':[reference],
               'context_box':ownership_box}
        for key in ('classification_basis','context_table_box','semantic_role_review_required','business_original_kind'):
            if key in reading:entry[key]=deepcopy(reading[key])
        # Only the same literal at the same observed location is a duplicate
        # observation. An identical title elsewhere remains a separate item.
        existing=next((index for index,part in enumerate(items) if part['text']==value and part.get('box')
                       and _intersection(box,part['box'])/max(_area(box),_area(part['box']))>=.85),None)
        if existing is not None:
            items[existing]['evidence_refs'].append(reference)
            if items[existing]['kind']=='text' and kind in semantic_kinds:items[existing]['kind']=kind
            decision.update(disposition='duplicate_observation',reason='same_literal_same_location',item_index=existing)
        else:
            items.append(entry)
            decision.update(disposition='retained_context',reason='conflicting_ui_and_business_geometry' if excluded else
                            'independent_business_label' if kind in semantic_kinds else 'nearby_independent_context',item_index=len(items)-1)
        decisions.append(decision)
    counts={kind:sum(row['disposition']==kind for row in decisions)
            for kind in ('retained_context','table_content','excluded_ui','duplicate_observation','evidence_only')}
    counts.update(items=len(items),review_required=sum(bool(item.get('review_required')) for item in items),observations=len(decisions))
    return {'policy_version':'screen-business-context-v2','items':items,'decisions':decisions,'counts':counts,
            'auxiliary_evidence':original,'reconciled_evidence':reconciled,'human_review':'not_performed',
            'values_modified':False}


def _sequential_rulers(layout, texts, *, screen_confirmed=False):
    """Classify only ordered ruler tokens outside every detected business table.

    A business number column inside a detected table is never classified as UI.
    A single '1', 'A', or toolbar-like word is insufficient evidence.
    """
    tables = [item["box"] for item in layout if item.get("kind")=="table" and _box(item)]
    if not tables:
        return []
    protected = [_box(item) for item in layout if item.get('kind') in {'title','caption','unit','note'}
                 and isinstance(item.get('confidence',item.get('score')), (int,float))
                 and item.get('confidence',item.get('score')) >= .85 and _box(item)]
    candidates = [item for item in texts if item.get('kind') not in UI_KINDS and not any(_intersection(item["box"],box)>0 for box in tables+protected)
                  and re.fullmatch(r"\d{1,5}|[A-Z]{1,2}",str(item.get("text","")).strip())]
    groups = []
    for axis in (0,1):
        ordered = sorted(candidates,key=lambda item:((item["box"][axis]+item["box"][axis+2])/2,item["box"][1-axis]))
        bands = []
        for item in ordered:
            other = 1-axis
            center = (item["box"][other]+item["box"][other+2])/2
            band = next((b for b in bands if abs(b["center"]-center) <= max(5,(item["box"][other+2]-item["box"][other])*.55)),None)
            if band is None:
                band = {"center":center,"items":[]}
                bands.append(band)
            band["items"].append(item)
        for band in bands:
            items = sorted(band["items"],key=lambda item:item["box"][axis])
            if len(items) < 4:
                continue
            values = [str(item["text"]).strip() for item in items]
            if all(value.isdecimal() for value in values):
                numbers = [int(value) for value in values]
            elif all(re.fullmatch(r"[A-Z]{1,2}",value) for value in values):
                numbers = [sum((ord(ch)-64)*26**n for n,ch in enumerate(reversed(value))) for value in values]
            else:
                continue
            steps=[b-a for a,b in zip(numbers,numbers[1:])]
            if any(step not in (1,2) for step in steps):
                continue
            centers = [(i["box"][axis]+i["box"][axis+2])/2 for i in items]
            spacing = [(b-a)/step for a,b,step in zip(centers,centers[1:],steps)]
            if min(spacing) <= 0 or max(spacing)/min(spacing) > 1.35:
                continue
            box = [min(i["box"][0] for i in items),min(i["box"][1] for i in items),max(i["box"][2] for i in items),max(i["box"][3] for i in items)]
            # Ordered tokens must border a real table, rather than an arbitrary
            # list elsewhere in the document. This is a geometric association.
            # A photographed sheet can have blank rows between its column ruler
            # and the business table. Use observed glyph height as well as the
            # confirmed application chrome; a lone ordered business list still
            # cannot establish chrome or cross a protected caption.
            reach=max(100,(box[3]-box[1])*8) if screen_confirmed and axis==0 else max(35,(box[3]-box[1])*3)
            nearby = any((axis==1 and box[2] <= tb[0] and tb[0]-box[2] < max(35,(box[2]-box[0])*3)
                          and min(box[3],tb[3])-max(box[1],tb[1]) > (box[3]-box[1])*.7)
                         or (axis==0 and box[3] <= tb[1] and tb[1]-box[3] < reach
                             and min(box[2],tb[2])-max(box[0],tb[0]) > (box[2]-box[0])*.7) for tb in tables)
            if nearby:
                groups.append({"kind":"ruler","box":box,"polygon":box_polygon(box),"source":"ordered_tokens_outside_table",
                               "confidence":None,"orientation":"horizontal" if axis==0 else "vertical",
                               "inference_basis":"sequence_alignment_spacing_and_table_adjacency"})
                for item in items:
                    item["kind"] = "ruler"
                    item['orientation']='horizontal' if axis==0 else 'vertical'
                    item["classification_basis"] = "sequence_alignment_spacing_and_table_adjacency"
    return groups


def normalize_screen_context(context, *, width=None, height=None, candidate=None):
    """Normalize actual context-model output, optionally mapping from a crop.

    Dimensions refer to original image coordinates when ``candidate`` supplied.
    Coordinates outside the image and missing boxes are rejected explicitly.
    Only ordered external ruler evidence is inferred as UI; other text remains.
    """
    if not isinstance(context,dict) or context.get("status") != "success":
        raise ValueError("上下文模型没有成功输出")
    image = context.get("image",{})
    width,height = width or image.get("width"),height or image.get("height")
    if type(width) not in (int,float) or type(height) not in (int,float) or min(width,height) <= 0:
        raise ValueError("缺少当前原图尺寸")
    transform = candidate.get("prepared_to_source") if candidate else IDENTITY
    if not transform:
        raise ValueError("上下文缺少可逆原图坐标映射")
    raw_layout = context.get("layout_blocks",[]) or [dict(item,kind="table") for item in context.get("regions",[])]
    raw_text = context.get("ocr_blocks",context.get("blocks",[]))
    if len(raw_layout) > 1000 or len(raw_text) > 10000:
        raise ValueError("上下文证据超过单图上限")
    layout,text,warnings = [],[],[]
    for kind,items,target in (("layout",raw_layout,layout),("text",raw_text,text)):
        for number,item in enumerate(items):
            if not isinstance(item,dict):
                warnings.append({"code":"invalid_context_item","collection":kind,"index":number})
                continue
            mapped = _mapped(item,transform,width,height)
            if not mapped:
                warnings.append({"code":"invalid_or_outside_context_box","collection":kind,"index":number})
                continue
            label = str(item.get("label",item.get("kind","text")))
            mapped["kind"] = LABELS.get(label,"text")
            mapped["original_label"] = label
            mapped["source"] = item.get("source", "PP-DocLayoutV3" if kind=="layout" else context.get("ocr_source","PP-OCRv6-auxiliary"))
            mapped["confidence"] = item.get("confidence",item.get("score"))
            if kind == "text":
                mapped["text"] = str(item.get("text",""))
            target.append(mapped)
    layout,text,screen_confirmed=classify_screen_ui(layout,text,width,height)
    text=_anchored_business_titles(layout,text)
    layout.extend(_sequential_rulers(layout,text,screen_confirmed=screen_confirmed))
    return {"policy_version":POLICY_VERSION,"layout":layout,"text":text,"warnings":warnings,
            "provenance":{"component":context.get("component"),"ocr_source":context.get("ocr_source"),
                          "image_version":context.get("image_version"),"source_candidate_id":candidate.get("id") if candidate else "original",
                          "inference_seconds":context.get("inference_seconds"),"load_seconds":context.get("load_seconds"),
                          "contains_reference_input":False}}


def collect_screen_context(input_path, output_dir, adapter, *, image_version=None, prepare=True):
    """Run one image using an already loaded, caller-owned context adapter.

    The existing GPU lease/timeout/cleanup remains with EngineAdapter. Reuse the
    same adapter for a whole group; unload before another GPU model is started.
    """
    from PIL import Image
    from ocr_workbench.image_utils import normalized_rgb
    from ocr_workbench.screen_imaging import prepare_screen_image
    input_path,output_dir = Path(input_path).resolve(),Path(output_dir).resolve()
    if output_dir.drive.upper() != "F:":
        raise ValueError("屏拍上下文产物必须保存于 F 盘")
    if getattr(adapter,"engine",None) != "geometry" or getattr(adapter,"geometry_provider",None) != "context":
        raise ValueError("请提供已配置 context 的几何模型适配器")
    output_dir.mkdir(parents=True,exist_ok=True)
    normalized_path = output_dir/"context-input.png"
    with Image.open(input_path) as image:
        normalized_rgb(image).save(normalized_path,format="PNG")
    context = adapter.recognize(normalized_path,output_dir,geometry_request={"include_all_layout":True,"image_version":image_version})
    evidence = normalize_screen_context(context)
    (output_dir/"screen-context-evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    preparation = prepare_screen_image(input_path,output_dir/"prepared",evidence) if prepare else None
    return {"context":context,"evidence":evidence,"preparation":preparation}


def merge_local_text_evidence(tile_results, *, width, height, max_tokens=2000):
    """Map overlapping tile text back; never concatenate tile rows into tables.

    Identical text in substantially overlapping boxes is the same observation.
    Similar/repeated text in distinct boxes remains distinct, and conflicting
    overlapping readings are preserved for review rather than majority voted.
    """
    if not isinstance(tile_results,list) or len(tile_results) > 16 or not 1 <= max_tokens <= 10000:
        raise ValueError("局部证据预算无效")
    blocks,conflicts,warnings = [],[],[]
    for result in tile_results:
        candidate = result.get("candidate")
        if not candidate or candidate.get("kind") != "tile":
            raise ValueError("局部证据必须带实际分块及坐标映射")
        normalized = normalize_screen_context(result["context"],width=width,height=height,candidate=candidate)
        warnings.extend(normalized["warnings"])
        for block in normalized["text"]:
            overlaps = [old for old in blocks if _intersection(old["box"],block["box"])/min(_area(old["box"]),_area(block["box"])) > .8]
            identical = next((old for old in overlaps if old["text"] == block["text"]),None)
            if identical:
                identical.setdefault("tile_sources",[]).append(candidate["id"])
                continue
            if len(blocks) >= max_tokens:
                warnings.append({"code":"local_token_budget","message":"局部文字证据达到上限；保留整表结果，不声明分块已覆盖全表。"})
                return {"blocks":blocks,"conflicts":conflicts,"warnings":warnings,"can_replace_whole_table":False,"complete":False}
            block["tile_sources"] = [candidate["id"]]
            for old in overlaps:
                if old["text"] != block["text"]:
                    conflicts.append({"code":"overlap_text_conflict","before":old["text"],"alternative":block["text"],
                                      "box":block["box"],"tile_ids":old.get("tile_sources",[])+[candidate["id"]],"resolution":"pending_review"})
            blocks.append(block)
    return {"blocks":blocks,"conflicts":conflicts,"warnings":warnings,"can_replace_whole_table":False,"complete":not warnings}


def build_screen_visual_snapshot(raw, image_info, issues, input_path, *, max_targets=16):
    """Build bounded independent-reading targets with reliable local geometry.

    The model sees an empty ``before`` field and original local pixels, so an OCR
    candidate cannot steer its transcription. Actual current values stay in the
    source_targets audit map, which ReviewPipeline does not send to the model.
    """
    if not 1 <= max_targets <= 32:
        raise ValueError("视觉核对预算无效")
    path = Path(input_path)
    width,height = image_info["width"],image_info["height"]
    from ocr_workbench.screen_diagnostics import (reliable_cell_box,observe_screen_grid_cells,localized_grid_box)
    info={**image_info,'input_path':str(path)}
    localization=observe_screen_grid_cells(raw,info)
    targets,source_targets,skipped = [],[],[]
    used = set()
    for issue in issues:
        key = (issue.get("table"),issue.get("row"),issue.get("column"))
        if key in used or any(type(v) is not int for v in key):
            continue
        used.add(key)
        ti,row,column = key
        tables = raw.get("tables",[])
        if not 0 <= ti < len(tables):
            continue
        matches=[c for c in tables[ti].get("cells",[]) if isinstance(c,dict) and c.get("row")==row and c.get("column")==column]
        cell=matches[0] if len(matches)==1 else None
        native_box=reliable_cell_box(cell,info)
        box=native_box or localized_grid_box(localization,ti,cell)
        if not cell or not box or box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height:
            skipped.append({"target":key,"reason":"no_reliable_local_cell_geometry"})
            continue
        if len(targets) == max_targets:
            skipped.append({"target":key,"reason":"visual_target_budget"})
            continue
        ident = f"screen:{ti}:{row}:{column}"
        targets.append({"id":ident,"before":"","context":"逐字读取此单元格实际可见内容，保留负号、小数点、前导零；模糊或遮挡时返回未知，不推测。",
                        "evidence":{"level":"cell","polygon":cell.get("polygon") if native_box and cell.get("polygon") else box_polygon(box),"range_semantics":"full_cell"}})
        source_targets.append({"id":ident,"table":ti,"row":row,"column":column,"before":cell.get("text",""),"box":box,"issue_code":issue.get("code")})
    return {"image_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"targets":targets,"source_targets":source_targets,
            "skipped":skipped,"policy_version":POLICY_VERSION,"human_review":"not_performed"}


def review_screen_visual_proposals(session, input_path, snapshot):
    """Use an existing caller-owned Qwen review session; return suggestions only."""
    if not snapshot.get("targets"):
        return {"proposals":[],"skipped":snapshot.get("skipped",[]),"human_review":"not_performed"}
    response = session.review(input_path,snapshot)
    originals = {target["id"]:target for target in snapshot["source_targets"]}
    proposals = []
    for item in response.get("items",[]):
        original = originals[item["target_id"]]
        reading = None if item["decision"] == "uncertain" else item["after"]
        decision = "uncertain" if reading is None else "keep" if reading == original["before"] else "replace"
        proposals.append({**original,"reading":reading,"decision":decision,"reason":item.get("reason"),
                          "automatic_apply":False,"human_review":"not_performed","source":"local_visual_independent_reading"})
    return {"proposals":proposals,"skipped":snapshot.get("skipped",[]),"response":response,"human_review":"not_performed"}
