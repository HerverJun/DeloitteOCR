"""Two-stage token/cell and adopted/predicted correspondence, without GT access.

Unique text anchors establish local, monotone row/column constraints. Repeated
and empty values require independent row AND column anchors. Gaps are never
converted to guessed coordinates, and conflicting proposals are rejected
simultaneously so iteration order cannot settle an ambiguous assignment.
"""
from collections import Counter, defaultdict
from itertools import combinations
import json
import hashlib
from pathlib import Path
import time

from ocr_workbench.coordinates import bounds, box_polygon
from ocr_workbench.geometry_contract import (ALGORITHM_VERSION, CellCorrespondence, TokenAssignment,
    adopted_cells, as_json, evidence_v2, fingerprint, intersection_area, matching_text,
    signed_area, tokens_from_blocks)
from ocr_workbench.geometry_diagnostics import diagnostic
from ocr_workbench.geometry_providers import adapt_prediction


def default_policy():
    path=Path(__file__).resolve().parents[2]/'config/geometry-matching-policy.json'
    return json.loads(path.read_text('utf-8'))


def policy_for_algorithm(algorithm=None):
    policy = default_policy()
    algorithm = algorithm or policy['algorithm']
    if algorithm in ('local-v3','local-v4'):
        path = Path(__file__).resolve().parents[2] / ('config/geometry-matching-policy-'+algorithm.split('-')[1]+'.json')
        return json.loads(path.read_text('utf-8'))
    if algorithm not in ('local-v2', 'legacy'):
        raise ValueError('未知定位算法')
    return {**policy, 'algorithm': algorithm}


def matching_code_fingerprint():
    base=Path(__file__).resolve().parent
    paths=[base/name for name in ('table_matching.py','table_anchor_refinement.py','spatial_candidates.py','geometry_contract.py','geometry_providers.py','geometry_diagnostics.py','coordinates.py','tables.py')]
    paths += [base/'_vendor/tableformer'/name for name in ('tf_cell_matcher.py','otsl.py','settings.py')]
    return fingerprint({str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def policy_identity(policy):
    legacy=policy.get('algorithm')=='legacy'
    return {'algorithm_version':'legacy-v1' if legacy else policy['algorithm'] if policy.get('algorithm') in ('local-v3','local-v4') else ALGORITHM_VERSION,
        'algorithm_sha256':hashlib.sha256(Path(__file__).with_name('geometry.py').read_bytes()).hexdigest() if legacy else matching_code_fingerprint(),
        'policy_version':policy['policy_version'],'policy_sha256':fingerprint(policy)}


def assign_tokens(tokens,cells,policy,*,deadline=None):
    """Upstream bbox candidate generation plus local polygon/exclusivity audit."""
    from ocr_workbench._vendor.tableformer.tf_cell_matcher import CellMatcher
    ordered_cells=sorted([c for c in cells if c.cell_polygon],key=lambda c:c.id)
    ordered_tokens=sorted(tokens,key=lambda t:t.id)
    indexed = policy.get('spatial_index', False)
    if not indexed and len(ordered_cells)*len(ordered_tokens)>policy['maximum_token_cell_pairs']:
        return [],{},'candidate_budget_exceeded'
    if not ordered_tokens or not ordered_cells:return [],{},None
    if indexed:
        from ocr_workbench.spatial_candidates import BoxIndex, SpatialBudget
        try:index=BoxIndex(ordered_cells,policy['maximum_token_cell_pairs'],deadline)
        except SpatialBudget as error:return [],{},str(error)
        candidates={}
    else:
        matcher=CellMatcher({'predict':{'pdf_cell_iou_thres':0}})
        candidates,_=matcher._intersection_over_pdf_match(
            [{'cell_id':c.id,'bbox':bounds(c.cell_polygon)} for c in ordered_cells],
            [{'id':t.id,'bbox':bounds(t.polygon)} for t in ordered_tokens])
    by_id={c.id:c for c in ordered_cells};assigned=defaultdict(list);records=[]
    examined=0
    for token in ordered_tokens:
        if deadline and time.perf_counter()>deadline:return records,dict(assigned),'timeout'
        if indexed:
            box=bounds(token.polygon)
            choices=index.query(box)
            examined+=len(choices)
            if examined>policy['maximum_token_cell_pairs']:
                return records,dict(assigned),'candidate_budget_exceeded'
            candidates[token.id]=[{'table_cell_id':cell.id,'iopdf':
                max(0,min(box[2],index.boxes[cell.id][2])-max(box[0],index.boxes[cell.id][0]))*
                max(0,min(box[3],index.boxes[cell.id][3])-max(box[1],index.boxes[cell.id][1]))/
                ((box[2]-box[0])*(box[3]-box[1]))} for cell in choices]
        options=[]
        for candidate in candidates.get(token.id,[]):
            cell=by_id[candidate['table_cell_id']]
            overlap=intersection_area(token.polygon,cell.cell_polygon)/abs(signed_area(token.polygon))
            if overlap>0:options.append({'cell_id':cell.id,'token_overlap':overlap,'upstream_bbox_overlap':float(candidate['iopdf'])})
        if deadline and time.perf_counter()>deadline:return records,dict(assigned),'timeout'
        options.sort(key=lambda v:(-v['token_overlap'],v['cell_id']))
        chosen=None;conflicts=[]
        first=options[0]['token_overlap'] if options else 0
        second=options[1]['token_overlap'] if len(options)>1 else 0
        if first>=policy['token_minimum_containment'] and first-second>=policy['token_minimum_margin'] and second<=policy['token_maximum_other_overlap']:
            chosen=options[0]['cell_id'];assigned[chosen].append(token)
        elif len(options)>1:conflicts=['cross_cell_token']
        elif options:conflicts=['token_assignment_ambiguous']
        records.append(TokenAssignment(token.id,options,chosen,conflicts,token.source_result,token.id))
    for group in assigned.values():group.sort(key=lambda t:(bounds(t.polygon)[1],bounds(t.polygon)[0],t.id))
    return records,dict(assigned),None


def _value(cell,assigned):
    return matching_text(''.join(t.raw_text for t in assigned.get(cell.id,[])))


def _span(cell):return (cell.row_end-cell.row_start,cell.column_end-cell.column_start)


def _relation(a0,a1,b0,b1):
    if a0==b0 and a1==b1:return 'equal'
    if a1<=b0:return 'before'
    if b1<=a0:return 'after'
    return 'overlap'


def _order_compatible(a,p,b,q):
    for axis in ('row','column'):
        left=_relation(getattr(a,axis+'_start'),getattr(a,axis+'_end'),getattr(b,axis+'_start'),getattr(b,axis+'_end'))
        right=_relation(getattr(p,axis+'_start'),getattr(p,axis+'_end'),getattr(q,axis+'_start'),getattr(q,axis+'_end'))
        if left!=right:return False
    return True


def _anchors(adopted,predicted,assigned,deadline=None):
    ac=Counter(c.matching_text for c in adopted if c.matching_text)
    pc=Counter(_value(c,assigned) for c in predicted if _value(c,assigned))
    by_text={_value(c,assigned):c for c in predicted if _value(c,assigned)}
    pairs=[(a,by_text[a.matching_text]) for a in adopted if a.matching_text and ac[a.matching_text]==pc[a.matching_text]==1
           and _span(a)==_span(by_text[a.matching_text])]
    # A common translation of both interval endpoints preserves every pairwise
    # ordering relation, including overlaps; this is a complete conflict proof.
    offsets={(p.row_start-a.row_start,p.column_start-a.column_start) for a,p in pairs}
    if len(offsets)<=1:return pairs,set()
    bad=set()
    for (a,p),(b,q) in combinations(pairs,2):
        if deadline is not None and time.perf_counter()>deadline:
            raise TimeoutError('anchor_conflict_check_timeout')
        if not _order_compatible(a,p,b,q):bad.update((a.id,b.id))
    return [(a,p) for a,p in pairs if a.id not in bad],bad


def _axis_anchors(a,p,anchors,axis):
    return [(b,q) for b,q in anchors if b.id!=a.id and
        getattr(a,axis+'_start')==getattr(b,axis+'_start') and getattr(a,axis+'_end')==getattr(b,axis+'_end') and
        getattr(p,axis+'_start')==getattr(q,axis+'_start') and getattr(p,axis+'_end')==getattr(q,axis+'_end')]


def _local_support(a,p,anchors):
    row=_axis_anchors(a,p,anchors,'row');column=_axis_anchors(a,p,anchors,'column')
    independent=any(b.id!=c.id for b,_ in row for c,_ in column)
    if not independent:return []
    if any(not _order_compatible(a,p,b,q) for b,q in anchors):return []
    return sorted({b.id for b,_ in row+column})


def _group_cells(a,predicted,assigned,anchors,policy,deadline):
    """Enumerate bounded rectangular logical groups, not arbitrary subsets."""
    if _span(a)==(1,1):return [],None
    rs,cs=_span(a);lookup={(p.row_start,p.column_start):p for p in predicted};groups=[];attempts=0
    for start in sorted(predicted,key=lambda p:(p.row_start,p.column_start,p.id)):
        if time.perf_counter()>deadline:return groups,'timeout'
        r0,c0=start.row_start,start.column_start;r1,c1=r0+rs,c0+cs
        group=[p for p in predicted if r0<=p.row_start and p.row_end<=r1 and c0<=p.column_start and p.column_end<=c1]
        attempts+=len(group)
        if attempts>policy['maximum_group_candidates']:return groups,'candidate_budget_exceeded'
        if not 2<=len(group)<=policy['maximum_group_cells']:continue
        occupied=set();conflict=False
        for p in group:
            slots={(r,c) for r in range(p.row_start,p.row_end) for c in range(p.column_start,p.column_end)}
            conflict|=bool(occupied&slots);occupied|=slots
        if conflict or occupied!={(r,c) for r in range(r0,r1) for c in range(c0,c1)}:continue
        group.sort(key=lambda p:(p.row_start,p.column_start,p.id))
        if matching_text(''.join(_value(p,assigned) for p in group))!=a.matching_text or not a.matching_text:continue
        from dataclasses import replace
        virtual=replace(start,row_end=r1,column_end=c1)
        support=_local_support(a,virtual,anchors)
        # A unique concatenated value is additional content evidence, but a
        # merged group must also agree with at least one external row/col anchor.
        if not support:
            support=sorted({b.id for b,q in anchors if _order_compatible(a,virtual,b,q) and
                (b.row_start==a.row_start and q.row_start==r0 or b.column_start==a.column_start and q.column_start==c0)})
        if support and all(_order_compatible(a,virtual,b,q) for b,q in anchors):groups.append((group,support))
    return groups,None


def _complete_group_polygon(group):
    """A derived full cell is allowed only for a genuine rectangular union."""
    if any(not c.cell_polygon or c.reason_codes for c in group):return None
    boxes=[bounds(c.cell_polygon) for c in group]
    if any(abs(abs(signed_area(c.cell_polygon))-(b[2]-b[0])*(b[3]-b[1]))>1e-6 for c,b in zip(group,boxes)):return None
    extent=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
    area=sum((b[2]-b[0])*(b[3]-b[1]) for b in boxes)
    overlap=sum(intersection_area(a.cell_polygon,b.cell_polygon) for a,b in combinations(group,2))
    if overlap>1e-6 or abs(area-(extent[2]-extent[0])*(extent[3]-extent[1]))>1e-6:return None
    return box_polygon(extent)


def _fallback(target,table_polygon,reasons,**details):
    return evidence_v2(target=target,table_polygon=table_polygon,display_polygon=table_polygon,
        **diagnostic(*reasons),**details)


def local_mapping(edit,prediction,width,height,*,policy=None,result_id='snapshot',revision=0,image_version=None,ocr_blocks=None):
    policy=policy or default_policy();identity=policy_identity(policy)
    image_version=image_version or prediction.get('image_version') or 'legacy-artifact-image'
    input_hash=fingerprint({'edit':edit,'revision':revision,'image_version':image_version,
        'prediction':prediction,'ocr_blocks':ocr_blocks,'policy':policy})
    common={**identity,'input_sha256':input_hash,'anchor_snapshot':{'result_id':result_id,'revision':revision,'text_sha256':fingerprint(edit)},
            'image_version':image_version,'contributes_to_votes':False}
    wrong_version=bool(prediction.get('image_version') and prediction['image_version']!=image_version)
    if wrong_version:tables=[]
    else:tables=adapt_prediction(prediction,width,height)
    blocks=ocr_blocks if ocr_blocks is not None else prediction.get('ocr_blocks',[])
    tokens,rejected=tokens_from_blocks(blocks,source_result=prediction.get('ocr_source') or result_id,
        image_version=image_version,width=width,height=height,engine=prediction.get('component','external'))
    # Token membership in overlapping table regions must itself be exclusive.
    membership={t.id:[table['id'] for table in tables if table['polygon'] and intersection_area(t.polygon,table['polygon'])/abs(signed_area(t.polygon))>=.8] for t in tokens}
    prepared={}
    for table in tables:
        started=time.perf_counter();deadline=started+policy['table_budget_ms']/1000
        local=[t for t in tokens if membership[t.id]==[table['id']]]
        if len(table['cells']) > policy['maximum_cells']:
            records,assigned,error=[],{},'candidate_budget_exceeded'
        else:
            records,assigned,error=assign_tokens(local,table['cells'],policy,deadline=deadline)
        prepared[table['id']]={'tokens':local,'records':records,'assigned':assigned,'error':error,'started':started,
                             'token_ms':(time.perf_counter()-started)*1000}
    adopted_tables=[];rankings=[]
    for ti,table in enumerate(edit.get('tables',[])):
        try:adopted=adopted_cells(table,table_id=str(ti),result_id=result_id,revision=revision)
        except ValueError:adopted=[]
        adopted_tables.append(adopted);rank=[]
        ac=Counter(c.matching_text for c in adopted if c.matching_text)
        for candidate in tables:
            state=prepared[candidate['id']];pc=Counter(_value(c,state['assigned']) for c in candidate['cells'] if _value(c,state['assigned']))
            unique=sum(ac[v]==pc[v]==1 for v in ac)
            if unique<policy['table_minimum_unique_anchors']:continue
            if table.get('region_id') and table['region_id']!=candidate['region_id']:continue
            shared=sum((ac&pc).values());score=shared/max(1,min(sum(ac.values()),sum(pc.values())))
            rank.append((score,candidate))
        rank.sort(key=lambda x:(-x[0],x[1]['id']));rankings.append(rank)
    selected=[]
    for rank in rankings:
        selected.append(rank[0][1] if rank and (len(rank)==1 or rank[0][0]-rank[1][0]>=policy['table_minimum_margin']) else None)
    counts=Counter(t['id'] for t in selected if t)
    mappings=[]
    for ti,(table,adopted,chosen) in enumerate(zip(edit.get('tables',[]),adopted_tables,selected)):
        region=None;reason=None;state=None
        if wrong_version:reason='coordinate_version_mismatch'
        elif prediction.get('status')=='failed':reason='inference_failed'
        elif not tables:reason='no_table_candidates'
        elif not adopted:reason='span_conflict'
        elif chosen and counts[chosen['id']]>1:reason='prediction_table_reused';chosen=None
        elif not chosen:reason='no_text_tokens' if not tokens else 'table_identity_ambiguous'
        if chosen:
            region=chosen['polygon'];state=prepared[chosen['id']]
            if state['error']:reason=state['error']
        elif len(tables)==len(edit.get('tables',[]))==1:
            region=tables[0]['polygon']
            if prepared[tables[0]['id']]['error']:reason=prepared[tables[0]['id']]['error']
            if not tables[0]['cells']:reason=next(iter(tables[0]['reason_codes']),'no_cell_candidates')
        table_target={'kind':'table','table':ti}
        mappings.append(_fallback(table_target,region,[reason or 'accepted'],region_id=chosen['region_id'] if chosen else None,**common))
        if reason:
            for cell in table['cells']:
                mappings.append(_fallback({'kind':'cell','table':ti,'row':cell['row'],'column':cell['column']},region,[reason],**common))
            continue
        started=time.perf_counter();deadline=started+max(0,policy['table_budget_ms']-state['token_ms'])/1000
        predicted=chosen['cells'];assigned=state['assigned']
        refinement_error = None
        try:anchors,order_bad=_anchors(adopted,predicted,assigned,deadline)
        except TimeoutError:
            anchors,order_bad=[],set()
            refinement_error='timeout'
        anchor_lineage = {a.id: {'roots': [a.id], 'parents': [], 'round': 0} for a, _ in anchors}
        if policy.get('neighbor_anchors') and not refinement_error:
            from ocr_workbench.table_anchor_refinement import refine_anchors
            anchors, anchor_lineage, refinement_error = refine_anchors(
                adopted, predicted, assigned, anchors, order_bad, policy, deadline)
        exact_topology=sorted((c.row_start,c.row_end,c.column_start,c.column_end) for c in adopted)==sorted((c.row_start,c.row_end,c.column_start,c.column_end) for c in predicted)
        proposals={};errors={};by_anchor={a.id:p for a,p in anchors}
        predicted_by_value=defaultdict(list)
        for p in predicted:predicted_by_value[(_span(p),_value(p,assigned))].append(p)
        adopted_counts=Counter(a.matching_text for a in adopted if a.matching_text)
        for a in adopted:
            if refinement_error and not (policy.get('preserve_validated_anchors') and a.id in by_anchor):errors[a.id]=refinement_error;continue
            if policy.get('preserve_validated_anchors') and a.id in by_anchor:
                proposals[a.id]=([by_anchor[a.id]],[a.id],'one_to_one');continue
            if time.perf_counter()>deadline:errors[a.id]='timeout';continue
            if len(adopted)>policy['maximum_cells']:errors[a.id]='candidate_budget_exceeded';continue
            if not policy['local_correspondence'] and not exact_topology:errors[a.id]='topology_mismatch';continue
            if a.id in order_bad:errors[a.id]='order_conflict';continue
            if a.id in by_anchor:proposals[a.id]=([by_anchor[a.id]],[a.id],'one_to_one');continue
            candidates=[]
            if policy['repeated_empty_cells']:
                for p in predicted_by_value[(_span(a),a.matching_text)]:
                    if _span(a)==_span(p) and a.matching_text==_value(p,assigned):
                        support=_local_support(a,p,anchors)
                        if not support and policy.get('neighbor_anchors'):
                            from ocr_workbench.table_anchor_refinement import neighbor_support
                            support = neighbor_support(a, p, anchors)
                        if support:candidates.append(([p],support,'one_to_one'))
            if policy['merged_cells']:
                groups,budget=_group_cells(a,predicted,assigned,anchors,policy,deadline)
                candidates += [(group,support,'one_to_many') for group,support in groups]
                if budget:errors[a.id]=budget;continue
            if len(candidates)==1:proposals[a.id]=candidates[0]
            elif len(candidates)>1:errors[a.id]='candidate_tie'
            else:errors[a.id]='empty_cell_without_anchors' if not a.matching_text else 'repeated_value_ambiguous' if adopted_counts[a.matching_text]>1 else 'no_local_anchors'
        owners=defaultdict(list)
        for aid,(group,_,_) in proposals.items():
            for p in group:owners[p.id].append(aid)
        collision={aid for ids in owners.values() if len(ids)>1 for aid in ids}
        # All unique token previews are also one-to-one; no string splitting.
        token_values=Counter(t.matching_text for t in state['tokens'])
        records_by_cell=defaultdict(list)
        for r in state['records']:records_by_cell[r.adopted_cell_id].append(r)
        anchor_by_id={a.id:(a,p) for a,p in anchors}
        for a in adopted:
            target={'kind':'cell','table':ti,'row':a.row_start,'column':a.column_start}
            content=[];cell_polys=[];origin=None;display=region;correspondence=None;reason=errors.get(a.id)
            if a.id in collision:reason='prediction_cell_reused'
            elif a.id in proposals:
                group,support,relation=proposals[a.id]
                owned=[t for p in group for t in assigned.get(p.id,[])]
                content=[t.polygon for t in owned];origin=group[0].geometry_origin
                if len(group)==1 and group[0].cell_polygon and not group[0].reason_codes:cell_polys=[group[0].cell_polygon];display=cell_polys[0]
                elif len(group)>1:
                    union=_complete_group_polygon(group)
                    if union:cell_polys=[p.cell_polygon for p in group];display=union;origin='derived_from_cells'
                    else:reason='noncontiguous_group'
                else:reason=next(iter(group[0].reason_codes),'invalid_polygon')
                correspondence=as_json(CellCorrespondence(a.id,[p.id for p in group],relation,
                    [{'adopted_cell_id':b.id,'predicted_cell_id':q.id,'token_ids':[t.id for t in assigned.get(q.id,[])],
                      'dependency':'independent_exact_token_anchor' if anchor_lineage[b.id]['round'] == 0 else 'resolved_neighbor_anchor',
                      'lineage':anchor_lineage[b.id]} for b,q in (anchor_by_id[i] for i in support)],
                    {'text_exact':True,'span_compatible':True,'local_order_consistent':True,'exclusive':True},1.,[reason] if reason else []))
            if not cell_polys:
                if not content and a.matching_text and adopted_counts[a.matching_text]==token_values[a.matching_text]==1 and reason not in ('order_conflict','prediction_cell_reused','timeout','candidate_budget_exceeded'):
                    content=[t.polygon for t in state['tokens'] if t.matching_text==a.matching_text]
                if content:
                    display=box_polygon([min(bounds(p)[0] for p in content),min(bounds(p)[1] for p in content),max(bounds(p)[2] for p in content),max(bounds(p)[3] for p in content)])
                    origin='text_extent'
                reason=reason or 'text_extent_only'
            mapping=evidence_v2(target=target,table_polygon=region,region_id=chosen['region_id'],content_polygons=content,
                cell_polygons=cell_polys,display_polygon=display,origin=origin,**diagnostic('accepted' if cell_polys else reason),
                correspondence=correspondence,provider=chosen['provider'],prediction_table=chosen['id'],
                predicted_cells=[as_json(p) for p in proposals[a.id][0]] if a.id in proposals else [],
                token_assignments=[as_json(r) for p in proposals[a.id][0] for r in records_by_cell[p.id]] if a.id in proposals else [],
                provider_reason_codes=chosen['reason_codes'],rejected_tokens=rejected,**common)
            mappings.append(mapping)
        elapsed=(time.perf_counter()-started)*1000
        for m in mappings:
            if m['target'].get('table')==ti:m['timing_ms']={'token_matching':state['token_ms'],'local_correspondence':elapsed}
    return mappings
