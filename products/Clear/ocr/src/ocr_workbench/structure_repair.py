"""Opt-in research patch contract. No production routing and no inferred geometry.

Adopted cells are indivisible content fragments unless a prior operation saved
their actual constituent fragments. Merely matching a value never locates it.
"""
from collections import Counter
from copy import deepcopy

from ocr_workbench.complex_table_contract import validate_grid
from ocr_workbench.geometry_contract import fingerprint, polygon_iou, intersection_area, signed_area
from ocr_workbench.structure_diagnostics import span, differences

VERSION = 'adopted-content-patch-v1'
MAX_AFFECTED = 8
MAX_PATCHES = 6


def content_baseline(table, *, result_id, revision, image_version, image_sha256):
    """The namespace identifies content in one revision, never a spatial box."""
    namespace = fingerprint({'result':result_id,'revision':revision,'table':table,
                             'image_version':image_version,'image_sha256':image_sha256})
    cells = []
    for i,cell in enumerate(table['cells']):
        cells.append({'id':namespace+':'+str(i),'literal':cell['text'],'slot':list(span(cell)),
                      'source':deepcopy(cell.get('structure_source',{})),
                      'manual_state':deepcopy(cell.get('manual_state','unknown'))})
    return {'version':VERSION,'namespace':namespace,'result_id':result_id,'revision':revision,
            'image_version':image_version,'image_sha256':image_sha256,
            'table':deepcopy(table),'contents':cells,'table_sha256':fingerprint(table)}


def _source_ids(cell, image_version):
    source = cell.get('structure_source',{})
    # Short token IDs from another source/revision do not establish identity.
    if not source.get('text_source_result') or source.get('image_version') != image_version:
        return []
    return [(source['text_source_result'],source['image_version'],i) for i in source.get('token_ids',[])]


def _inside(inner, outer):
    return bool(inner and outer and abs(signed_area(inner)) and
                intersection_area(inner,outer)/abs(signed_area(inner)) >= .995)


def bind_adopted(baseline, skeleton, *, method='source', manual_bindings=()):
    """Three cumulative, frozen evidence paths; return conflicts instead of guessing.

    Only one-to-one destinations are supported in this first bounded prototype.
    Unsupported merges/splits are deliberately left to explicit fragment work.
    """
    if method not in ('source','geometry','adjacency'):raise ValueError('unknown method')
    output=deepcopy(skeleton)
    conflicts=[];moves=[];owners={}
    try:validate_grid(output)
    except ValueError as error:
        return output,[{'kind':'invalid_grid','reason':str(error)}],moves
    old=baseline['table']
    stable={(span(c),c['text']) for c in old['cells']} == {(span(c),c['text']) for c in output['cells']}
    for content,cell in zip(baseline['contents'],old['cells']):
        ids=_source_ids(cell,baseline['image_version'])
        targets=[];reason=None
        if ids and len(ids)==len(set(ids)):
            targets=[i for i,c in enumerate(output['cells']) if _source_ids(c,baseline['image_version'])==ids]
            if targets:reason='exclusive_source_identity'
        if method in ('geometry','adjacency'):
            bindings=[b for b in manual_bindings if b.get('content_id')==content['id'] and
                      b.get('image_version')==baseline['image_version'] and b.get('revision')==baseline['revision']]
            source=cell.get('structure_source',{})
            poly=bindings[0].get('polygon') if len(bindings)==1 else (
                source.get('cell_polygon') if source.get('range_semantics')=='full_cell' and
                source.get('image_version')==baseline['image_version'] else None)
            if poly:
                located=[i for i,c in enumerate(output['cells']) if c.get('structure_source',{}).get('image_version')==baseline['image_version']
                         and c.get('structure_source',{}).get('range_semantics')=='full_cell'
                         and polygon_iou(poly,c['structure_source'].get('cell_polygon'))>=.98]
                if targets and located!=targets:
                    conflicts.append({'kind':'evidence_disagrees','content_id':content['id']});continue
                if not targets:
                    targets=located;reason='manual_binding' if bindings else 'independent_full_cell_geometry'
        # Unchanged slots are not a license to move text. This path only allows
        # header metadata changes on an otherwise exact adopted layout.
        if method=='adjacency' and not targets and stable:
            targets=[i for i,c in enumerate(output['cells']) if span(c)==span(cell) and c['text']==cell['text']]
            reason='unchanged_slot_and_literal'
        if len(targets)!=1 or targets[0] in owners:
            conflicts.append({'kind':'content_identity_unproved','content_id':content['id'],'slot':content['slot']});continue
        i=targets[0];owners[i]=content['id'];target=output['cells'][i]
        target['text']=content['literal']
        target['content_fragments']=[deepcopy(content)]
        target['content_separator']=''
        # Keep the adopted state, even if it is a deliberate manual clear.
        target['manual_state']=deepcopy(content['manual_state'])
        moves.append({'content_id':content['id'],'from':content['slot'],'to':list(span(target)),'evidence':reason})
    for i,cell in enumerate(output['cells']):
        if i not in owners:
            conflicts.append({'kind':'unowned_destination','slot':list(span(cell)),
                              'literal':cell['text'],'empty_is_unknown':not bool(cell['text'])})
    return output,conflicts,moves


def validate_conservation(baseline, table):
    validate_grid(table)
    expected={c['id']:c for c in baseline['contents']}
    fragments=[f for c in table['cells'] for f in c.get('content_fragments',[])]
    counts=Counter(f.get('id') for f in fragments)
    if counts != Counter(expected.keys()):raise ValueError('content identity omitted, added or duplicated')
    for cell in table['cells']:
        parts=cell.get('content_fragments',[])
        if not parts:raise ValueError('unknown destination, including unknown blank')
        for f in parts:
            if f!=expected[f['id']]:raise ValueError('adopted literal, source or manual state changed')
        if cell.get('content_separator','').join(f['literal'] for f in parts)!=cell['text']:
            raise ValueError('display differs from preserved literal fragments')
        if len(parts)==1 and cell.get('manual_state','unknown')!=parts[0]['manual_state']:
            raise ValueError('manual state changed')
    return True


def make_patch(baseline, table, moves, *, independent_region=None, local=True, manual_bindings=()):
    validate_conservation(baseline,table)
    rebound,conflicts,verified_moves=bind_adopted(baseline,table,method='adjacency',manual_bindings=manual_bindings)
    if conflicts or moves!=verified_moves or rebound!=table:
        raise ValueError('patch correspondence evidence changed or unproved')
    old=baseline['table']
    old_cells={c['id']:cell for c,cell in zip(baseline['contents'],old['cells'])}
    changed=[]
    for cell in table['cells']:
        for part in cell['content_fragments']:
            prior=old_cells[part['id']]
            if (span(prior),prior.get('is_header'),prior.get('header_role')) != (span(cell),cell.get('is_header'),cell.get('header_role')):
                changed.append(part['id'])
    if not changed:raise ValueError('no structural change')
    if local and len(changed)>MAX_AFFECTED:raise ValueError('patch impact budget exceeded')
    # Larger row/column extent without an independent region is a boundary gap.
    expanding=table['rows']>old['rows'] or table['columns']>old['columns']
    if expanding and independent_region is None:raise ValueError('independent table boundary missing')
    if independent_region is not None:
        if independent_region.get('image_version')!=baseline['image_version'] or independent_region.get('source') not in ('pdf-vector','manual','independent-detector'):
            raise ValueError('unverified table boundary')
        for cell in table['cells']:
            if not _inside(cell.get('structure_source',{}).get('cell_polygon'),independent_region['polygon']):
                raise ValueError('cell outside independently supported table region')
    return {'version':VERSION,'baseline_sha256':fingerprint(baseline),'revision':baseline['revision'],
        'image_version':baseline['image_version'],'image_sha256':baseline['image_sha256'],
        'before':deepcopy(old),'after':deepcopy(table),'affected_content_ids':changed,
        'slot_transforms':deepcopy(moves),'differences':differences(old,table),
        'boundary':deepcopy(independent_region),'manual_bindings':deepcopy(list(manual_bindings)),
        'local':local,'automatic_adoption':False}


def apply_patch(baseline, patch, *, revision, image_version, image_sha256):
    if (patch['baseline_sha256']!=fingerprint(baseline) or revision!=patch['revision'] or
        image_version!=patch['image_version'] or image_sha256!=patch['image_sha256']):
        raise ValueError('stale content revision or image')
    # Recompute all checks, including the whole grid and impact, at acceptance.
    checked=make_patch(baseline,patch['after'],patch['slot_transforms'],
                       independent_region=patch['boundary'],local=patch['local'],manual_bindings=patch['manual_bindings'])
    if checked!=patch:raise ValueError('patch evidence changed')
    return deepcopy(patch['after'])


def native_header_patches(baseline):
    """Bounded research proposals, never automatic header classification.

    Require recorded native adoption evidence. The three fixed alternatives
    change metadata only; a visual reviewer still decides whether it is a header.
    """
    table = baseline['table']
    for cell in table['cells']:
        record = cell.get('native_content', {})
        if (record.get('version') != 'native-adopted-fragments-v1' or
            record.get('image_version') != baseline['image_version'] or
            record.get('text_source_result') != cell.get('structure_source', {}).get('text_source_result')):
            return [], [{'kind': 'native_adoption_evidence_missing'}]
    selectors = [
        ('column-headings-row-1', lambda c: c['row'] == 0 and bool(c['text'].strip())),
        ('column-headings-rows-1-2', lambda c: c['row'] < 2 and bool(c['text'].strip())),
        ('body-row-labels', lambda c: c['row'] > 0 and c['column'] == 0 and any(ch.isalpha() for ch in c['text'])),
    ]
    patches, rejected, seen = [], [], set()
    for name, select in selectors:
        proposed = deepcopy(table)
        for cell in proposed['cells']:
            if select(cell): cell['is_header'] = True
        try:
            bound, conflicts, moves = bind_adopted(baseline, proposed, method='adjacency')
            if conflicts:
                rejected.append({'variant': name, 'conflicts': conflicts})
                continue
            patch = make_patch(baseline, bound, moves)
            key = fingerprint(patch['after'])
            if key in seen: continue
            seen.add(key)
            patches.append({'variant': name, 'patch': patch})
        except ValueError as error:
            rejected.append({'variant': name, 'reason': str(error)})
    patches.sort(key=lambda p: (len(p['patch']['affected_content_ids']), p['variant']))
    return patches[:MAX_PATCHES], rejected
