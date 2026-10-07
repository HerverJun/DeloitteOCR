"""Project catalogs and source-verified native result attachment."""
from dataclasses import replace
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentToolResult, Identity
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.application_services.native import scoped_set, attempt_set, availability
from workbench_platform.application_services.native_mapping import adapter_for, attach, binding, read
from workbench_platform.application_services.references import verify, asset_allowed


class Catalog(AgentModel):
    application: Literal['ocr', 'classification']
    link_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class Attach(AgentModel):
    application: Literal['ocr', 'classification']
    link_id: Identity
    object_id: Identity


class Read(AgentModel):
    binding_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class Import(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)


class Projects(AgentModel):
    application: Literal['ocr', 'classification']


class ProjectCreate(Projects):
    name: str = Field(min_length=1, max_length=100)
    categories: list[str] | None = Field(default=None, min_length=2, max_length=100)


def projects(args, ctx):
    from workbench_platform.jobs.ocr_projects import OcrProjects
    from workbench_platform.jobs.classification_projects import ClassificationProjects
    service = (OcrProjects if args.application == 'ocr' else ClassificationProjects)(ctx.services.job_service)
    return AgentToolResult(status='success', summary='已读取现有原生项目目录和平台关联', data=service.projects(ctx.workspace_id)).model_dump()


def create_project(args, ctx):
    from workbench_platform.jobs.ocr_projects import OcrProjects
    from workbench_platform.jobs.classification_projects import ClassificationProjects
    service = (OcrProjects if args.application == 'ocr' else ClassificationProjects)(ctx.services.job_service)
    if args.application == 'classification' and not args.categories:
        raise ValueError('新分类项目须明确至少2个类别')
    value = service.create(ctx.workspace_id, args.name, request_id=ctx.operation_id,
        **({'categories': args.categories} if args.application == 'classification' else {}))
    return AgentToolResult(status='success', summary='已创建并关联新的原生项目，可继续导入及使用既有工具', data=value).model_dump()


def catalog(args, ctx):
    adapter = adapter_for(ctx, args.application, args.link_id)
    if args.application == 'ocr':
        state = adapter.snapshot()
        images = {item['id']: item for item in state['images']}
        values = [{'kind': 'result', 'object_id': item['result_id'], 'image_id': item['image_id'], 'task_id': item['id'], 'status': item['status'],
                   'title': images[item['image_id']]['name']+' · 识别结果'}
                  for item in state['tasks'] if item.get('result_id')]
    else:
        # Native job enumeration is paginated independently of platform jobs.
        values = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/jobs?offset={args.offset}&limit={args.limit}')
        return AgentToolResult(status='success', summary='已读取关联原生项目任务目录，读取结果前须核实并回传来源',
            data={'items': [{key: item[key] for key in ('id', 'kind', 'state', 'created_at')} for item in values]},
            next_offset=args.offset+len(values) if len(values) == args.limit else None).model_dump()
    return AgentToolResult(status='success', summary='已读取关联原生项目结果目录，读取结果前须核实并回传来源',
        data={'items': values[args.offset:args.offset+args.limit], 'total': len(values)},
        next_offset=args.offset+args.limit if args.offset+args.limit < len(values) else None).model_dump()


def attach_result(args, ctx):
    # Discovery registers sources without granting them to this task.
    value = attach(replace(ctx, selected_assets=()), args.application, args.link_id, args.object_id)
    required = [source['asset_id'] for source in value['sources'].values() if not asset_allowed(ctx, source['asset_id'])]
    if required:
        return AgentToolResult(status='success', summary='已核实并登记原资料；尚未授权读取，请用户在手动返回候选中加入这些资料后继续',
            data={'binding_id': value['id'], 'required_assets': sorted(set(required))}).model_dump()
    ref = read(ctx, value)
    return AgentToolResult(status='success', summary='已按原件字节核实回传原生结果，可继续证据与结果转换',
        data={'binding_id': value['id']}, result_refs=[ref]).model_dump()


def read_result(args, ctx):
    value = binding(ctx, args.binding_id)
    if not value:
        raise ValueError('原生回传身份不存在')
    ref = read(ctx, value); checked = verify(ref.model_dump(), ctx)
    if value['application'] == 'ocr':
        edited = checked['snapshot']['edited']
        rows = [line[start:start+2000] for line in edited['text'].splitlines()
                for start in range(0, max(1, len(line)), 2000)]
        data = {'text_lines': rows[args.offset:args.offset+args.limit], 'table_count': len(checked['table_snapshot']['tables']), 'total': len(rows)}
    else:
        rows = checked['items']; data = {'items': rows[args.offset:args.offset+args.limit], 'total': len(rows), 'model_id': checked['model_id']}
    return AgentToolResult(status='success', summary='已读取核实后的原生结果', data=data, result_refs=[ref],
        next_offset=args.offset+args.limit if args.offset+args.limit < len(rows) else None).model_dump()


def import_classification(args, ctx):
    fixed = attempt_set(ctx, scoped_set(ctx, args.asset_set_id, args.asset_set_revision))
    value = ctx.services.classification_handoff.begin(ctx.workspace_id, args.link_id, fixed.asset_set_id, fixed.revision, {})
    return AgentToolResult(status='success', summary='已导入固定分类资料，可读取标注、发布训练版本或推理', data=value).model_dump()


def export_result(args, ctx):
    import csv
    import io
    from workbench_platform.agent.store import canonical
    from workbench_platform.artifacts.native_exports import publish
    from workbench_platform.audit.direct import source_locator
    from packages.contracts.agent import AgentRef
    value = binding(ctx, args.binding_id)
    if not value:
        raise ValueError('回传身份不存在')
    ref = read(ctx, value); checked = verify(ref.model_dump(), ctx)
    files = {'result.json': (canonical(checked).encode('utf-8'), 'application/json')}
    if value['application'] == 'ocr':
        adapter = adapter_for(ctx, 'ocr', value['link_id'])
        native = next(result for result in adapter.results(value['native_items']) if result.native_result_id == ref.object_id)
        payload, media_type = adapter.export_results([(native, ref.revision)])
        files['ocr-export.zip' if media_type == 'application/zip' else 'ocr-export.json'] = payload, media_type
    else:
        stream = io.StringIO(newline='')
        keys = ('asset_id', 'source_name', 'state', 'prediction_label_id', 'prediction_score', 'human_label_id', 'disposition_state')
        writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader()
        for item in checked['items']:
            # Spreadsheet-safe display export; result.json keeps raw literals.
            writer.writerow({key: "'"+str(item[key]) if isinstance(item[key], str) and item[key].startswith(('=', '+', '-', '@')) else item[key] for key in keys})
        files['predictions.csv'] = stream.getvalue().encode('utf-8-sig'), 'text/csv'
    inputs = list(value['sources'].values())
    locators = [source_locator(ctx.services.asset_library[0].get(ctx.workspace_id, item['asset_id'])) for item in inputs]
    bundle = publish(ctx, '原生回传 · '+value['application'], files, inputs, locators, str(ref.revision))
    return AgentToolResult(status='success', summary='已归集原生独立导入结果的实际导出文件与来源', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, application=value['application'], object_id=bundle['bundle_id'], revision=1)]).model_dump()


def export_lookup(args, ctx):
    from workbench_platform.artifacts.native_exports import lookup_result
    return lookup_result(ctx)


def catalog_availability(services):
    """Keep application-specific readiness visible before argument selection."""
    applications = {family: availability(services, family) for family in ('ocr', 'classification')}
    states = {value['state'] for value in applications.values()}
    if states == {'ready'}:
        state, reason = 'ready', ''
    elif states <= {'ready', 'preparable'}:
        state, reason = 'preparable', '未运行的目标应用须先调用ocr_prepare或classification_prepare'
    elif states == {'unavailable'}:
        state, reason = 'unavailable', 'OCR和分类原生服务均不可用'
    else:
        state, reason = 'conditional', '按目标应用查看applications状态；未运行须先prepare，不可用应用不能调用'
    return {'state': state, 'reason': reason, 'applications': applications}


def register(registry):
    for name, schema, handler, description, effect in [
        ('native_list_projects', Projects, projects, '读取现有OCR/分类项目目录与当前工作空间关联，先启动已安装服务。', 'read'),
        ('native_create_project', ProjectCreate, create_project, '创建并关联原生项目，新分类项目须明确类别，再通过固定输入导入、标注、训练或推理。', 'derive'),
        ('native_list_results', Catalog, catalog, '分页列出当前工作空间关联原生项目的既有结果/任务，包括工具内独立导入。', 'read'),
        ('native_attach_result', Attach, attach_result, '下载并校验原件哈希，将工具内独立导入结果接回平台；新资料须显式加入任务范围。', 'derive'),
        ('native_read_result', Read, read_result, '分页读取已完成原件回传并属于当前资料范围的原生结果。', 'read'),
        ('native_export_result', Read, export_result, '将原生独立导入结果归集为实际可下载OCR导出包或分类CSV/JSON，保留原件来源。', 'derive'),
        ('classification_import_assets', Import, import_classification, '将固定图片集导入分类项目，训练前可读取及保存标注，不要求已有推理模型。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=(lambda services: availability(services, 'classification'))
                if name == 'classification_import_assets' else catalog_availability,
            lookup_operation=export_lookup if name == 'native_export_result' else handler if name in {'native_attach_result', 'native_create_project', 'classification_import_assets'} else None))
