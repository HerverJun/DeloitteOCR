"""Offline-only formal pilot cohorts, performance and quality-routing misses.

Annotated truth is read only by this report. It is never used by an inference
adapter, preprocessing function, experiment manifest or visual target selector.
Only ended success/failed records enter quality denominators; failures count.
"""
import argparse
from collections import Counter, defaultdict
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


def load_module(path):
    spec = importlib.util.spec_from_file_location('formal_screen_offline_score',fpath(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def report(inputs_path,truth_path,runs,tools_root):
    inputs_path,truth_path = fpath(inputs_path),fpath(truth_path)
    inputs = json.loads(inputs_path.read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Formal input manifest is not isolated')
    truths = json.loads(truth_path.read_text('utf-8'))['samples']
    samples = {sample['id']:sample for sample in inputs['samples']}
    if set(samples)!={sample['id'] for sample in truths}:
        raise ValueError('Inputs and offline truth differ')
    score = load_module(fpath(tools_root)/'score.py')
    result = {'scope':'Offline-only ended-record formal dataset-v2 pilot summary; no annotated truth supplied to inference.',
        'created':datetime.now(timezone.utc).isoformat(),'inputs_sha256':digest(inputs_path),'truth_sha256':digest(truth_path),
        'scorer_sha256':digest(fpath(tools_root)/'score.py'),'models':{},
        'denominator_rule':'Quality rates include success+failed ended records only; failed records remain zero-score cases. Pending/missing images are shown separately and never called final. Legacy pilot-hunyuan-full is excluded.'}
    for label,runroot in runs.items():
        runroot = fpath(runroot)
        binding_path = runroot/'binding.json'
        if not binding_path.is_file():
            result['models'][label] = {'run':str(runroot),'progress':{'total':len(truths),'completed':0,'success':0,'failed':0,'pending':0,'missing':len(truths)},'is_final':False,'state':'not_started'}
            continue
        binding = json.loads(binding_path.read_text('utf-8'))
        if binding['inputs_sha256']!=digest(inputs_path) or binding.get('labels_or_ideal_regions_provided') is not False or not binding.get('first_pass'):
            raise ValueError('Not the frozen formal first-pass run: '+str(runroot))
        scored,records,results,identities = [],[],{},[]
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
            if record.get('quality') is not None:
                raw = {**raw,'quality':record['quality']}
            rowscore = score.score_sample(truth,raw,record['status'])
            rowscore['_expected_tables'] = truth['tables']
            scored.append(rowscore)
            records.append(record)
            results[truth['id']] = raw
            identities.append({'id':truth['id'],'record_sha256':hashlib.sha256(record_bytes).hexdigest(),
                               'result_sha256':record.get('result_sha256'),'status':record['status']})
        groups = {field:{str(key):{'quality':score.summarize([row for row in scored if row[field]==key]),
            'performance':performance([record for row,record in zip(scored,records) if row[field]==key])}
            for key in sorted({row[field] for row in scored if row[field] is not None})} for field in ('cohort','level')}
        cross = {cohort+'/'+level:{'quality':score.summarize([row for row in scored if row['cohort']==cohort and row['level']==level]),
            'performance':performance([record for row,record in zip(scored,records) if row['cohort']==cohort and row['level']==level])}
            for cohort,level in sorted({(row['cohort'],row['level']) for row in scored})}
        route = routing(scored,records,results,score)
        for row in scored:
            del row['_expected_tables']
        result['models'][label] = {'run':str(runroot),'binding':binding,'binding_sha256':digest(binding_path),
            'progress':dict(progress),'is_final':progress['completed']==len(truths),'quality':score.summarize(scored),
            'performance':performance(records),'stratified':groups,'cohort_level':cross,'routing':route,'record_identities':identities}
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
    lines += ['','数字串差异中可能包含结构错位导致的差异，不能全部称为单个字符误识别。完整样本身份、信号分布及少量逐格示例保存在同名 JSON 中。当前结果属于屏拍模拟试验，不是实际手机照片的泛化准确率。','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--truth',type=Path,required=True)
    parser.add_argument('--run',action='append',required=True,help='label=F:/formal/run')
    parser.add_argument('--benchmark-tools',type=Path,default=Path('F:/OCR/scripts/screen_benchmark'))
    parser.add_argument('--output',type=Path,required=True,help='New .json path; sibling .md also written')
    args=parser.parse_args()
    output=fpath(args.output)
    if output.exists() or output.with_suffix('.md').exists():
        raise ValueError('Preserve old evidence; select a new report filename')
    runs=dict(value.split('=',1) for value in args.run)
    data=report(args.inputs,args.truth,runs,args.benchmark_tools)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n','utf-8')
    output.with_suffix('.md').write_text(markdown(data),'utf-8')
    print(json.dumps({label:model['progress'] for label,model in data['models'].items()},ensure_ascii=False))


if __name__=='__main__':
    main()
