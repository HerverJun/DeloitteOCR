"""Conservative same-page split/merge suggestions with independent region identity."""
from collections import Counter
from copy import deepcopy

from ocr_workbench.editing import table_bindings, tables_html
from ocr_workbench.fusion_alignment import canonical_edit, complete_structure, table_content
from ocr_workbench.geometry_contract import intersection_area, polygon_iou, signed_area
from ocr_workbench.tables import parse_tables


def _inside(inner, outer):
    return bool(inner and outer and intersection_area(inner,outer)/abs(signed_area(inner)) >= .95)


def _disjoint(polygons):
    return all(p and all(polygon_iou(p,q) < .01 for q in polygons[i+1:]) for i,p in enumerate(polygons))


def replace_group(edit, indices, tables):
    """Replace exact source ranges; never leave old HTML as duplicate output."""
    value = canonical_edit(edit)
    parsed, bindings, used = table_bindings(value)
    if len(used) != len(value['tables']) or len(parsed) != len(value['tables']):
        raise ValueError('表格阅读位置尚未确定')
    positions = [p for p,i in bindings.items() if i in indices]
    positions.sort()
    if positions != list(range(positions[0],positions[-1]+1)) or indices != list(range(indices[0],indices[-1]+1)):
        raise ValueError('只支持同页相邻表格的结构建议')
    if [bindings[p] for p in positions] != indices:
        raise ValueError('表格阅读顺序不一致')
    for a,b in zip(positions,positions[1:]):
        if value['text'][parsed[a]['source']['end']:parsed[b]['source']['start']].strip():
            raise ValueError('表间存在文字，请先手工核对阅读顺序')
    start,end = parsed[positions[0]]['source']['start'],parsed[positions[-1]]['source']['end']
    value['text'] = value['text'][:start]+tables_html(tables)+value['text'][end:]
    value['tables'][indices[0]:indices[-1]+1] = deepcopy(tables)
    new_parsed = parse_tables(value['text'])
    # Original text/table order must agree throughout before positional rebinding.
    old_order = [bindings[i] for i in sorted(bindings)]
    if old_order != list(range(len(bindings))):
        raise ValueError('请先核对当前文档的表格顺序')
    for table, source in zip(value['tables'],new_parsed):
        table['source'] = source['source']
    return value


def group_suggestions(edit, current, originals, candidates, manual_tables):
    def values(tables):
        return Counter(c['text'] for t in tables for c in t['cells'] if c['text'])
    def eligible(indices, proposed):
        return (all(i not in manual_tables and originals[i] and table_content(current[i]) == table_content(originals[i]) for i in indices)
            and all(complete_structure(c['skeleton']) and not c['unassigned_token_ids'] and not c['reason_codes'] for c in proposed)
            and values([current[i] for i in indices]) == values([c['skeleton'] for c in proposed]))
    def anchors(indices, proposed):
        old = values([current[i] for i in indices])
        return all(sum(old[v] == 1 and n == 1 for v,n in values([p['skeleton']]).items()) >= 2 for p in proposed)
    groups = []
    for i, table in enumerate(current):
        region = table.get('region_polygon')
        targets = [c for c in candidates if _inside(c['polygon'],region)]
        if len(targets) >= 2 and _disjoint([c['polygon'] for c in targets]) and eligible([i],targets) and anchors([i],targets):
            targets.sort(key=lambda c:(min(p[1] for p in c['polygon']),min(p[0] for p in c['polygon'])))
            groups.append(('split_tables',[i],targets))
    for candidate in candidates:
        indices = [i for i,t in enumerate(current) if _inside(t.get('region_polygon'),candidate['polygon'])]
        if len(indices) >= 2 and indices == list(range(indices[0],indices[-1]+1)) and _disjoint([current[i]['region_polygon'] for i in indices]) and eligible(indices,[candidate]):
            # Each pre-existing table needs its own independent text anchors.
            new = values([candidate['skeleton']])
            if all(sum(new[v] == 1 and n == 1 for v,n in values([current[i]]).items()) >= 2 for i in indices):
                groups.append(('merge_tables',indices,[candidate]))
    output = []
    for kind,indices,targets in groups:
        try:
            proposed = replace_group(edit,indices,[c['skeleton'] for c in targets])
        except ValueError:
            continue
        output.append({'kind':kind,'table_indices':indices,'candidate_tables':targets,'proposed_edit':proposed})
    return output
