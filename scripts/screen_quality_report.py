"""Offline-only formal pilot cohorts, performance and quality-routing misses.

Annotated truth is read only by this report. It is never used by an inference
adapter, preprocessing function, experiment manifest or visual target selector.
Only ended success/failed records enter quality denominators; failures count.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fpath(path):
    value = Path(path).resolve()
    if value.drive.upper() != 'F:':
        raise ValueError('F-only report path')
    return value


def quantile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values)-1)*fraction
    lower = int(index)
    upper = min(lower+1,len(values)-1)
    return values[lower]+(values[upper]-values[lower])*(index-lower)


def load_module(path, name='formal_screen_offline_score'):
    path=fpath(path)
    sys.path.insert(0,str(path.parent))
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def production_runtime(source_root, tools_root):
    source_root,tools_root=fpath(source_root),fpath(tools_root)
    source=source_root/'src'
    sys.path.insert(0,str(source))
    evidence=__import__('ocr_workbench.screen_evidence',fromlist=['normalize_screen_context'])
    quality=__import__('ocr_workbench.screen_quality',fromlist=['assess_screen_result'])
    imaging=__import__('ocr_workbench.screen_imaging',fromlist=['POLICY_VERSION'])
    modules=(evidence,quality,imaging)
    if any(not fpath(module.__file__).is_relative_to(source) for module in modules):
        raise ValueError('Production replay modules came from another source root')
    return {'normalize':evidence.normalize_screen_context,'reconcile':evidence.reconcile_screen_evidence,
        'assess':quality.assess_screen_result,'verify':load_module(tools_root/'context.py','production_context_verifier').verify_context_record,
        'preparation_policy':imaging.POLICY_VERSION,'quality_policy':quality.POLICY_VERSION,
        'identity':{'source_root':str(source_root),'files':{Path(module.__file__).name:digest(module.__file__) for module in modules},
                    'context_verifier_sha256':digest(tools_root/'context.py')}}


def replay_production_quality(record, raw, sample, binding, runroot, runtime):
    """Assess unchanged actual OCR using current evidence and exact saved crops."""
    runroot=fpath(runroot)
    context_root=fpath(binding['context_run'])
    if digest(context_root/'binding.json')!=binding['context_binding_sha256']:
        raise ValueError('Consumed context binding changed')
    context_record,context,_=runtime['verify'](context_root/sample['id'],sample,normalize=None)
    if (record.get('context_sha256')!=context_record['context_sha256']
            or record.get('context_evidence_sha256')!=context_record['evidence_sha256']):
        raise ValueError('Consumed context record differs from inference')
    attempt=fpath(record['output'])
    if not attempt.is_relative_to(runroot/sample['id']):
        raise ValueError('Preparation attempt belongs to another run/image')
    prepared_path=attempt/'preprocess.json'
    prepared=json.loads(prepared_path.read_text('utf-8'))
    if (prepared.get('input_sha256')!=sample['image_sha256']
            or prepared.get('policy_version')!=runtime['preparation_policy']
            or any(prepared.get(key)!=sample[key] for key in ('width','height'))):
        raise ValueError('Exact preparation is not the current ROI v2 image/policy')
    if digest(fpath(sample['image']))!=sample['image_sha256']:
        raise ValueError('Production replay source image changed')
    candidates={item['id']:item for item in prepared['prepared_images']}
    selected=record.get('prepared_images',[])
    if not selected:
        raise ValueError('Successful record has no bound inference images')
    for image in selected:
        saved=candidates.get(image['id'])
        if (saved!={key:value for key,value in image.items() if key!='sha256'}
                or digest(fpath(image['path']))!=image.get('sha256')):
            raise ValueError('Consumed crop metadata/pixels differ from exact saved preparation')
    effective=runtime['reconcile'](runtime['normalize'](context),width=prepared['width'],height=prepared['height'])
    before=deepcopy(raw)
    quality=runtime['assess'](raw,image_info=prepared,
        text_evidence={'blocks':effective['text'],'provenance':effective['provenance']})
    if raw!=before:
        raise ValueError('Quality replay modified model output')
    return {'id':sample['id'],'original_record_quality':deepcopy(record.get('quality')),
        'original_result_quality':deepcopy(raw.get('quality')),'effective_quality':quality,
        'preparation_manifest':str(prepared_path),'preparation_manifest_sha256':digest(prepared_path),
        'context_sha256':context_record['context_sha256'],'stored_context_evidence_sha256':context_record['evidence_sha256'],
        'effective_evidence_sha256':hashlib.sha256(json.dumps(effective,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),
        'effective_evidence_policy':effective.get('policy_version'),'reconciliation_policy':effective.get('region_reconciliation_policy'),
        'effective_text_counts':dict(Counter(item.get('kind') for item in effective['text'])),
        'result_sha256':record['result_sha256'],'model_values_unchanged':True,
        'note':'CPU quality-rule replay only. Exact saved preparation is consumed without creating new crops. Its full manifest hash is captured at replay time; inference-selected metadata/pixels are checked against the original record.'}


def performance(records):
    inference = [float(row['inference_seconds']) for row in records if row.get('inference_seconds') is not None]
    wall = [float(row['wall_seconds']) for row in records if row.get('wall_seconds') is not None]
    sessions = {}
    for record in records:
        session = record.get('engine_session')
        if isinstance(session,dict) and session.get('id'):
            sessions[session['id']] = session.get('load_seconds',record.get('model_load_seconds'))
    return {'records':len(records),'inference_p50_seconds':quantile(inference,.5),
        'inference_p95_seconds':quantile(inference,.95),'inference_sum_seconds':sum(inference),
        'per_image_wall_p50_seconds':quantile(wall,.5),'per_image_wall_p95_seconds':quantile(wall,.95),
        'per_image_wall_sum_seconds':sum(wall),'preparation_sum_seconds':sum(row.get('preprocessing_seconds',0) for row in records),
        'unique_sessions':len(sessions),'model_load_seconds_once_per_session':sum(value or 0 for value in sessions.values()),
        'note':'Per-image timing is an ended-record snapshot, not whole-batch wall time. Shared context and model load are not multiplied by images/cohorts.'}


def routing(scored,records,results,score):
    totals = Counter()
    code_counts = Counter()
    examples = {'automatic_errors':[],'unflagged_error_images':[],'numeric_errors_without_local_signal':[]}
    numeric = re.compile(r'^[\s\d.,+\-−–()（）%％￥¥$€£万亿元年月日/:：]+$')
    for row,record in zip(scored,records):
        quality = record.get('quality')
        issues = quality.get('issues',[]) if isinstance(quality,dict) else []
        code_counts.update(str(issue.get('code','unknown')) for issue in issues)
        if row['status'] != 'success':
            totals['failed_records'] += 1
            continue
        totals['success_records'] += 1
        flagged = bool(issues)
        wrong = not row['metrics']['perfect_images']
        critical_wrong = bool(row['metrics']['critical_error_images'])
        automatic = bool(isinstance(quality,dict) and quality.get('automatic_delivery'))
        totals['error_images'] += wrong
        totals['error_images_routed_by_any_signal'] += wrong and flagged
        totals['critical_error_images'] += critical_wrong
        totals['critical_error_images_routed_by_any_signal'] += critical_wrong and flagged
        totals['correct_images_sent_to_review'] += not wrong and bool(isinstance(quality,dict) and quality.get('review_required'))
        totals['automatic_images'] += automatic
        totals['automatic_error_images'] += automatic and wrong
        totals['automatic_critical_error_images'] += automatic and critical_wrong
        if wrong and not flagged:
            examples['unflagged_error_images'].append({'id':row['id'],'critical':critical_wrong,'errors':row['errors'][:3]})
        if automatic and wrong:
            examples['automatic_errors'].append({'id':row['id'],'critical':critical_wrong,'errors':row['errors'][:3],
                'extra_tables':row['metrics']['extra_tables'],'ui_text_markers':row['ui_text_markers'],
                'context_retained':row['metrics'].get('context_retained',0),'context_targets':row['metrics'].get('context_targets',0)})
        actual = score.result_tables(results[row['id']])
        # Scoring may match tables in another order. Local signal indexes are
        # actual output table indexes; retain that correspondence explicitly.
        expected = row['_expected_tables']
        pairs,_ = score.match_tables(expected,actual)
        actual_indexes = {index:next((i for i,value in enumerate(actual) if value is got),None)
                          for index,(_,got) in enumerate(pairs)}
        signals = {(issue.get('table'),issue.get('row'),issue.get('column')) for issue in issues
                   if all(type(issue.get(key)) is int for key in ('table','row','column'))}
        for error in row['errors']:
            if not error['critical']:
                continue
            totals['critical_error_cells'] += 1
            key = (actual_indexes.get(error['table']),error['row'],error['column'])
            localized = key[0] is not None and key in signals
            totals['critical_error_cells_with_local_signal'] += localized
            before,after = str(error['expected']),str(error['actual'] or '')
            transcription = bool(numeric.fullmatch(before) and numeric.fullmatch(after)
                                 and any(char.isdigit() for char in after) and score.text(before)!=score.text(after))
            if transcription:
                totals['numeric_value_difference_cells'] += 1
                totals['numeric_value_difference_cells_without_local_signal'] += not localized
                if not localized and len(examples['numeric_errors_without_local_signal'])<30:
                    examples['numeric_errors_without_local_signal'].append({'id':row['id'],**error,
                        'output_table_index':key[0],'image_has_any_signal':flagged,'automatic_delivery':automatic})
    def ratio(numerator,denominator):
        return totals[numerator]/totals[denominator] if totals[denominator] else None
    return {'counts':dict(totals),'ratios':{
        'error_image_any_signal_recall':ratio('error_images_routed_by_any_signal','error_images'),
        'critical_error_image_any_signal_recall':ratio('critical_error_images_routed_by_any_signal','critical_error_images'),
        'critical_error_cell_local_signal_coverage':ratio('critical_error_cells_with_local_signal','critical_error_cells'),
        'automatic_error_rate':ratio('automatic_error_images','automatic_images')},
        'issue_codes':dict(code_counts),'examples':examples,
        'interpretation':'Any signal only means the image is routed for review; UI warnings do not prove its numeric errors were identified. Local coverage requires actual table/row/column signal correspondence. Numeric differences can include shifted content caused by structure errors; they are not all digit substitutions.'}


def source_input_failure(record,sample):
    def canonical(value):
        value=str(value).replace('/','\\')
        while '\\\\' in value:
            value=value.replace('\\\\','\\')
        return value.casefold()
    return (record.get('status')=='failed' and record.get('error_type')=='FileNotFoundError'
        and canonical(sample['image']) in canonical(record.get('error','')))


def ended_run_timing(runroot,records):
    path=fpath(runroot)/'run-summary.json'
    summary=json.loads(path.read_text('utf-8')) if path.is_file() else None
    return {'ended_record_performance':performance(records),
        'recorded_attempt_wall_seconds':summary.get('attempt_wall_seconds') if summary else None,
        'run_summary_sha256':digest(path) if summary else None,
        'run_summary_is_complete':bool(summary and summary.get('completed')==summary.get('total'))}


def recovered_content(recovery_spec,*,runroot,binding,samples,truths,original_records,original_results,score,runtime):
    """Substitute only actual successes for explicitly mapped missing-input failures."""
    if not isinstance(recovery_spec,dict) or not isinstance(recovery_spec.get('run'),str):
        raise ValueError('Recovery map needs models.label.run and sample_ids')
    ids=recovery_spec.get('sample_ids')
    if (not isinstance(ids,list) or not ids or any(not isinstance(ident,str) for ident in ids)
            or len(set(ids))!=len(ids)):
        raise ValueError('Recovery sample_ids must be explicit, nonempty and unique')
    recovery_root=fpath(recovery_spec['run'])
    if recovery_root==fpath(runroot):
        raise ValueError('Recovery must preserve a separate original first-pass run')
    recovery_binding_path=recovery_root/'binding.json'
    recovery_binding=json.loads(recovery_binding_path.read_text('utf-8'))
    # Image-subset selection is allowed to differ. Model, preprocessing, timeout,
    # actual context and their source/config identities are never relabelled.
    same_fields=('inputs_sha256','worker_source_sha256','engine','engine_config_sha256','bundle',
        'preprocess_module_sha256','request_timeout_seconds','context_binding_sha256','context_run',
        'context_source_policy','context_worker_source_sha256','runner_sha256','first_pass','labels_or_ideal_regions_provided')
    if any(recovery_binding.get(key)!=binding.get(key) for key in same_fields):
        raise ValueError('Recovery input/model/source/config/preparation/timeout/context binding differs')
    if not set(ids)<=set(recovery_binding.get('include_sample_ids',[])):
        raise ValueError('Recovery map IDs were not explicitly selected in the new inference run')
    actual_records={record['id']:record for record in original_records}
    actual_results=dict(original_results)
    locations={ident:fpath(runroot) for ident in actual_records}
    receipts,recovery_records=[],[]
    for ident in ids:
        if ident not in samples or ident not in actual_records or not source_input_failure(actual_records[ident],samples[ident]):
            raise ValueError('Only an ended FileNotFoundError for this original image can be restored: '+ident)
        source_path=fpath(runroot)/ident/'record.json'
        replacement_path=recovery_root/ident/'record.json'
        receipt={'id':ident,'image_sha256':samples[ident]['image_sha256'],
            'first_pass_record':str(source_path),'first_pass_record_sha256':digest(source_path),
            'recovery_record':str(replacement_path),'substituted':False}
        if not replacement_path.is_file():
            receipt['recovery_status']='missing'
            receipts.append(receipt)
            continue
        replacement_bytes=replacement_path.read_bytes()
        replacement=json.loads(replacement_bytes)
        if replacement.get('id')!=ident or replacement.get('image_sha256')!=samples[ident]['image_sha256']:
            raise ValueError('Recovery actual record image identity differs: '+ident)
        if digest(fpath(samples[ident]['image']))!=samples[ident]['image_sha256']:
            raise ValueError('Restored image bytes differ from original frozen input: '+ident)
        receipt.update(recovery_record_sha256=hashlib.sha256(replacement_bytes).hexdigest(),
                       recovery_status=replacement.get('status'))
        if replacement.get('status') in {'success','failed'}:
            recovery_records.append(replacement)
        if replacement.get('status')!='success':
            receipt.update(error_type=replacement.get('error_type'),error=replacement.get('error'))
            receipt['reason']='No successful actual recovery result; original source-input failure remains visible'
            receipts.append(receipt)
            continue
        raw_path=recovery_root/ident/'result.json'
        if replacement.get('engine')!=binding.get('engine'):
            raise ValueError('Recovery actual record model differs: '+ident)
        for name in ('context_sha256','context_evidence_sha256'):
            if replacement.get(name)!=actual_records[ident].get(name):
                raise ValueError('Recovery consumed per-image context differs: '+ident)
        attempt=fpath(replacement['output'])
        if not attempt.is_relative_to(recovery_root/ident):
            raise ValueError('Recovery preparation attempt belongs to another image/run')
        preparation=json.loads((attempt/'preprocess.json').read_text('utf-8'))
        if preparation.get('input_sha256')!=samples[ident]['image_sha256']:
            raise ValueError('Recovery preparation source image differs')
        prepared_images={image['id']:image for image in preparation.get('prepared_images',[])}
        if not replacement.get('prepared_images'):
            raise ValueError('Recovery success is missing consumed crop identities')
        for image in replacement['prepared_images']:
            if (prepared_images.get(image['id'])!={key:value for key,value in image.items() if key!='sha256'}
                    or digest(fpath(image['path']))!=image.get('sha256')):
                raise ValueError('Recovery consumed crop metadata/pixels changed')
        raw_bytes=raw_path.read_bytes()
        result_hash=hashlib.sha256(raw_bytes).hexdigest()
        if result_hash!=replacement.get('result_sha256'):
            raise ValueError('Recovery actual result changed: '+ident)
        observed=json.loads(raw_bytes)
        actual_records[ident],actual_results[ident],locations[ident]=replacement,observed,recovery_root
        receipt.update(substituted=True,result=str(raw_path),result_sha256=result_hash)
        receipts.append(receipt)
    scored,records,results,replayed,replay_records=[],[],{},[],[]
    for truth in truths:
        ident=truth['id']
        if ident not in actual_records:
            continue
        record=actual_records[ident]
        raw=actual_results.get(ident,{})
        scored_raw={**raw,'quality':record['quality']} if record.get('quality') is not None else raw
        row=score.score_sample(truth,scored_raw,record['status'])
        row['_expected_tables']=truth['tables']
        scored.append(row);records.append(record);results[ident]=scored_raw
        if runtime and record['status']=='success':
            replay=replay_production_quality(record,raw,samples[ident],
                recovery_binding if locations[ident]==recovery_root else binding,locations[ident],runtime)
            replayed.append(replay)
            replay_records.append({**record,'quality':replay['effective_quality']})
        else:
            replay_records.append(record)
    remaining=[record['id'] for record in records if source_input_failure(record,samples[record['id']])]
    output={'scope':'源输入事故恢复后唯一图内容汇总；原始 first-pass 与独立恢复回执均保留。',
        'recovery_run':str(recovery_root),'recovery_binding':recovery_binding,
        'recovery_binding_sha256':digest(recovery_binding_path),'receipts':receipts,
        'source_input_failures_original':sum(source_input_failure(record,samples[record['id']]) for record in original_records),
        'restored_successes':sum(receipt['substituted'] for receipt in receipts),
        'source_input_failures_remaining':remaining,'unique_ended_images':len(scored),'total_images':len(truths),
        'content_measurement_complete':len(scored)==len(truths) and not remaining,
        'quality':score.summarize(scored),'routing':routing(scored,records,results,score),
        'cohort_level':{cohort+'/'+level:score.summarize([row for row in scored if row['cohort']==cohort and row['level']==level])
                        for cohort,level in sorted({(row['cohort'],row['level']) for row in scored})},
        'timing':{'first_pass':ended_run_timing(runroot,original_records),
                  'independent_recovery':ended_run_timing(recovery_root,recovery_records),
                  'note':'两次运行墙钟分别记录，不拼成连续72图批次时间。唯一图内容评分选择可用恢复成功结果，不用于删除旧尝试的计算成本。'}}
    if runtime:
        output['production_v2_quality_replay']={'scope':'生产v2规则对唯一图实际结果CPU回放，OCR值和原始quality保持不变。',
            'source':runtime['identity'],'successes_replayed':len(replayed),
            'routing':routing(scored,replay_records,results,score),'rows':replayed,'ocr_metrics_unchanged':True}
    return output


def report(inputs_path,truth_path,runs,tools_root,*,production_source=None,required_preprocess_sha256=None,recovery_map_path=None):
    inputs_path,truth_path = fpath(inputs_path),fpath(truth_path)
    inputs = json.loads(inputs_path.read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Formal input manifest is not isolated')
    truths = json.loads(truth_path.read_text('utf-8'))['samples']
    samples = {sample['id']:sample for sample in inputs['samples']}
    if set(samples)!={sample['id'] for sample in truths}:
        raise ValueError('Inputs and offline truth differ')
    score = load_module(fpath(tools_root)/'score.py')
    runtime=production_runtime(production_source,tools_root) if production_source else None
    if runtime and not required_preprocess_sha256:
        raise ValueError('Production v2 replay requires a frozen ROI v2 preprocess hash')
    recovery_map=json.loads(fpath(recovery_map_path).read_text('utf-8')) if recovery_map_path else None
    if recovery_map is not None and (not isinstance(recovery_map,dict) or not isinstance(recovery_map.get('models'),dict)
                                    or not recovery_map['models'] or set(recovery_map['models'])-set(runs)):
        raise ValueError('Recovery receipt map must name only explicit report models')
    result = {'scope':'Offline-only ended-record formal dataset-v2 pilot summary; no annotated truth supplied to inference.',
        'created':datetime.now(timezone.utc).isoformat(),'inputs_sha256':digest(inputs_path),'truth_sha256':digest(truth_path),
        'scorer_sha256':digest(fpath(tools_root)/'score.py'),'models':{},
        'denominator_rule':'Quality rates include success+failed ended records only; failed records remain zero-score cases. Pending/missing images are shown separately and never called final. Legacy pilot-hunyuan-full is excluded.'}
    if recovery_map_path:
        result['recovery_receipt_map']={'path':str(fpath(recovery_map_path)),'sha256':digest(recovery_map_path)}
    for label,runroot in runs.items():
        runroot = fpath(runroot)
        binding_path = runroot/'binding.json'
        if not binding_path.is_file():
            result['models'][label] = {'run':str(runroot),'progress':{'total':len(truths),'completed':0,'success':0,'failed':0,'pending':0,'missing':len(truths)},'is_final':False,'state':'not_started'}
            continue
        binding = json.loads(binding_path.read_text('utf-8'))
        if binding['inputs_sha256']!=digest(inputs_path) or binding.get('labels_or_ideal_regions_provided') is not False or not binding.get('first_pass'):
            raise ValueError('Not the frozen formal first-pass run: '+str(runroot))
        if required_preprocess_sha256 and binding.get('preprocess_module_sha256')!=required_preprocess_sha256:
            raise ValueError('Cannot mix ROI v1/other preparation with ROI v2 replay: '+str(runroot))
        scored,records,results,source_results,identities = [],[],{},{},[]
        replay_rows,replay_records=[],[]
        progress = Counter(total=len(truths))
        for truth in truths:
            folder = runroot/truth['id']
            record_path = folder/'record.json'
            if not record_path.is_file():
                progress['missing'] += 1
                continue
            record_bytes = record_path.read_bytes()
            record = json.loads(record_bytes)
            if record.get('id')!=truth['id'] or record.get('image_sha256')!=samples[truth['id']]['image_sha256']:
                raise ValueError('Record identity differs: '+truth['id'])
            if record.get('status') not in {'success','failed'}:
                progress['pending'] += 1
                continue
            progress['completed'] += 1
            progress[record['status']] += 1
            raw = {}
            if record['status']=='success':
                raw_bytes = (folder/'result.json').read_bytes()
                if hashlib.sha256(raw_bytes).hexdigest()!=record['result_sha256']:
                    raise ValueError('Completed result changed: '+truth['id'])
                raw = json.loads(raw_bytes)
            source_raw=raw
            source_results[truth['id']]=source_raw
            if record.get('quality') is not None:
                raw = {**raw,'quality':record['quality']}
            rowscore = score.score_sample(truth,raw,record['status'])
            rowscore['_expected_tables'] = truth['tables']
            scored.append(rowscore)
            records.append(record)
            results[truth['id']] = raw
            if runtime and record['status']=='success':
                replay=replay_production_quality(record,source_raw,samples[truth['id']],binding,runroot,runtime)
                replay_rows.append(replay)
                replay_records.append({**record,'quality':replay['effective_quality']})
            else:
                replay_records.append(record)
            identities.append({'id':truth['id'],'record_sha256':hashlib.sha256(record_bytes).hexdigest(),
                               'result_sha256':record.get('result_sha256'),'status':record['status']})
        groups = {field:{str(key):{'quality':score.summarize([row for row in scored if row[field]==key]),
            'performance':performance([record for row,record in zip(scored,records) if row[field]==key])}
            for key in sorted({row[field] for row in scored if row[field] is not None})} for field in ('cohort','level')}
        cross = {cohort+'/'+level:{'quality':score.summarize([row for row in scored if row['cohort']==cohort and row['level']==level]),
            'performance':performance([record for row,record in zip(scored,records) if row['cohort']==cohort and row['level']==level])}
            for cohort,level in sorted({(row['cohort'],row['level']) for row in scored})}
        route = routing(scored,records,results,score)
        replay_route=routing(scored,replay_records,results,score) if runtime else None
        for row in scored:
            del row['_expected_tables']
        result['models'][label] = {'run':str(runroot),'binding':binding,'binding_sha256':digest(binding_path),
            'progress':dict(progress),'is_final':progress['completed']==len(truths),'quality':score.summarize(scored),
            'performance':performance(records),'stratified':groups,'cohort_level':cross,'routing':route,'record_identities':identities}
        result['models'][label]['first_pass_failure_attribution']={
            'source_input_FileNotFoundError':sum(source_input_failure(record,samples[record['id']]) for record in records),
            'other_failed_records':sum(record['status']=='failed' and not source_input_failure(record,samples[record['id']]) for record in records),
            'note':'输入缺文件是测试执行事故，不能当作模型识别错误；first-pass原分母/回执保留，恢复后的唯一图内容单列。'}
        if runtime:
            result['models'][label]['production_v2_quality_replay']={
                'scope':'生产 v2 质量规则 CPU 回放；原始 quality 与 OCR 内容不覆盖，不产生新推理准确率。',
                'source':runtime['identity'],'required_preprocess_sha256':required_preprocess_sha256,
                'preparation_policy':runtime['preparation_policy'],'quality_policy':runtime['quality_policy'],
                'successes_replayed':len(replay_rows),'failed_records_not_replayed':progress.get('failed',0),
                'routing':replay_route,'rows':replay_rows,'ocr_metrics_unchanged':True,
                'delivery_transitions':dict(Counter(
                    ('auto' if row['original_record_quality'].get('automatic_delivery') else 'review')+'→'+
                    ('auto' if row['effective_quality'].get('automatic_delivery') else 'review')
                    for row in replay_rows if isinstance(row['original_record_quality'],dict)))}
        if recovery_map and label in recovery_map['models']:
            result['models'][label]['source_input_incident_recovery']=recovered_content(
                recovery_map['models'][label],runroot=runroot,binding=binding,samples=samples,truths=truths,
                original_records=records,original_results=source_results,score=score,runtime=runtime)
    return result


def markdown(data):
    def percent(value):
        return '—' if value is None else f'{value:.2%}'
    def seconds(value):
        return '—' if value is None else f'{value:.2f}'
    lines = ['# 正式试验集质量与疑点分流','',f"采样时间：{data['created']}",'',
        '仅统计已经结束的成功或失败记录，失败保留在质量分母。未完成记录单列；旧版 Hunyuan 数据不混入。关键字段指含数字的字段，其精确率同时要求文字、行列和合并范围正确。','',
        '|模型|结束/72|失败|整图正确|关键字段精确|OCR P50秒|OCR P95秒|自动交付/自动错误|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for label,model in data['models'].items():
        p=model['progress'];q=model.get('quality',{}).get('ratios',{});t=model.get('performance',{});r=model.get('routing',{}).get('counts',{})
        lines.append(f"|{label}{'' if model['is_final'] else '（未结束）'}|{p['completed']}/{p['total']}|{p.get('failed',0)}|{percent(q.get('whole_image_correct_rate'))}|{percent(q.get('critical_exact_rate'))}|{seconds(t.get('inference_p50_seconds'))}|{seconds(t.get('inference_p95_seconds'))}|{r.get('automatic_images',0)}/{r.get('automatic_error_images',0)}|")
    lines += ['','|模型 / 来源 / 难度|结束图片|失败|整图正确|关键字段精确|OCR P95秒|','|---|---:|---:|---:|---:|---:|']
    for label,model in data['models'].items():
        for group,values in model.get('cohort_level',{}).items():
            q=values['quality'];p=values['performance']
            lines.append(f"|{label} / {group}|{q['totals']['images']}|{q['totals'].get('failed_images',0)}|{percent(q['ratios']['whole_image_correct_rate'])}|{percent(q['ratios']['critical_exact_rate'])}|{seconds(p['inference_p95_seconds'])}|")
    lines += ['','有疑点只表示整张图进入复核，不能证明数字错误已被定位。以下“局部标记”要求识别结果的表、行、列对应；普通 UI 或完整性提示不算具体数字提示。','']
    for label,model in data['models'].items():
        route=model.get('routing')
        if not route:
            continue
        c=route['counts'];r=route['ratios']
        lines.append(f"- {label}：{c.get('error_images',0)} 张错误图中，{c.get('error_images_routed_by_any_signal',0)} 张有任意提示；关键错误图分流召回 {percent(r['critical_error_image_any_signal_recall'])}。{c.get('critical_error_cells',0)} 个关键字段错误中，{c.get('critical_error_cells_with_local_signal',0)} 个有局部标记；{c.get('numeric_value_difference_cells_without_local_signal',0)} 个数字串差异没有对应局部标记。自动交付 {c.get('automatic_images',0)} 张，错误 {c.get('automatic_error_images',0)} 张，其中关键错误 {c.get('automatic_critical_error_images',0)} 张。")
        for example in route['examples']['automatic_errors'][:3]:
            lines.append(f"  - 自动错误示例：{example['id']}；关键错误={example['critical']}；{json.dumps(example['errors'],ensure_ascii=False)}")
    if any(model.get('production_v2_quality_replay') for model in data['models'].values()):
        lines += ['','## 生产 v2 质量规则回放','',
            '对实际 ROI v2 结果，使用当前 normalize/reconcile 从已冻结真实上下文重建有效证据，读取同次推理保存的完整预处理信息与选中裁剪，不重跑模型、不改数字、文字、结构或原 quality。上面的 OCR 内容成绩仍按原始实际结果计算。','',
            '|模型|成功结果回放数|错误图有任意提示|关键错误图有提示|关键错误格局部标记|自动交付/错误/关键错误|',
            '|---|---:|---:|---:|---:|---:|']
        for label,model in data['models'].items():
            replay=model.get('production_v2_quality_replay')
            if not replay:
                continue
            c=replay['routing']['counts']
            lines.append(f"|{label}|{replay['successes_replayed']}|{c.get('error_images_routed_by_any_signal',0)}/{c.get('error_images',0)}|{c.get('critical_error_images_routed_by_any_signal',0)}/{c.get('critical_error_images',0)}|{c.get('critical_error_cells_with_local_signal',0)}/{c.get('critical_error_cells',0)}|{c.get('automatic_images',0)}/{c.get('automatic_error_images',0)}/{c.get('automatic_critical_error_images',0)}|")
        lines += ['','回放使用的完整预处理 manifest 哈希在回放时采集；选中裁剪的原始元数据和像素哈希与推理记录逐项校验。失败记录留在 OCR 质量分母，不调用质量回放。来源版本、原始与回放 quality、逐图状态变化均保存在 JSON 中。']
    if data.get('recovery_receipt_map'):
        lines += ['','## 源输入事故恢复后的唯一图内容','',
            '只允许为旧记录中原图缺文件的 FileNotFoundError 接入独立恢复成功回执。相同输入、模型/源码/配置、预处理、超时和上下文绑定均核验；旧成功与真正模型失败不替换。原始 first-pass 成绩仍保留在前文，不能把其中缺文件事故称为模型识别错误。','',
            '|模型|原输入事故|恢复成功|仍缺实际识别结果|唯一图结束/总数|整图正确|关键字段精确|内容测量完整|',
            '|---|---:|---:|---:|---:|---:|---:|---|']
        for label,model in data['models'].items():
            recovery=model.get('source_input_incident_recovery')
            if not recovery:
                continue
            q=recovery['quality']['ratios']
            lines.append(f"|{label}|{recovery['source_input_failures_original']}|{recovery['restored_successes']}|{len(recovery['source_input_failures_remaining'])}|{recovery['unique_ended_images']}/{recovery['total_images']}|{percent(q['whole_image_correct_rate'])}|{percent(q['critical_exact_rate'])}|{'是' if recovery['content_measurement_complete'] else '否'}|")
            for stage,timing in recovery['timing'].items():
                if isinstance(timing,dict):
                    lines.append(f"- {label} {stage} 独立运行墙钟：{seconds(timing['recorded_attempt_wall_seconds'])} 秒；结束记录推理累计：{seconds(timing['ended_record_performance']['inference_sum_seconds'])} 秒。")
        lines += ['','上述两次墙钟不拼为连续72图批次时间。恢复后的内容指标每张图仅使用一份实际结果；旧失败尝试的耗时和回执继续保留。']
    lines += ['','数字串差异中可能包含结构错位导致的差异，不能全部称为单个字符误识别。完整样本身份、信号分布及少量逐格示例保存在同名 JSON 中。当前结果属于屏拍模拟试验，不是实际手机照片的泛化准确率。','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--truth',type=Path,required=True)
    parser.add_argument('--run',action='append',required=True,help='label=F:/formal/run')
    parser.add_argument('--benchmark-tools',type=Path,default=Path('F:/OCR/scripts/screen_benchmark'))
    parser.add_argument('--output',type=Path,required=True,help='New .json path; sibling .md also written')
    parser.add_argument('--production-quality-source',type=Path,help='Current F-drive source root for CPU production v2 quality replay')
    parser.add_argument('--required-preprocess-sha256',help='Frozen ROI v2 preprocess identity; rejects any ROI v1/other run')
    parser.add_argument('--recovery-receipt-map',type=Path,help='Explicit JSON models.label={run:F:/new-recovery,sample_ids:[...]}')
    args=parser.parse_args()
    output=fpath(args.output)
    if output.exists() or output.with_suffix('.md').exists():
        raise ValueError('Preserve old evidence; select a new report filename')
    runs=dict(value.split('=',1) for value in args.run)
    data=report(args.inputs,args.truth,runs,args.benchmark_tools,
        production_source=args.production_quality_source,required_preprocess_sha256=args.required_preprocess_sha256,
        recovery_map_path=args.recovery_receipt_map)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n','utf-8')
    output.with_suffix('.md').write_text(markdown(data),'utf-8')
    print(json.dumps({label:model['progress'] for label,model in data['models'].items()},ensure_ascii=False))


if __name__=='__main__':
    main()
