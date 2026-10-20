"""Bounded, measured OCR parameter trials over the existing native services."""
from __future__ import annotations

from dataclasses import replace
import re
import time
import unicodedata
from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentToolResult, Identity
from . import composed
from .operations import ocr
from .execution import invoke
from .identity import digest


class Candidate(AgentModel):
    engine: Identity
    preprocess: list[ocr.Rotate | ocr.Contrast] = Field(default_factory=list, max_length=2)


class TuneBatch(AgentModel):
    asset_ids: list[Identity] = Field(default_factory=list, max_length=50)
    sample_size: int = Field(default=2, ge=1, le=5)
    candidates: list[Candidate] = Field(default_factory=list, max_length=4)
    ground_truth: dict[str, str] = Field(default_factory=dict, max_length=5,
        description='仅使用用户给定的样本真值；无真值时不得虚构。每份最多2000字符。')
    expected_labels: list[str] = Field(default_factory=list, max_length=20,
        description='用户实际要求识别的字段名，用于核对字段覆盖。')
    retry_failed: bool = True
    link_id: Identity | None = None

    @model_validator(mode='after')
    def limits(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError('Duplicate assets')
        if any(not text or len(text) > 2000 for text in self.ground_truth.values()):
            raise ValueError('Ground truth must contain 1..2000 characters per sample')
        if any(not label or len(label) > 100 for label in self.expected_labels):
            raise ValueError('Field labels must contain 1..100 characters')
        if len({digest(item.model_dump()) for item in self.candidates}) != len(self.candidates):
            raise ValueError('Duplicate candidates')
        return self


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def character_errors(actual, expected):
    # Exact Levenshtein edits, bounded by the truth contract and native preview.
    actual, expected = normalized(actual), normalized(expected)
    if len(actual) > 4000:
        return {'evaluated': False, 'reason': '识别文本超过4000字符，未计算字符误差'}
    previous = list(range(len(expected) + 1))
    for index, character in enumerate(actual, 1):
        current = [index]
        for offset, target in enumerate(expected, 1):
            current.append(min(current[-1] + 1, previous[offset] + 1, previous[offset - 1] + (character != target)))
        previous = current
    edits = previous[-1]
    return {'evaluated': True, 'edit_count': edits, 'truth_characters': len(expected),
            'character_error_rate': edits / max(1, len(expected))}


def source_id(item):
    return (item.get('source_locator') or {}).get('asset_id')


def measure(texts, sample_ids, truth, labels):
    by_asset = {source_id(item): item for item in texts}
    observations, errors, truth_chars, nonempty, replacements, total_chars, covered = [], 0, 0, 0, 0, 0, 0
    for identifier in sample_ids:
        item = by_asset.get(identifier, {})
        text = item.get('text', '')
        nonempty += bool(text.strip())
        replacements += text.count('\ufffd')
        total_chars += len(text)
        covered += sum(label in text for label in labels)
        observed = {'asset_id': identifier, 'available': bool(item), 'characters': len(text),
                    'truncated': bool(item.get('text_truncated')), 'field_labels_found': [label for label in labels if label in text]}
        if identifier in truth:
            error = character_errors(text, truth[identifier]) if not item.get('text_truncated') else {
                'evaluated': False, 'reason': '识别预览被截断，不能据此计算全文字符误差'}
            observed['truth_comparison'] = error
            if error.get('evaluated'):
                errors += error['edit_count']
                truth_chars += error['truth_characters']
        observations.append(observed)
    result = {'samples': observations, 'nonempty_samples': nonempty, 'sample_count': len(sample_ids),
              'replacement_characters': replacements, 'characters': total_chars,
              'field_label_coverage': covered / max(1, len(labels) * len(sample_ids)),
              'truth_characters_compared': truth_chars}
    if truth_chars:
        result.update(metric='character_error_rate', character_error_rate=errors / truth_chars,
                      selection_score=-errors / truth_chars)
    else:
        # These are observables, not accuracy. Extra unverified text earns no
        # score, and ties preserve the first (baseline) candidate.
        result.update(metric='observable_completeness', selection_score=(
            nonempty / len(sample_ids) + (covered / max(1, len(labels) * len(sample_ids)) if labels else 0)
            - replacements / max(1, total_chars)))
    return result


async def tune(args, context):
    op = composed.Operations(context)
    inputs = composed.selected(context, args.asset_ids or context.selected_assets)
    if any(not item['media_type'].startswith('image/') for item in inputs):
        raise ValueError('样本选参当前接受图片；PDF请先使用页面处理工具')
    ids = [item['asset_id'] for item in inputs]
    if not set(args.ground_truth) <= set(ids):
        raise ValueError('真值只能属于本次授权图片')
    prepared = op.require_success(await op.call('tune_prepare', {'link_id': args.link_id},
        lambda value, ctx: ocr.prepare_tool(ocr.OcrPrepare.model_validate(value), ctx), replay=True))['data']
    if prepared.get('requires_project_selection'):
        raise ValueError('存在多个OCR项目，请明确link_id')
    engines = [item['id'] for item in prepared['engines'] if item.get('available', True) is not False]
    candidates = args.candidates or [Candidate(engine=composed._engines(prepared, [])[0])]
    if not args.candidates:
        candidates.append(Candidate(engine=candidates[0].engine, preprocess=[ocr.Contrast(kind='contrast', factor=1.5)]))
    if any(item.engine not in engines for item in candidates):
        raise ValueError('选参方案包含当前不可用引擎，请先核对引擎目录')
    count = min(args.sample_size, len(ids))
    samples = list(args.ground_truth)
    if len(samples) > count:
        raise ValueError('sample_size不能少于提供真值的图片数量')
    for index in range(count):
        identifier = ids[round(index * (len(ids) - 1) / max(1, count - 1))]
        if identifier not in samples:
            samples.append(identifier)
    for identifier in ids:
        if len(samples) >= count:
            break
        if identifier not in samples:
            samples.append(identifier)
    samples = samples[:count]
    trials = []
    for index, candidate in enumerate(candidates):
        started = time.monotonic()
        request = composed.ExtractText(asset_ids=samples, engines=[candidate.engine],
                                      preprocess=candidate.preprocess, link_id=prepared['link_id'])
        result = await composed.extract_text(request, context, publish=False)
        observation = measure(result['data']['readable_text'], samples, args.ground_truth, args.expected_labels)
        trial = {'index': index, 'parameters': candidate.model_dump(mode='json'), 'metrics': observation,
                 'elapsed_seconds': time.monotonic() - started, 'result': result}
        trials.append(trial)
        context.store.public_event(context.run_id, context.generation,
            context.operation_id + ':trial:' + str(index), 'tuning_progress', {
                'phase': 'sample_comparison', 'candidate': index + 1, 'candidate_count': len(candidates),
                'parameters': trial['parameters'], 'metrics': observation, 'elapsed_seconds': trial['elapsed_seconds']})
    best = max(trials, key=lambda item: item['metrics']['selection_score'])
    if not best['metrics']['nonempty_samples']:
        return AgentToolResult(status='error', summary='样本没有可用文字，已保留试验回执，未扩大到整批',
            data={'trials': trials, 'selected': None}, error={'code': 'no_usable_sample', 'message': '样本识别未形成可用文字'}).model_dump(mode='json')
    candidate = candidates[best['index']]
    selected_refs = {}
    def collect(result):
        for item in result['data']['readable_text']:
            identifier = source_id(item)
            text = item.get('text', '')
            if text.strip() and all(label in text for label in args.expected_labels):
                selected_refs[identifier] = item['reference']
    collect(best['result'])
    successful = set(selected_refs)
    sample_success = set(successful)
    remaining = [identifier for identifier in ids if identifier not in successful]
    batch = None
    if remaining:
        batch = await composed.extract_text(composed.ExtractText(asset_ids=remaining, engines=[candidate.engine],
            preprocess=candidate.preprocess, link_id=prepared['link_id']), context, publish=False)
        collect(batch)
        successful = set(selected_refs)
    failed = [identifier for identifier in ids if identifier not in successful]
    retried = []
    if failed and args.retry_failed:
        # The original sample/batch successes are reused. One separately
        # journaled retry contains only proven missing/failed input identities.
        result = await op.call('tune_retry_failed', {'asset_ids': failed, 'parameters': candidate.model_dump(mode='json')},
            lambda value, ctx: composed.extract_text(composed.ExtractText(asset_ids=value['asset_ids'],
                engines=[value['parameters']['engine']], preprocess=value['parameters']['preprocess'],
                link_id=prepared['link_id']), ctx, publish=False), replay=True)
        collect(result)
        successful = set(selected_refs)
        retried = failed
        failed = [identifier for identifier in ids if identifier not in successful]
    unique = list({digest(ref): ref for ref in selected_refs.values()}.values())
    outputs = await op.publish_text({'result_refs': unique})
    public_trials = [{key: value for key, value in trial.items() if key != 'result'} |
                     {'result_refs': trial['result']['result_refs']} for trial in trials]
    metric = best['metrics']['metric']
    return AgentToolResult(status='success', summary='已比较样本并按选定参数处理整批' + (f'，{len(failed)}份仍待处理' if failed else ''),
        result_refs=unique, artifact_refs=[item['artifact_ref'] for item in outputs],
        data={'selected_candidate': best['index'], 'selected_parameters': candidate.model_dump(mode='json'),
            'selection_metric': metric, 'trials': public_trials,
            'sample_asset_ids': samples, 'sample_method': '提供真值的图片优先，其余按输入顺序均匀取样',
            'readable_text': [item['readable_text'] for item in outputs],
            'reused_sample_asset_ids': sorted(sample_success), 'retried_asset_ids': retried,
            'failures': [{'asset_id': identifier} for identifier in failed],
            'requested_count': len(ids), 'completed_count': len(successful & set(ids)),
            'limitations': ['无真值样本的字段覆盖、空文本和替代字符属于可观测指标，不能称为准确率。',
                            '少量样本不能代表整批所有版式；选参只作用于本次任务。'],
            'ground_truth_policy': '仅使用调用者提供的样本真值；忽略Unicode空白后计算Levenshtein字符误差'}).model_dump(mode='json')


def register(registry):
    from workbench_platform.agent.registry import ToolSpec
    registry.register(ToolSpec('ocr_tune_batch', '对授权图片按真实样本比较引擎/旋转/对比度，选定本任务参数并处理整批；复用成功样本，仅重试失败项。最多4方案、5样本。真值仅来自用户；无真值只能报告覆盖等指标，不能声称准确率提升。返回实际选参证据和全文下载。',
        TuneBatch, tune, effect='derive', idempotent=True))
