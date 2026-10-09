"""Dev-informed closed-field/header CPU scope experiment; never product adoption.

Freeze policy/boundaries before replay. Only a complete independently read pixel
partition matching the declared two-row field-header hierarchy may narrow review.
All data rows, unknown labels, Chinese amounts/names/identifiers remain reviewed.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import unicodedata

FORMAT = 'closed-business-header-review-scope-policy-v1'
HELPERS = ('closed_header_review_scope_probe.py', 'closed_header_scope_coverage_review.py',
           'strict_review_subset_probe.py', 'native_diagnostics.py', 'common.py', 'controlled_recovery.py', 'quality.py')

def safe(value):
    p=Path(value).absolute()
    if p.resolve().drive.upper()!='F:':raise ValueError('All project bindings remain on F:')
    for q in (p,*p.parents):
        if q.is_symlink() or q.is_junction():raise ValueError('Linked policy/source/output path')
    return p

def sha(path):return hashlib.sha256(safe(path).read_bytes()).hexdigest()
def bound(path):
    p=safe(path);return {'path':str(p),'sha256':sha(p),'bytes':p.stat().st_size}
def read(path):return json.loads(safe(path).read_text('utf-8'))
def save(path,value):
    with safe(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
def fp(v):return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def no_reference(value):
    if isinstance(value,dict):
        if any(value.get(k) is True for k in ('contains_reference_input','reference_input_used','truth_available_to_inference')):
            raise ValueError('Reference material in rule input')
        for v in value.values():no_reference(v)
    elif isinstance(value,list):
        for v in value:no_reference(v)

def policy_template():
    return {'format':FORMAT,'identity':'m2-development-informed-closed-header-policy-001',
        'development_informed':True,'basis':'Development observed reimbursement field vocabulary and complete spatial header hierarchy; no reference values copied or selected.',
        'field_literal_comparison':'Exact original Unicode string; no whitespace removal, NFC/NFKC or numeric conversion for the closed label predicate.',
        'schema':{'table_count':1,'business_region_count':1,'rows':4,'columns':5,'header_row_count':2,
            'fields':[{'row':0,'column':0,'row_span':2,'column_span':1,'literal':'报销事项'},
                      {'row':0,'column':1,'row_span':1,'column_span':2,'literal':'经办核对'},
                      {'row':0,'column':3,'row_span':1,'column_span':2,'literal':'财务核对'},
                      {'row':1,'column':1,'row_span':1,'column_span':1,'literal':'原报'},
                      {'row':1,'column':2,'row_span':1,'column_span':1,'literal':'核准'},
                      {'row':1,'column':3,'row_span':1,'column_span':1,'literal':'凭证'},
                      {'row':1,'column':4,'row_span':1,'column_span':1,'literal':'处理'}]},
        'prerequisites':['Original and effective tables identical; every nonempty cell uniquely agrees with independent PP-OCR at confidence >= .98.',
            'Actual owned source pixels SHA/size verified and complete anchored observed partition verified by native pure CPU diagnostics.',
            'Metadata source/version binding and literal agreement, no metadata review flag, no medium/high source-quality flag.',
            'All seven exact field labels and their merged parent/child positions/spans cover the first two rows without gap/overlap.',
            'Every header box equals the actual observed source-line bounds; model header flags alone are insufficient.'],
        'always_review':'Every data-row cell; unknown CJK token/name/identifier/amount/ordinal; all digits, signs, punctuation, symbols, blank or non-exact field literals.',
        'unknown_role_behavior':'Keep full image review. No partial header guess and no invented role.',
        'model_calls':0,'candidate_value_repairs':0,'product_state_changed':False,
        'validation40':'Unknown and unread; requires a future frozen validation execution.',
        'previous_pure_cjk_v1_adopted':False,'adoption_qualification':False,'final_delivery_qualification':False,
        'human_review_time_measured':False}

def field_literal(value,field,*,verified_header_role):
    return verified_header_role and isinstance(value,str) and value==field['literal']

def freeze(output):
    output=safe(output)
    if output.exists():raise ValueError('Use a new immutable policy identity')
    output.mkdir(parents=True)
    policy=policy_template();save(output/'policy.json',policy)
    field=policy['schema']['fields'][0]
    examples=['二十元','壹佰','第十号','王小明','住宿','未付款','冲回','00001007','12.00','−20.00','-20.00','(20.00)',
              '12345678901234567890','=1+1','+','−','±','百分之五','第壹号','核 准','报销事项 ']
    boundary=[{'literal':v,'verified_header_role':True,'may_leave_review':field_literal(v,field,verified_header_role=True)} for v in examples]
    if any(v['may_leave_review'] for v in boundary):raise AssertionError('Sensitive/unknown literal escaped')
    categories={k:0 for k in 'NPS'};sequence=hashlib.sha256()
    for ordinal in range(0x110000):
        text=chr(ordinal);category=unicodedata.category(text)
        if category[0] not in categories:continue
        categories[category[0]]+=1;sequence.update(f'{ordinal}:{category}\n'.encode())
        if field_literal(text,field,verified_header_role=True) or field_literal(field['literal']+text,field,verified_header_role=True):
            raise AssertionError('Unicode number/punctuation/symbol escaped')
    assert all(not field_literal(f['literal'],f,verified_header_role=False) for f in policy['schema']['fields'])
    assert all(field_literal(f['literal'],f,verified_header_role=True) for f in policy['schema']['fields'])
    save(output/'boundary.json',{'policy':bound(output/'policy.json'),'unicode_version':unicodedata.unidata_version,
        'all_unicode_number_punctuation_symbol_codepoints_checked':categories,'checked_sequence_sha256':sequence.hexdigest(),
        'all_sensitive_and_unknown_cases_retain_review':True,'label_without_proven_header_role_retains_review':True,
        'exact_labels_in_declared_header_role_only_can_pass':True,'counterexamples':boundary,
        'input_observations_or_truth_opened':False,'development_informed':True})
    helpers={}
    for name in HELPERS:
        p=Path(__file__).resolve().parent/name;target=output/'helper-source'/name
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(p.read_bytes());helpers[name]=bound(target)
    save(output/'freeze.json',{'format':'closed-business-header-policy-freeze-v1','utc':datetime.now(timezone.utc).isoformat(),
        'policy':bound(output/'policy.json'),'boundaries':bound(output/'boundary.json'),'helpers':helpers,
        'frozen_before_full60_rule_execution':True,'input_observations_or_truth_opened':False,'source_or_candidate_values_changed':False,
        'development_informed':True,'validation_or_holdout_read':False,'adoption_qualification':False})
    print({'policy_identity':policy['identity'],'policy_sha256':sha(output/'policy.json'),'output':str(output)})

def header_scope(record,full,policy):
    answer={'id':record['id'],'eligible':False,'reasons':[],'freed_fields':[],'reviewed_cells':[],'base_full_proof':full}
    if not full['eligible']:
        answer['reasons']=['full_independent_source_proof_rejected',*full['reasons']];return answer
    raw=record['candidate']['original'];effective=record['candidate'].get('edited',{}).get('tables',raw['tables'])
    if raw['tables']!=effective:
        answer['reasons']=['effective_tables_differ_from_proof'];return answer
    schema=policy['schema'];tables=raw['tables'];prep=raw['screen']['prepared'];regions=prep.get('business_regions',[])
    if len(tables)!=1 or len(regions)!=1 or (tables[0]['rows'],tables[0]['columns'])!=(schema['rows'],schema['columns']):
        answer['reasons']=['no_declared_complete_header_schema'];return answer
    raw_cells={(c['row'],c['column'],c.get('row_span',1),c.get('column_span',1)):c for c in tables[0]['cells']}
    expected={(f['row'],f['column'],f['row_span'],f['column_span']):f for f in schema['fields']}
    actual_header={key for key in raw_cells if key[0]<schema['header_row_count']}
    if actual_header!=set(expected):answer['reasons']=['header_partition_does_not_match_closed_hierarchy'];return answer
    covered=set()
    for row,column,row_span,column_span in actual_header:
        slots={(r,c) for r in range(row,row+row_span) for c in range(column,column+column_span)}
        if covered&slots:answer['reasons']=['overlapping_header_partition'];return answer
        covered|=slots
    if covered!={(r,c) for r in range(2) for c in range(5)}:
        answer['reasons']=['header_partition_incomplete'];return answer
    proof=full['grid']['tables'][0]
    lines=regions[0]['observed_grid'];ys=lines['horizontal_lines'];xs=lines['vertical_lines']
    observed={(c['row'],c['column'],c.get('row_span',1),c.get('column_span',1)):c for c in proof['cells']}
    full_cells={(c['table'],c['row'],c['column']):c for c in full['cells']}
    for key,field in expected.items():
        if not field_literal(raw_cells[key]['text'],field,verified_header_role=True):
            answer['reasons']=['unknown_or_nonexact_literal_in_header_hierarchy'];return answer
        geometry=observed.get(key)
        if not geometry or geometry.get('range_semantics')!='full_cell' or geometry.get('geometry_origin')!='observed_grid_with_text_anchors':
            answer['reasons']=['header_geometry_not_source_verified'];return answer
        r,c,rs,cs=key;expected_box=[xs[c],ys[r],xs[c+cs],ys[r+rs]]
        if any(abs(a-b)>1e-6 for a,b in zip(geometry['box'],expected_box)):
            answer['reasons']=['header_box_not_bound_to_observed_lines'];return answer
    for key,cell in raw_cells.items():
        entry={**full_cells[(0,key[0],key[1])],'literal':cell['text'],'row_span':key[2],'column_span':key[3]}
        if key in expected:
            entry.update(role='frozen_business_field_header',role_basis='complete_source_pixel_top_two_row_parent_child_partition_with_exact_closed_field_literals',review_reason=None)
            answer['freed_fields'].append(entry)
        else:
            entry.update(role='data_or_unknown_role',review_reason='all_data_and_unknown_values_remain_reviewed')
            answer['reviewed_cells'].append(entry)
    if not answer['reviewed_cells']:answer['reasons']=['no_remaining_review'];answer['freed_fields']=[];return answer
    answer['eligible']=True;answer['header_band_slots']=sorted(map(list,covered));return answer

def run(args):
    capsule=safe(args.policy);output=safe(args.output)
    if output.exists():raise ValueError('Use a new immutable CPU replay identity')
    frozen=read(capsule/'freeze.json');policy=read(capsule/'policy.json')
    assert policy['format']==FORMAT and bound(capsule/'policy.json')==frozen['policy']
    assert bound(capsule/'boundary.json')==frozen['boundaries']
    for part in frozen['helpers'].values():assert bound(part['path'])==part
    assert sha(__file__)==frozen['helpers']['closed_header_review_scope_probe.py']['sha256']
    observations_path=safe(args.observations);inputs_path=safe(args.inputs);source=safe(args.source)
    observations=read(observations_path);inputs=read(inputs_path)
    assert inputs['truth_available_to_inference'] is False and observations['truth_read'] is False
    no_reference(inputs);no_reference(observations)
    assert observations['inputs_sha256']==sha(inputs_path)
    samples={r['id']:r for r in inputs['samples']};records={r['id']:r for r in observations['records']}
    assert len(samples)==len(inputs['samples'])==len(records)==len(observations['records']) and samples.keys()==records.keys()
    assert all(s.startswith('development-') for s in samples),'Only development execution allowed'
    for sid,r in records.items():assert sha(samples[sid]['image'])==samples[sid]['image_sha256']==r['image_sha256']
    modules=[source/'src/ocr_workbench'/f'{n}.py' for n in ('__init__','coordinates','tables','screen_quality','screen_diagnostics','image_utils')]
    source_bindings=[bound(p) for p in modules];original_sha=sha(observations_path)
    output.mkdir(parents=True)
    sys.path.insert(0,str(capsule/'helper-source'))
    from native_diagnostics import diagnostic_source
    from strict_review_subset_probe import decide
    audit_reads=[]
    def audit(event,a):
        if event in ('subprocess.Popen','socket.connect','socket.getaddrinfo','os.system'):raise AssertionError('CPU rule cannot use models/network/subprocess')
        if event!='open' or not isinstance(a[0],(str,bytes,os.PathLike)):return
        p=Path(os.fsdecode(a[0])).absolute();mode,flags=a[1],a[2]
        writing=isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)
        if writing:assert p.resolve().is_relative_to(output)
        if 'truth' in p.name.lower() or any(t in ('validation','holdout','challenge') for t in p.parts):raise AssertionError('Reference/final split forbidden in rule phase')
        frame=sys._getframe(1);diagnostic=False
        while frame:
            diagnostic|=Path(frame.f_code.co_filename).name=='screen_diagnostics.py';frame=frame.f_back
        if diagnostic and p.suffix.lower()=='.png':audit_reads.append(str(safe(p)))
    sys.addaudithook(audit)
    with diagnostic_source(source) as module:
        decisions=[header_scope(row,decide(row,module),policy) for row in observations['records']]
    derived=deepcopy(observations);rows={r['id']:r for r in derived['records']}
    derived.update(evidence_kind='development_cpu_closed_business_header_scope_feasibility',
        derivation={'policy_freeze':bound(capsule/'freeze.json'),'policy':bound(capsule/'policy.json'),'source_bindings':source_bindings,'development_informed':True,'product_state_unchanged':True})
    narrowed=[]
    for decision in decisions:
        if not decision['eligible']:continue
        row=rows[decision['id']]
        assert str(Path(row['candidate']['original']['screen']['prepared']['input_path'])) in audit_reads
        row['native_original_review_snapshot']={k:deepcopy(row.get(k)) for k in ('automatic_delivery','review_image','review_regions','review_state','human_confirmed')}
        row.update(automatic_delivery=False,review_image=False,review_required=True,requires_human_review=True,human_confirmed=False)
        row['review_regions']=[{'scope':'cell','table_index':c['table'],'row':c['row'],'column':c['column'],'reliable':True,
            'reason':c['review_reason'],'evidence_ref':{'policy':bound(capsule/'policy.json'),'sample_id':row['id'],'source_sha256':row['image_sha256'],
                'input_version_id':row['input_version_id'],'result_id':row['result_id'],'result_revision':row['result_revision'],
                'box':c['box'],'auxiliary_text_indices':c['evidence_indices']}} for c in decision['reviewed_cells']]
        narrowed.append({'id':row['id'],'freed_fields':len(decision['freed_fields']),'reviewed_cells':len(decision['reviewed_cells'])})
    assert all(row['candidate']==records[row['id']]['candidate'] for row in derived['records'])
    assert sha(observations_path)==original_sha and all(sha(b['path'])==b['sha256'] for b in source_bindings)
    save(output/'decisions.json',{'scope_inputs':len(decisions),'decisions':decisions})
    save(output/'observations.json',derived)
    report={'format':'closed-business-header-scope-cpu-replay-v1','utc':datetime.now(timezone.utc).isoformat(),'policy':bound(capsule/'policy.json'),
        'policy_freeze':bound(capsule/'freeze.json'),'inputs':bound(inputs_path),'original_observations':bound(observations_path),'source':str(source),
        'source_modules':source_bindings,'helper':bound(__file__),'decisions':bound(output/'decisions.json'),'derived_observations':bound(output/'observations.json'),
        'scope_inputs':len(decisions),'narrowed_images':len(narrowed),'freed_field_cells':sum(n['freed_fields'] for n in narrowed),
        'remaining_local_review_cells':sum(n['reviewed_cells'] for n in narrowed),'narrowed':narrowed,
        'actual_diagnostic_source_pixel_reads':audit_reads,'truth_or_final_split_read':False,'candidate_or_native_values_changed':False,
        'development_informed':True,'gpu_model_calls':0,'human_review_time_measured':False,'adoption_qualification':False,'final_delivery_qualification':False}
    save(output/'result.json',report)
    print({k:report[k] for k in ('scope_inputs','narrowed_images','freed_field_cells','remaining_local_review_cells')})

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze',action='store_true');parser.add_argument('--output',required=True)
    for name in ('policy','source','observations','inputs'):parser.add_argument('--'+name)
    args=parser.parse_args()
    for key in ('TEMP','TMP','APPDATA','LOCALAPPDATA','HF_HOME','TORCH_HOME','XDG_CACHE_HOME'):safe(os.environ[key])
    sys.dont_write_bytecode=True
    if args.freeze:freeze(args.output)
    else:
        if not all(getattr(args,k) for k in ('policy','source','observations','inputs')):parser.error('Replay needs frozen policy/source/observations/inputs')
        run(args)

if __name__=='__main__':main()
