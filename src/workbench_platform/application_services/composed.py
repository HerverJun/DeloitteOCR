"""Goal-sized operations composed from the existing journal and native services.

No model decisions or second task engine live here. Each internal operation has
a stable child identity and the same receipt lookup used by explicit actions.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Literal
import asyncio

from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentToolResult, Identity
from packages.contracts.data_operations import DataInspect, DataStep, InputOptions
from .context import require_write
from .execution import invoke
from .identity import digest
from .image_text import _operation, _fixed_inputs, _engines, ReceiptUnknown
from .operations import data, ocr, pdf


class ExtractText(AgentModel):
    asset_ids: list[Identity] = Field(default_factory=list, max_length=50)
    page_numbers: list[int] | None = Field(default=None, min_length=1, max_length=1000)
    mode: Literal['auto', 'native', 'ocr'] = 'auto'
    engines: list[Identity] = Field(default_factory=list, max_length=16)
    preprocess: list[ocr.Rotate | ocr.Contrast] = Field(default_factory=list, max_length=2)
    link_id: Identity | None = None

    @model_validator(mode='after')
    def distinct(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError('Duplicate assets')
        if self.page_numbers and (min(self.page_numbers) < 1 or len(set(self.page_numbers)) != len(self.page_numbers)):
            raise ValueError('Pages must be distinct positive numbers')
        return self


class CleanFile(AgentModel):
    asset_id: Identity
    options: InputOptions = Field(default_factory=InputOptions)


class CleanFiles(AgentModel):
    files: list[CleanFile] = Field(min_length=1, max_length=50)
    # Reuse the native, discriminated step contract, without its session fields.
    steps: list[DataStep.model_fields['step'].annotation] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def distinct(self):
        if len({item.asset_id for item in self.files}) != len(self.files):
            raise ValueError('Each input must occur once')
        return self


def selected(context, identifiers):
    from .references import asset_allowed
    registry = context.services.asset_library[0]
    result = []
    for identifier in identifiers:
        if not asset_allowed(context, identifier):
            raise ValueError('资料不在当前任务范围')
        asset = registry.get(context.workspace_id, identifier)
        result.append({'asset_id': identifier, 'revision': asset.revision,
                       'blob_hash': asset.blob_hash, 'name': asset.original_name, 'media_type': asset.media_type})
    if not result:
        raise ValueError('请先选择输入资料')
    return result


class Operations:
    def __init__(self, context):
        require_write(context)
        context.current()
        self.context = context
        self.runtime = context.services.agent
        self.state = self.runtime.graph.load(context.run_id)
        self.namespace = ''

    async def call(self, name, arguments, handler, *, lookup=None, replay=False):
        args = {**arguments, 'parent_operation_id': self.context.operation_id, '_composition_key': self.namespace}
        def payload(value):
            return {key: item for key, item in value.items() if key not in {'parent_operation_id', '_composition_key'}}
        result, child = await _operation(self.runtime, self.context, name, args,
            lambda value, ctx: handler(payload(value), ctx),
            lookup=(lambda value, ctx: lookup(payload(value), ctx)) if lookup else None,
            safe_replay=replay)
        if result['status'] == 'pending':
            result = await self.runtime.harness_settle(self.state,
                {'call_id': 'direct-' + name + '-' + digest(args)[:24], 'tool': name, 'arguments': args},
                result, child.operation_id)
        if result['status'] == 'needs_verification':
            raise ReceiptUnknown(result['summary'])
        return result

    async def typed(self, name, schema, handler, arguments, *, lookup=None, replay=False):
        return await self.call(name, arguments, lambda value, ctx: handler(schema.model_validate(value), ctx),
            lookup=(lambda value, ctx: lookup(schema.model_validate(value), ctx, include_not_submitted=True)) if lookup else None,
            replay=replay)

    def require_success(self, result):
        if result['status'] != 'success':
            raise ValueError(result['summary'])
        return result

    async def publish_text(self, result):
        from workbench_platform.artifacts.text_outputs import publish_text_result
        outputs = []
        for reference in result.get('result_refs', []):
            output = await invoke(publish_text_result, self.context, reference)
            outputs.append(output)
            self.context.store.public_event(self.context.run_id, self.context.generation,
                self.context.operation_id + ':text:' + digest(reference), 'delivery', {
                    'status': 'available', 'content': '文字已可读取和下载',
                    'delivery': {'status': 'success', 'summary': '文字结果', 'result_refs': [reference],
                        'artifact_refs': [output['artifact_ref']], 'data': {'verified': True,
                            'finalized': False, 'readable_text': [output['readable_text']],
                            'delivery': [output['delivery']]}}})
        return outputs


async def extract_text(args, context, *, publish=True):
    op = Operations(context)
    inputs = selected(context, args.asset_ids or context.selected_assets)
    images = [item for item in inputs if item['media_type'].startswith('image/')]
    documents = [item for item in inputs if item['media_type'] == 'application/pdf']
    if len(images) + len(documents) != len(inputs):
        raise ValueError('extract_text接受图片和PDF；普通文本请使用read')
    if args.page_numbers and (images or len(documents) != 1):
        raise ValueError('指定页码时请选择一份PDF')
    prepared = op.require_success(await op.call('extract_prepare', {'link_id': args.link_id},
        lambda value, ctx: ocr.prepare_tool(ocr.OcrPrepare.model_validate(value), ctx), replay=True))['data']
    if prepared.get('requires_project_selection'):
        return AgentToolResult(status='error', summary='存在多个OCR项目，请指定link_id',
            data=prepared, error={'code': 'ambiguous_project', 'message': '请选择适用项目'}).model_dump(mode='json')
    refs, artifacts, texts, jobs, failures = [], [], [], [], []
    groups = ([images] if images else []) + [[item] for item in documents]
    for group in groups:
        op.namespace = digest([item['asset_id'] for item in group])
        frozen = op.require_success(await op.call('extract_inputs', {'inputs': [
            {key: item[key] for key in ('asset_id', 'revision', 'blob_hash')} for item in group]},
            _fixed_inputs, replay=True))['data']
        base = {'link_id': prepared['link_id'], 'asset_set_id': frozen['asset_set_id'],
                'asset_set_revision': frozen['revision']}
        if group[0]['media_type'] == 'application/pdf':
            inspected = op.require_success(await op.typed('ocr_inspect_document', pdf.InspectDocument,
                pdf.inspect, base, replay=True))['data']
            document = inspected['documents'][0]
            pages = args.page_numbers or list(range(1, document['page_count'] + 1))
            result = await op.typed('ocr_process_pages', pdf.ProcessPages, pdf.process,
                {'handoff_id': inspected['handoff_id'], 'document_id': document['document_id'],
                 'page_numbers': pages, 'mode': args.mode, 'engine': args.engines[0] if args.engines else 'auto'}, lookup=pdf.lookup)
        else:
            result = await op.typed('ocr_run', ocr.OcrRun, ocr.run,
                {**base, 'engines': _engines(prepared, args.engines),
                 'preprocess': [item.model_dump() for item in args.preprocess]}, lookup=ocr.lookup)
        jobs.extend(result.get('job_refs', []))
        if result['status'] != 'success' or result.get('coverage', {}).get('failed'):
            failures.append({'asset_ids': [item['asset_id'] for item in group], 'result': result})
        refs.extend(result.get('result_refs', []))
        if publish:
            outputs = await op.publish_text(result)
        else:
            from workbench_platform.artifacts.text_outputs import verified_text, text_page
            from .references import verify
            outputs = []
            for reference in result.get('result_refs', []):
                value = await invoke(verify, reference, context)
                outputs.append({'readable_text': {'reference': reference,
                    'source_locator': value['source_asset']['source_locator'],
                    **text_page(verified_text(value), limit=16000)}})
        artifacts.extend(item['artifact_ref'] for item in outputs if 'artifact_ref' in item)
        texts.extend(item['readable_text'] for item in outputs)
    # Every complete text is in its verified UTF-8 sidecar. Keep the model's
    # preview bounded and explicitly preserve pagination metadata.
    budget = 32000
    for item in texts:
        text = item.get('text', '')
        item['text'] = text[:budget]
        budget -= len(item['text'])
        if len(item['text']) < item.get('text_total_chars', len(text)):
            item.update(text_truncated=True, text_next_offset=len(item['text']))
    return AgentToolResult(status='success' if refs else 'error', summary=(
        '文字已提取并提供全文下载' if not failures else '已保存可用文字，部分输入仍需处理'),
        data={'readable_text': texts, 'failures': failures, 'requested_assets': inputs},
        result_refs=refs, artifact_refs=artifacts, job_refs=jobs,
        **({'error': {'code': 'extraction_failed', 'message': '没有可用文字结果'}} if not refs else {})).model_dump(mode='json')


async def clean_files(args, context):
    op = Operations(context)
    inputs = selected(context, [item.asset_id for item in args.files])
    refs, artifacts, results, failures, jobs = [], [], [], [], []
    from packages.contracts.data_operations import TrimColumns
    steps, columns = [], []
    for step in args.steps:
        if step.operator in {'trim', 'trim_columns'}:
            columns.extend(step.columns if step.operator == 'trim_columns' else [step.column])
        else:
            if columns:
                steps.append(TrimColumns(operator='trim_columns', columns=list(dict.fromkeys(columns))))
                columns = []
            steps.append(step)
    if columns:
        steps.append(TrimColumns(operator='trim_columns', columns=list(dict.fromkeys(columns))))
    for file, asset in zip(args.files, inputs, strict=True):
        op.namespace = 'file:' + file.asset_id
        file_jobs = []
        checked = DataInspect(asset_id=asset['asset_id'], asset_revision=asset['revision'],
                              blob_hash=asset['blob_hash'], options=file.options)
        inspected = await op.typed('data_inspect_input', DataInspect, data.inspect, checked.model_dump(), replay=True)
        if inspected['status'] != 'success' or inspected.get('data', {}).get('requires_sheet_selection'):
            failures.append({'asset_id': file.asset_id, 'reason': '请明确工作表' if inspected['status'] == 'success' else inspected['summary']})
            continue
        created = op.require_success(await op.typed('data_create_session', data.DataCreate, data.create,
            {'name': (asset['name'] + ' 清洗')[:100]}, replay=True))['data']
        session_id = created['data_session_id']
        def write_args():
            return {'data_session_id': session_id, 'expected_version': context.services.data_workbench.get(session_id)['version'], 'side': 'left'}
        # Versions are part of immutable native intents. Recover the existing
        # child's original arguments instead of inventing a newer version.
        async def write(name, schema, handler, payload, step_index=None):
            op.namespace = 'file:' + file.asset_id + (':step:' + str(step_index) if step_index is not None else '')
            prefix = {'data_session_id': session_id, 'side': 'left', **payload, '_composition_key': op.namespace}
            previous = context.store.rows('SELECT arguments FROM operations WHERE run_id=? AND tool=? AND parent_id=? ORDER BY created',
                                          (context.run_id, name, context.operation_id))
            import json
            match = next((json.loads(row['arguments']) for row in previous
                          if all(json.loads(row['arguments']).get(key) == value for key, value in prefix.items())), None)
            arguments = {key: value for key, value in match.items() if key not in {'parent_operation_id', '_composition_key'}} if match else {**write_args(), **payload}
            result = await op.typed(name, schema, handler, arguments, lookup=data.lookup)
            file_jobs.extend(result.get('job_refs', []))
            return result
        imported = await write('data_import', data.DataImport, data.import_data, checked.model_dump())
        result = imported
        if result['status'] == 'success':
            for step_index, step in enumerate(steps):
                result = await write('data_apply_step', data.DataStep, data.apply_step, {'step': step.model_dump(exclude_none=True)}, step_index)
                if result['status'] != 'success':
                    break
        if result['status'] == 'success':
            op.namespace = 'file:' + file.asset_id
            table = op.require_success(await op.typed('data_read_table', data.DataTable, data.read_table,
                {'data_session_id': session_id, 'side': 'left', 'limit': 5}, replay=True))
            result = await write('data_export', data.DataExport, data.export, {})
            if result['status'] == 'success':
                from .cleaned_downloads import present, downloads
                presented = op.require_success(await op.call('data_publish_cleaned', {
                    'bundle_id': result['data']['bundle']['bundle_id'], 'asset': asset,
                    'columns': table['data']['metadata']['columns'], 'row_count': table['data']['total'],
                    'table_reference': table['result_refs'][0]}, present, replay=True))
                refs.extend(table['result_refs'])
                artifacts.extend(presented['artifact_refs'])
                jobs.extend(file_jobs)
                bundle = presented['data']['bundle']
                file_downloads = downloads(context, bundle)
                item = {'asset_id': file.asset_id, 'name': asset['name'], 'data_session_id': session_id,
                        'row_count': table['data']['total'], 'business_column_count': len(table['data']['metadata']['columns']),
                        'columns': [{key: column[key] for key in ('column_id', 'header')} for column in table['data']['metadata']['columns']],
                        'preview': [[cell['value'] for cell in row['cells']] for row in table['data']['rows'][:2]],
                        'artifact_refs': presented['artifact_refs'], 'downloads': file_downloads,
                        'bundle': {'bundle_id': bundle['bundle_id'], 'files': file_downloads}}
                results.append(item)
                context.store.public_event(context.run_id, context.generation,
                    context.operation_id + ':file:' + file.asset_id, 'delivery', {'status': 'available',
                        'content': asset['name'] + ' 已完成', 'delivery': presented})
        if result['status'] != 'success':
            failures.append({'asset_id': file.asset_id, 'data_session_id': session_id, 'reason': result['summary'],
                             'error': result.get('error')})
    return AgentToolResult(status='success' if results else 'error', summary=f'已完成{len(results)}份，待处理{len(failures)}份',
        data={'files': results, 'failures': failures, 'requested_count': len(args.files)},
        result_refs=refs, artifact_refs=artifacts, job_refs=jobs,
        **({'error': {'code': 'cleaning_failed', 'message': '没有完成的文件'}} if not results else {})).model_dump(mode='json')


def register(registry):
    from workbench_platform.agent.registry import ToolSpec
    registry.register(ToolSpec('extract_text', '提取所选图片或PDF文字；一次完成准备、原生等待、全文读取和TXT导出。PDF可指定页；未指定则读取全文。返回真实来源和可下载全文，模型无需搬运内部ID。',
        ExtractText, extract_text, effect='derive', idempotent=True, availability=lambda services: {'state': 'ready'}))
    registry.register(ToolSpec('data_clean_files', '按已有Data算子批量清洗文件，内部完成导入、步骤、等待、读回和逐文件导出。trim_columns一次处理多列；只改变指定列，保留其余原值和来源。files需真实asset_id；多工作表需options.sheet_name。返回成功文件与失败项。',
        CleanFiles, clean_files, effect='derive', idempotent=True, availability=data.available))

    from .native import availability as native_availability
    from .composed_business import ClassificationPredict, CompareFiles, classification_predict, data_compare_files
    registry.register(ToolSpec('classification_predict', '使用指定或唯一活动的已有模型预测所选图片；内部完成准备、固定输入、等待、读回和导出。无需内部版本或资料集ID；不训练、不修改人工标签。失败返回原操作续办入口。',
        ClassificationPredict, classification_predict, effect='derive', idempotent=True,
        lookup_operation=classification_predict, availability=lambda services: native_availability(services, 'classification')))
    registry.register(ToolSpec('data_compare_files', '按明确条件核对两份文件；内部完成导入、清洗、比对、读回与导出。列配对接受实际列ID或唯一表头，amount_pairs自动精确转Decimal；steps使用列ID。只有多工作表或重复列名需澄清。',
        CompareFiles, data_compare_files, effect='derive', idempotent=True,
        lookup_operation=data_compare_files, availability=data.available))
