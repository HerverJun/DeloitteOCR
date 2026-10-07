"""Strict offline scoring. Missing/failed outputs remain in the image denominator."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import itertools
import json
from pathlib import Path
import re
import random
import hashlib
import unicodedata
from run import select_input_samples

def text(value):
    # Report exact NFC text with whitespace folded, but never repair digits/punctuation.
    return re.sub(r'\s+', '', unicodedata.normalize('NFC',str(value or '')))

def levenshtein(a,b):
    if len(a)<len(b):a,b=b,a
    row=list(range(len(b)+1))
    for i,ca in enumerate(a,1):
        new=[i]
        for j,cb in enumerate(b,1):new.append(min(new[-1]+1,row[j]+1,row[j-1]+(ca!=cb)))
        row=new
    return row[-1]

def result_tables(result):
    for value in [result.get('edited'),result.get('original'),result]:
        if isinstance(value,dict) and isinstance(value.get('tables'),list):return value['tables']
    return []

def position(c):return int(c.get('row',-1)),int(c.get('column',-1))
def span(c):return position(c)+(int(c.get('row_span',1)),int(c.get('column_span',1)))

def match_tables(expected,actual):
    """Table order may differ; match whole tables, never move individual cells."""
    remaining=set(range(len(actual)));pairs=[]
    for want in expected:
        keys={position(c):text(c['text']) for c in want['cells']}
        def affinity(index):
            got={position(c):text(c.get('text')) for c in actual[index].get('cells',[])}
            return sum(bool(v) and got.get(k)==v for k,v in keys.items()) - abs(len(got)-len(keys))*.05
        index=max(remaining,key=affinity) if remaining else None
        if index is not None:remaining.remove(index)
        pairs.append((want,actual[index] if index is not None else {'cells':[]}))
    return pairs,sorted(remaining)

def score_sample(truth,result=None, status='success'):
    result=result or {};expected=truth.get('tables',[]);actual=result_tables(result) if status=='success' else []
    pairs,unmatched=match_tables(expected,actual)
    metrics=Counter({'images':1,'failed_images':int(status in {'failed','truncated_output'}),'missing_images':int(status=='missing'),'pending_images':int(status=='running'),'expected_tables':len(expected),'actual_tables':len(actual),
                     'missing_tables':max(0,len(expected)-len(actual)),'extra_tables':len(unmatched)})
    errors=[];expected_texts=[];actual_texts=[]
    for ti,(want,got) in enumerate(pairs):
        got_cells=got.get('cells',[]);got_map={position(c):c for c in got_cells};expected_keys={position(c) for c in want['cells']}
        metrics['duplicate_positions']+=len(got_cells)-len(got_map)
        metrics['extra_cells']+=sum(position(c) not in expected_keys for c in got_cells)
        metrics['structure_shape_errors']+=int(want.get('rows')!=got.get('rows') or want.get('columns')!=got.get('columns'))
        for c in want['cells']:
            expected_texts.append(text(c['text']));key=position(c);g=got_map.get(key);v=text(g.get('text')) if g else '';actual_texts.append(v)
            metrics['reference_characters']+=len(text(c['text']));metrics['character_edits']+=levenshtein(text(c['text']),v)
            metrics['cells']+=1;visible=c.get('visibility','visible')=='visible'
            metrics['visible_cells']+=visible;exact=bool(g is not None and text(c['text'])==v)
            span_ok=bool(g is not None and span(c)==span(g));metrics['text_exact_cells']+=exact;metrics['exact_span_cells']+=span_ok
            metrics['visible_exact_cells']+=int(visible and exact and span_ok)
            critical=bool(re.search(r'\d',str(c['text'])));metrics['critical_cells']+=critical
            metrics['critical_exact_cells']+=int(critical and exact and span_ok)
            metrics['visible_critical_cells']+=int(critical and visible);metrics['visible_critical_exact_cells']+=int(critical and visible and exact and span_ok)
            if g is None:metrics['omitted_cells']+=1
            if text(c['text']) and not v:metrics['omitted_nonempty_cells']+=1
            if not exact or not span_ok:
                errors.append({'table':ti,'row':key[0],'column':key[1],'expected':c['text'],'actual':g.get('text') if g else None,
                               'expected_span':list(span(c)[2:]),'actual_span':list(span(g)[2:]) if g else None,'visibility':c.get('visibility','visible'),'critical':critical})
    for index in unmatched:metrics['extra_cells']+=len(actual[index].get('cells',[]))
    expected_all={text(c['text']) for t in expected for c in t['cells'] if text(c['text'])}
    output_all=[text(c.get('text')) for t in actual for c in t.get('cells',[]) if text(c.get('text'))]
    ui_words={text(s) for r in truth.get('ui_regions',[]) for s in str(r.get('text','')).split()}
    metrics['ui_intrusion_cells']+=sum(v in ui_words and v not in expected_all for v in output_all)
    metrics['unmatched_output_text_cells']+=sum(v not in expected_all for v in output_all)
    body=text(result.get('text',''));body=body or ''.join(output_all)
    expected_body=''.join(expected_texts)+''.join(text(c['text']) for c in truth.get('context_targets',[]))
    ui_markers=['Excel','SUM(C2:C11)','C12','粘贴','排序和筛选','保存更改','文件','开始','状态：就绪']
    ui_leaks=[marker for marker in ui_markers if text(marker) in body and text(marker) not in expected_body]
    metrics['ui_intrusion_text_markers']=len(ui_leaks)
    for context in truth.get('context_targets',[]):
        metrics['context_targets']+=1;metrics['context_retained']+=int(text(context['text']) in body)
    perfect=status=='success' and not errors and not metrics['extra_cells'] and not metrics['extra_tables'] and not metrics['duplicate_positions'] and not metrics['structure_shape_errors'] and not ui_leaks and metrics['context_targets']==metrics['context_retained']
    metrics['perfect_images']=int(perfect)
    metrics['critical_error_images']=int(metrics['critical_exact_cells']!=metrics['critical_cells'])
    metrics['no_table_correct']=int(truth.get('expect_no_table',False) and not actual and status=='success')
    quality=result.get('quality')
    risk_tags=quality.get('issues',quality.get('flags',quality.get('signals',[]))) if isinstance(quality,dict) else result.get('warnings',[])
    metrics['risk_expected']=int(truth.get('expected_risk',False));metrics['risk_flagged']=int(bool(risk_tags))
    metrics['risk_detected']=int(truth.get('expected_risk',False) and bool(risk_tags))
    automatic=bool(quality.get('automatic_delivery',False)) if isinstance(quality,dict) else False
    metrics['quality_unavailable_images']=int(not isinstance(quality,dict))
    metrics['automatic_delivered_images']=int(automatic and status=='success')
    metrics['automatic_error_images']=int(automatic and status=='success' and not perfect)
    metrics['automatic_critical_error_images']=int(automatic and status=='success' and metrics['critical_exact_cells']!=metrics['critical_cells'])
    return {'id':truth['id'],'status':status,'cohort':truth.get('cohort'),'level':truth.get('level'),'group_id':truth.get('group_id'),
            'source_id':truth.get('source_id'),'family':truth.get('family'),'challenge':truth.get('challenge'),'metrics':dict(metrics),'ui_text_markers':ui_leaks,'errors':errors}

def summarize(records):
    sums=Counter()
    for r in records:sums.update(r['metrics'])
    ratios={'whole_image_correct_rate':('perfect_images','images'),'critical_exact_rate':('critical_exact_cells','critical_cells'),
            'visible_critical_exact_rate':('visible_critical_exact_cells','visible_critical_cells'),'visible_cells_exact_rate':('visible_exact_cells','visible_cells'),
            'span_exact_rate':('exact_span_cells','cells'),'character_error_rate':('character_edits','reference_characters'),
            'context_retention_rate':('context_retained','context_targets'),'risk_recall':('risk_detected','risk_expected'),
            'automatic_delivery_coverage':('automatic_delivered_images','images'),'automatic_delivery_error_rate':('automatic_error_images','automatic_delivered_images'),
            'automatic_delivery_critical_error_rate':('automatic_critical_error_images','automatic_delivered_images')}
    return {'totals':dict(sums),'ratios':{k:sums[n]/sums[d] if sums[d] else None for k,(n,d) in ratios.items()},
            'independent_source_count':len({r['source_id'] for r in records}),'group_count':len({r['group_id'] for r in records})}

def clustered_interval(records,repetitions=1000):
    groups=defaultdict(Counter)
    for r in records:groups[r['group_id']].update(r['metrics'])
    clusters=list(groups.values());rng=random.Random(20261007);values={k:[] for k in ['whole_image_correct_rate','critical_exact_rate','span_exact_rate']}
    names={'whole_image_correct_rate':('perfect_images','images'),'critical_exact_rate':('critical_exact_cells','critical_cells'),'span_exact_rate':('exact_span_cells','cells')}
    if not clusters:return {}
    for _ in range(repetitions):
        counts=Counter()
        for _ in clusters:counts.update(rng.choice(clusters))
        for key,(n,d) in names.items():
            if counts[d]:values[key].append(counts[n]/counts[d])
    def bounds(items):
        items=sorted(items)
        return [items[int((len(items)-1)*.025)],items[int((len(items)-1)*.975)]] if items else None
    return {'method':'seeded cluster bootstrap by original public document / Chinese template family; correlated variants retained within each group','seed':20261007,'repetitions':repetitions,'percentile_95':{k:bounds(v) for k,v in values.items()}}

def score_run(truth_path,runroot):
    samples=json.loads(Path(truth_path).read_text('utf-8'))['samples'];runroot=Path(runroot);records=[]
    input_path=Path(truth_path).parent/'inputs.json';binding_path=runroot/'binding.json';binding={}
    if input_path.exists() and binding_path.exists():
        expected_hash=hashlib.sha256(input_path.read_bytes()).hexdigest();binding=json.loads(binding_path.read_text('utf-8'))
        if binding['inputs_sha256']!=expected_hash:raise ValueError('Run inputs do not match the truth set; never mix dataset versions')
    include_ids=binding.get('include_sample_ids')
    if include_ids is not None:samples=select_input_samples(samples,include_ids=include_ids)
    for sample in samples:
        output=runroot/sample['id'];record_path=output/'record.json';result_path=output/'result.json'
        try:
            record=json.loads(record_path.read_text('utf-8')) if record_path.exists() else {'status':'missing'}
            result=json.loads(result_path.read_text('utf-8')) if result_path.exists() else {}
        except json.JSONDecodeError:
            record={'status':'truncated_output'};result={}
        if record.get('quality') is not None:result={**result,'quality':record['quality']}
        records.append(score_sample(sample,result,record.get('status','missing')))
    stratified={}
    for field in ['cohort','level','family','challenge']:
        keys=sorted({str(r[field]) for r in records if r[field] is not None})
        stratified[field]={key:summarize([r for r in records if str(r[field])==key]) for key in keys}
    completed=[r for r in records if r['status'] not in {'missing','running'}]
    report={'scope':'frozen simulated regression, not real smartphone or independent blind generalization','normalization':'NFC and whitespace removal only; punctuation, signs, leading zeros, digits never repaired','whole_image_denominator_includes_missing_failed_truncated':True,'summary':summarize(records),'completed_snapshot_only':summarize(completed),'completed_snapshot_is_final':len(completed)==len(records),'cluster_interval':clustered_interval(records),'grouped':{key:summarize([r for r in records if r['group_id']==key]) for key in sorted({r['group_id'] for r in records})},'source_grouped':{key:summarize([r for r in records if r['source_id']==key]) for key in sorted({r['source_id'] for r in records})},'stratified':stratified,'records':records}
    if include_ids is not None:report.update(scope='explicit diagnostic subset of frozen simulated regression; not a full dataset or independent accuracy claim',include_sample_ids=[sample['id'] for sample in samples])
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--truth',type=Path,required=True);p.add_argument('--runs',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.resolve().drive.upper()!='F:':raise ValueError('F-only output')
    report=score_run(a.truth,a.runs);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8');print(json.dumps(report['summary'],ensure_ascii=False))

if __name__=='__main__':main()
