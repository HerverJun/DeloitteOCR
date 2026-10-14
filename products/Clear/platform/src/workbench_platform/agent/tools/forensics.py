"""Six released local forensic algorithms with explicit limits and verified files."""
from contextlib import closing
import json
from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity, Hash
from packages.contracts.forensics import Tool
from workbench_platform.agent.registry import ToolSpec


class Inspect(AgentModel):
    asset_id: Identity
    asset_revision: int = Field(ge=1)
    blob_hash: Hash
    tools: list[Tool] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.tools)) != len(self.tools):
            raise ValueError('重复算法')
        return self


class Report(AgentModel):
    inspection_id: Identity


def availability(services):
    service = getattr(services, 'forensic_service', None)
    return {'state': 'ready' if service and service.python.is_file() else 'unavailable',
            'reason': '' if service and service.python.is_file() else '取证工作进程未配置',
            'hard_cancel': False}


def inspect(args, ctx):
    if ctx.selected_assets and args.asset_id not in ctx.selected_assets:
        raise ValueError('资料超出会话范围')
    value = ctx.services.forensic_service.create(ctx.workspace_id,
        {'schema_version': '1.0', 'request_id': ctx.operation_id, **args.model_dump()})
    return AgentToolResult(status='pending', summary='已提交本地取证算法；报告仅呈现线索，不裁定真伪',
        data={'inspection_id': value['inspection_id'], 'hard_cancel': False},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=value['inspection_id'],
            revision=1, application='forensics', parent_id=value['inspection_id'])]).model_dump()


def lookup(args, ctx):
    service = ctx.services.forensic_service
    with closing(service.db()) as db:
        row = db.execute('SELECT body FROM inspections WHERE workspace_id=? AND request_id=?',
                         (ctx.workspace_id, ctx.operation_id)).fetchone()
    return inspect(args, ctx) if row else None


def read(args, ctx):
    service = ctx.services.forensic_service
    value = service.get(ctx.workspace_id, args.inspection_id)
    if ctx.selected_assets and value['asset_id'] not in ctx.selected_assets:
        raise ValueError('资料超出会话范围')
    if value['status'] == 'needs_verification':
        return AgentToolResult(status='needs_verification', summary='取证任务曾中断，需要核实并明确恢复',
            data={'inspection_id': args.inspection_id, 'hard_cancel': False}).model_dump()
    if value['status'] == 'failed':
        return AgentToolResult(status='error', summary=value['error'] or '取证失败',
            error={'code': 'forensics_failed', 'message': value['error'] or '取证失败'}).model_dump()
    if value['status'] not in {'succeeded', 'partial'}:
        raise ValueError('报告未生成')
    content, _ = service.file(ctx.workspace_id, args.inspection_id, 'report.json')
    report = json.loads(content)
    if report['verdict'] is not None:
        raise ValueError('报告语义不符合取证限制')
    return AgentToolResult(status='success', summary='已读取有来源的取证线索与算法限制，不能作为真伪裁定',
        data={'inspection_id': args.inspection_id, 'report': report, 'files': value['files'], 'hard_cancel': False},
        result_refs=[AgentRef(kind='result', workspace_id=ctx.workspace_id, object_id=args.inspection_id,
            revision=value['revision'], sha256=value['report_hash'], application='forensics',
            sources=[AgentRef(kind='asset', workspace_id=ctx.workspace_id, object_id=value['asset_id'],
                revision=value['asset_revision'], sha256=value['blob_hash'])])],
        warnings=['算法输出为人工复核线索；不自动断言真伪'] + ([value['error']] if value['error'] else []),
        coverage={'succeeded': sum(item['status'] != 'error' for item in report['evidence']),
                  'failed': sum(item['status'] == 'error' for item in report['evidence'])}).model_dump()


def collect(args, ctx):
    read(args, ctx)
    from workbench_platform.forensics.artifacts import publish
    bundle = publish(ctx.services.forensic_service, ctx.workspace_id, args.inspection_id)
    return AgentToolResult(status='success', summary='已归集原始报告和热图，可下载人工复核', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'],
            revision=1, application='forensics', parent_id=args.inspection_id)]).model_dump()


def settle(ref, ctx):
    value = ctx.services.forensic_service.get(ctx.workspace_id, ref['object_id'])
    if value['status'] in {'queued', 'running'}:
        return None
    return read(Report(inspection_id=ref['object_id']), ctx)


def register(registry):
    for name, schema, handler, description, effect in [
        ('forensics_inspect', Inspect, inspect, '对图片选择已发布六算法metadata/noise/frequency/jpeg/jpeg_grid/copy_move；JPEG算法在PNG上会说明不适用；没有硬取消。', 'derive'),
        ('forensics_read_report', Report, read, '读取报告、热图清单、输入哈希和算法限制，不作机器真伪裁定。', 'read'),
        ('forensics_collect', Report, collect, '将真实取证报告和热图归集为可下载成果，不要求人工审计流程。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=availability, lookup_operation=lookup if name == 'forensics_inspect' else None,
            input_kinds=('asset',), output_kinds=('job', 'result', 'artifact')))
