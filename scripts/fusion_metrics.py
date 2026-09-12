"""Reference-anchored Levenshtein error accounting and evidence diagnostics."""
from collections import Counter
from functools import lru_cache
import unicodedata

from benchmark_metrics import normalize
from ocr_workbench.fusion_alignment import cell_key, table_content
from ocr_workbench.tables import parse_tables


@lru_cache(maxsize=4096)
def aligned(reference, hypothesis):
    """Minimum edit path. Ties: diagonal, deletion, insertion. Bounded corpus."""
    if len(reference)*len(hypothesis)>4_000_000:
        raise ValueError('Metric alignment exceeds 4M cells; split the sample explicitly')
    n,m=len(reference),len(hypothesis)
    steps=[bytearray(m+1) for _ in range(n+1)]
    previous=list(range(m+1))
    for i,a in enumerate(reference,1):
        row=[i]
        for j,b in enumerate(hypothesis,1):
            costs=(previous[j-1]+(a!=b),previous[j]+1,row[-1]+1)
            best=min(costs); row.append(best);steps[i][j]=costs.index(best)
        previous=row
    chars=[None]*n; insertions=['']*(n+1); hypothesis_reference=[None]*m
    counts=Counter(); i,j=n,m
    while i or j:
        op=steps[i][j] if i and j else 1 if i else 2
        if op==0:
            chars[i-1]=hypothesis[j-1];hypothesis_reference[j-1]=i-1
            counts['substitutions']+=reference[i-1]!=hypothesis[j-1];i-=1;j-=1
        elif op==1: counts['deletions']+=1;i-=1
        else: insertions[i]=hypothesis[j-1]+insertions[i];counts['insertions']+=1;j-=1
    return chars,insertions,hypothesis_reference,dict(counts)


def text_gains(reference,before,after):
    reference,before,after=map(normalize,(reference,before,after))
    a,ai,_,_=aligned(reference,before);b,bi,_,_=aligned(reference,after)
    corrected=sum(x!=r and y==r for r,x,y in zip(reference,a,b))
    introduced=sum(x==r and y!=r for r,x,y in zip(reference,a,b))
    # Insertion cost is measured separately per reference gap; a different
    # wrong character at the same gap is still an insertion error.
    corrected+=sum(max(0,len(x)-len(y)) for x,y in zip(ai,bi))
    introduced+=sum(max(0,len(y)-len(x)) for x,y in zip(ai,bi))
    return {'corrected':corrected,'introduced':introduced,'net_corrected':corrected-introduced}


def evidence_metrics(reference, output, sources, kind):
    units=output['fusion']['units']; result=Counter()
    expected=output['fusion']['expected_sources']
    valid=[s for s in sources if s['engine'] in expected and s['status']=='succeeded']
    result['missing_sources']=len(expected)-len(valid)
    result['samples_with_missing_sources']=len(valid)!=len(expected)
    result['evidence_units']=len(units)
    result['automatic_units']=sum(u['automatic'] for u in units)
    result['unresolved_units']=sum(u['needs_review'] for u in units)
    result['structure_review_units']=sum(u['category']=='structure' and u['needs_review'] for u in units)
    for unit in units: result['location_'+unit['location']['level']]+=1
    for s in sources: result['source_'+s['status']]+=1
    if kind=='table':
        tables=parse_tables(reference)
        index={t.get('fusion_id'):i for i,t in enumerate(output['tables'])}
        for unit in units:
            target=unit['target'];position=index.get(target.get('table_id'))
            correct=None
            if position is not None and position<len(tables):
                truth=tables[position]
                if target['kind']=='table': correct=table_content(unit['selected'])==table_content(truth)
                elif target['kind']=='cell':
                    values=[c['text'] for c in truth['cells'] if c['row']==target['row'] and c['column']==target['column']]
                    if len(values)==1: correct=unit['selected']==values[0]
            if unit['automatic']:
                result['automatic_scorable']+=correct is not None
                result['automatic_errors']+=correct is False
        if len(valid)==len(expected) and len(valid)>1:
            for i,t in enumerate(tables):
                candidate_values=[]
                for s in valid:
                    ts=s['original']['tables']
                    candidate_values.append({cell_key(c):c['text'] for c in ts[i]['cells']} if i<len(ts) else {})
                for cell in t['cells']:
                    values=[v.get(cell_key(cell)) for v in candidate_values]
                    wrong=all(v!=cell['text'] for v in values)
                    result['jointly_wrong_reference_cells']+=wrong
                    result['identical_joint_error_cells']+=wrong and len(set(values))==1
        result['multi_table_samples']=len(tables)>1
    else:
        raw_reference=unicodedata.normalize('NFC',reference)
        raw_output=unicodedata.normalize('NFC',output['text'])
        chars,insertions,hypothesis_reference,counts=aligned(raw_reference,raw_output)
        result.update(counts)
        for unit in units:
            if not unit['automatic']: continue
            target=unit['target'];a,z=target.get('start',0),target.get('end',len(raw_output))
            # Units use raw offsets. NFC length differences are unscorable,
            # rather than silently assigning the neighboring character.
            if raw_output!=output['text']: continue
            positions=hypothesis_reference[a:z]
            correct=bool(positions) and all(p is not None and raw_reference[p]==raw_output[a+i] for i,p in enumerate(positions))
            result['automatic_scorable']+=bool(positions)
            result['automatic_errors']+=bool(positions) and not correct
        reference_normal=normalize(reference)
        if len(valid)==len(expected) and len(valid)>1:
            aligned_sources=[aligned(reference_normal,normalize(s['original']['text']))[0] for s in valid]
            for i,r in enumerate(reference_normal):
                values=[s[i] for s in aligned_sources];wrong=all(v!=r for v in values)
                result['jointly_wrong_reference_characters']+=wrong
                result['identical_joint_error_characters']+=wrong and len(set(values))==1
        # Explicit observable line-order proxy; the crop corpus cannot assess
        # full-page mixed text/table reading order.
        lines=reference.splitlines(); unique={v:i for i,v in enumerate(lines) if lines.count(v)==1}
        order=[unique[v] for v in output['text'].splitlines() if v in unique]
        result['reading_order_samples_scorable']=len(order)>1
        result['reading_order_inversions']=sum(a>b for i,a in enumerate(order) for b in order[i+1:])
    return dict(result)


def evidence_summary(rows):
    result=Counter()
    for row in rows: result.update(row)
    value=dict(result)
    for name,num,den in [('automatic_coverage','automatic_units','evidence_units'),('automatic_error_rate','automatic_errors','automatic_scorable'),('unresolved_ratio','unresolved_units','evidence_units')]:
        value[name]=result[num]/result[den] if result[den] else None
    value['automatic_unscorable']=result['automatic_units']-result['automatic_scorable']
    return value
