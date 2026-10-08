"""Workspace discovery, clarification and verified delivery tools."""
from dataclasses import asdict
import json
from urllib.parse import quote

from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from packages.contracts.models import AssetSetItem, SourceLocator
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import digest


class Empty(AgentModel):
    pass


class ToolQuery(AgentModel):
    keyword: str = Field(default='', max_length=100)
    family: str = Field(default='', max_length=80)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=30)


class ToolDescribe(AgentModel):
    names: list[str] = Field(min_length=1, max_length=15)


class AssetSearch(AgentModel):
    keyword: str = Field(default='', max_length=100)
    media_type: str = Field(default='', max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class AssetDescribe(AgentModel):
    asset_ids: list[Identity] = Field(min_length=1, max_length=50)


class AssetSetCreate(AgentModel):
    asset_ids: list[Identity] = Field(default_factory=list,max_length=1000)
    selected_scope: bool = False


class AskUser(AgentModel):
    question: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=10)


class Delivery(AgentModel):
    summary: str = Field(min_length=1, max_length=2000)
    artifact_refs: list[AgentRef] = Field(default_factory=list, max_length=50)
    result_refs: list[AgentRef] = Field(default_factory=list, max_length=50)
    allow_partial_screen_batch: bool = False


class ArtifactGet(AgentModel):
    bundle_id: Identity


def safe_asset(asset):
    return {key: value for key, value in asset.items() if key not in {'source', 'source_id'}}


def context(args, ctx):
    from workbench_platform.agent.scope import scope_summary
    project = ctx.services.workspace_store.get_workspace(ctx.workspace_id)
    registry = ctx.services.asset_library[0]
    selected = [safe_asset(registry.describe(ctx.workspace_id, asset_id)) for asset_id in ctx.selected_assets[:10]]
    data = {'workspace': asdict(project), **scope_summary(ctx), 'selected_assets': selected,
            'initial_refs': ctx.store.session(ctx.session_id).get('initial_refs', []),
            'data_sessions': ctx.services.data_workbench.list_page(ctx.workspace_id, limit=20)['items'],
            'capabilities': [ctx.services.agent.registry.describe(name, ctx.services)['availability'] |
                             {'name': name} for name in ctx.services.agent.registry.tools]}
    return AgentToolResult(status='success', summary='已读取当前项目和明确选中的资料范围', data=data).model_dump()


def list_tools(args, ctx):
    page = ctx.services.agent.registry.catalog(keyword=args.keyword, family=args.family,
        offset=args.offset, limit=args.limit, services=ctx.services)
    return AgentToolResult(status='success', summary='已读取工具目录；需要调用时用 tools_describe 加载指定名称',
        data={key: value for key, value in page.items() if key != 'next_offset'},
        next_offset=page['next_offset']).model_dump()


async def describe_tools(args, ctx):
    # The actual input schema is included once, in the next native API tools
    # field. Output schema is enforced by the dispatcher rather than duplicated
    # in every subsequent historical tool message.
    tools = []
    registry = ctx.services.agent.registry
    for name in dict.fromkeys(args.names):
        if name not in registry.tools:
            tools.append({'name': name, 'allowed': False, 'loaded': False,
                          'error': {'code': 'unknown_tool', 'message': '工具未注册'}})
            continue
        item = registry.describe(name, ctx.services)
        tools.append({key: value for key, value in item.items()
                      if key not in {'input_schema', 'output_schema', 'examples'}})
    admit = getattr(ctx.services.agent, 'admit_tools', None)
    if admit is None:
        return AgentToolResult(status='error', summary='工具契约尚未加载：当前调用没有可核实的运行时预算',
            data={'requested_tools': args.names, 'tools': tools, 'activated_tools': []},
            error={'code': 'activation_context_unavailable', 'message': '请在当前 Agent 运行中按需加载工具'}).model_dump()
    return await admit(ctx, args.names, tools)


def models(args, ctx):
    view = ctx.services.agent.connection.view()
    items = [{'id': view.get('model', ''), 'role': 'controller', 'tool_family': 'agent',
              'workspace_id': ctx.workspace_id, 'input_kinds': ['messages', 'tool_results'],
              'supported_tasks': ['tool_selection'] if view.get('available') else [],
              'version_ref': {'connection_revision': view.get('revision', 0), 'model': view.get('model')},
              'device': 'configured_endpoint', 'limits': ['仅通过该连接原生工具探针的配置可用'],
              'ready': view.get('available', False), 'protocol_tested': view.get('capabilities', {}),
              'reason': view.get('reason', '尚未配置主控')}]
    projection = getattr(ctx.services.agent, 'model_projection', None)
    if projection:
        items.extend(projection(ctx))
    return AgentToolResult(status='success', summary='模型角色与就绪状态来自实际配置及原生服务', data={'models': items}).model_dump()


def search(args, ctx):
    registry = ctx.services.asset_library[0]
    # The selected scope is applied before pagination, never after returning a
    # page. A bounded input list (<=100) needs no unbounded workspace scan.
    if ctx.selected_assets:
        values = [registry.describe(ctx.workspace_id, identifier) for identifier in ctx.selected_assets]
        values = [value for value in values if args.keyword.casefold() in value['original_name'].casefold()
                  and (not args.media_type or value['media_type'] == args.media_type)]
        items, total = values[args.offset:args.offset + args.limit], len(values)
    else:
        items, total = registry.list(ctx.workspace_id, keyword=args.keyword, media_type=args.media_type,
                                    offset=args.offset, limit=args.limit)
    return AgentToolResult(status='success', summary='已查询当前范围的真实资料',
        data={'items': [safe_asset(value) for value in items], 'total': total},
        next_offset=args.offset + args.limit if args.offset + args.limit < total else None).model_dump()


def describe_assets(args, ctx):
    from workbench_platform.application_services.references import asset_allowed
    if any(not asset_allowed(ctx, identifier) for identifier in args.asset_ids):
        raise ValueError('资料超出选中范围')
    return AgentToolResult(status='success', summary='已读取所选资料版本',
        data={'items': [safe_asset(ctx.services.asset_library[0].describe(ctx.workspace_id, identifier))
                        for identifier in args.asset_ids]}).model_dump()


def create_set(args, ctx):
    from workbench_platform.agent.scope import fixed_scope,set_summary
    if args.selected_scope:
        if args.asset_ids:
            raise ValueError('固定选中范围不需要重复传入图片清单')
        asset_set=fixed_scope(ctx)
        if asset_set is None:
            raise ValueError('当前任务尚未选择资料，请使用已固定的输入集合或选择范围')
        return AgentToolResult(status='success',summary='已固定当前选中资料范围，后续使用集合引用',
            data={key:value for key,value in set_summary(asset_set).items() if value is not None},
            result_refs=[AgentRef(kind='asset_set',workspace_id=ctx.workspace_id,object_id=asset_set.asset_set_id,revision=asset_set.revision,sha256=asset_set.manifest_hash)]).model_dump()
    from workbench_platform.application_services.references import asset_allowed
    if not args.asset_ids or len(set(args.asset_ids))!=len(args.asset_ids) or any(not asset_allowed(ctx,identifier) for identifier in args.asset_ids):
        raise ValueError('请选择范围内不重复的资料')
    registry, sets, _ = ctx.services.asset_library
    items = []
    for identifier in args.asset_ids:
        asset = registry.get(ctx.workspace_id, identifier)
        locator = SourceLocator(schema_version='1.0', kind='file', asset_id=identifier,
                                asset_revision=asset.revision, blob_hash=asset.blob_hash, precision='unknown')
        items.append(AssetSetItem(schema_version='1.0', item_id='i' + digest([ctx.operation_id, identifier])[:32],
            asset_id=identifier, asset_revision=asset.revision, blob_hash=asset.blob_hash, source_locator=locator))
    asset_set = sets.create(ctx.workspace_id, items, asset_set_id='s' + ctx.operation_id[1:])
    return AgentToolResult(status='success', summary='已固定不可变资料集', data={key:value for key,value in set_summary(asset_set).items() if value is not None},
        result_refs=[AgentRef(kind='asset_set', workspace_id=ctx.workspace_id, object_id=asset_set.asset_set_id,
                             revision=asset_set.revision, sha256=asset_set.manifest_hash)]).model_dump()


def ask_user(args, ctx):
    with ctx.store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO decisions(id,run_id,question,revision) VALUES(?,?,?,1)',
                   (ctx.operation_id, ctx.run_id, args.question))
    return AgentToolResult(status='needs_user', summary=args.question,
        data={'decision_id': ctx.operation_id, 'revision': 1, 'options': args.options}).model_dump()


def get_artifact(args, ctx):
    bundle = ctx.services.artifact_center.get(ctx.workspace_id, args.bundle_id)
    bounded={key:value for key,value in bundle.items() if key not in {'inputs','items'}}
    bounded.update(input_count=len(bundle.get('inputs',[])),result_count=len(bundle.get('items',[])))
    batches = getattr(ctx.services, 'screen_batches', None)
    if batches:
        bounded['freshness'] = batches.bundle_freshness(ctx.workspace_id, args.bundle_id)
    return AgentToolResult(status='success', summary='已读取真实成果', data=bounded,
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=args.bundle_id, revision=1)]).model_dump()


def artifacts(args, ctx):
    return AgentToolResult(status='success', summary='已读取当前项目成果目录',
        data=ctx.services.artifact_center.list(ctx.workspace_id, args.offset, args.limit)).model_dump()


def screen_delivery_summary(coverage):
    lines = []
    for index, item in enumerate(coverage[:12], 1):
        prefix = f'第{index}份成果：' if len(coverage) > 1 else ''
        line = f"{prefix}范围共{item['total']}张图片，已导出{item['file_count']}个XLSX。"
        if item['total'] != item['file_count']:
            line += f"{item['total'] - item['file_count']}项未生成XLSX，原因见ZIP清单。"
        if item.get('unexplained_without_file'):
            line += f"其中{item['unexplained_without_file']}项缺少明确原因，需检查。"
        line += f"待复核{item['needs_review']}项。"
        if item['pending']:
            line += f"仍在处理{item['pending']}项。"
        if item['failed']:
            line += f"失败{item['failed']}项。"
        if item['cancelled']:
            line += f"取消{item['cancelled']}项。"
        lines.append(line)
    if len(coverage) > 12:
        lines.append(f'另有{len(coverage) - 12}份拍屏成果，完整范围见各ZIP清单。')
    return '\n'.join(lines)


def delivery(args, ctx):
    if not args.artifact_refs and not args.result_refs:
        raise ValueError('交付必须引用实际结果或成果')
    published = [];screen_coverage=[]
    from workbench_platform.application_services.references import verify
    for reference in args.artifact_refs:
        if reference.workspace_id != ctx.workspace_id or reference.kind != 'artifact':
            raise ValueError('跨项目成果')
        bundle = verify(reference.model_dump(), ctx)
        files = []
        for file in bundle['files']:
            artifact = ctx.services.artifact_center.file(ctx.workspace_id, bundle['bundle_id'], file['name'])
            # Verify actual bytes, rather than equating metadata with delivery.
            with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash):
                pass
            files.append({**file, 'download_url': f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/'
                f'{bundle["bundle_id"]}/files/{quote(file["name"])}'})
        published.append({'bundle_id': bundle['bundle_id'], 'title': bundle.get('title', '成果'), 'files': files})
        items=bundle.get('items',[])
        if reference.application=='ocr' and reference.parent_id and bundle.get('handoff_id')==reference.parent_id and items and all(
            isinstance(item,dict) and item.get('input_version_id') for item in items):
            freshness = ctx.services.screen_batches.export_freshness(ctx.workspace_id, reference.parent_id, bundle['bundle_id'])
            if not freshness['current']:
                raise ValueError('该 ZIP 是历史成果，当前结果已变化；请重新导出后交付')
            screen_coverage.append({'batch_id':reference.parent_id,'bundle_id':bundle['bundle_id'],'total':len(items),
                'current_verified': True, 'snapshot_id': freshness['current_snapshot_id'],
                'export_options': freshness['export_options'],
                'pending':sum(item.get('status') not in {'completed','needs_review','failed','cancelled'} for item in items),
                'file_count':sum(bool(item.get('file_name')) for item in items),
                'settled_without_file':sum(item.get('status') in {'completed','needs_review'} and not item.get('file_name')
                                           and bool(str(item.get('export_error') or '').strip()) for item in items),
                'unexplained_without_file':sum(item.get('status') in {'completed','needs_review'} and not item.get('file_name')
                                               and not str(item.get('export_error') or '').strip() for item in items),
                'needs_review':sum(item.get('status') == 'needs_review' for item in items),
                'failed':sum(item.get('status') == 'failed' for item in items),
                'cancelled':sum(item.get('status') == 'cancelled' for item in items)})
    for reference in args.result_refs:
        verify(reference.model_dump(), ctx)
    calls = ctx.store.rows('SELECT result FROM operations WHERE run_id=? AND result IS NOT NULL', (ctx.run_id,))
    results = [json.loads(row['result']) for row in calls]
    outstanding = [value for value in results if value['status'] in {'pending', 'needs_verification'}]
    if outstanding:
        from workbench_platform.agent.graph import screen_pending
        eligible = args.allow_partial_screen_batch and all(value['status'] == 'pending' and screen_pending(value)
                                                          for value in outstanding)
        batch_ids = {ref['object_id'] for value in outstanding for ref in value.get('job_refs', [])}
        if not eligible or not any(ref.application == 'ocr' and ref.parent_id in batch_ids for ref in args.artifact_refs):
            raise ValueError('仍有未完成任务；仅可显式交付当前拍屏批次的已核实部分成果')
    failed = [value for value in results if value['status'] in {'error', 'unsupported'} or
              any('未处理区域' in warning or '提取失败' in warning for warning in value.get('warnings', [])) or
              any(isinstance(value.get('coverage', {}).get(key), int) and value['coverage'][key] > 0
                  for key in ('failed', 'cancelled', 'pending', 'needs_review'))]
    partial_screen = any(item['pending'] or item['needs_review'] or item['failed'] or item['cancelled'] or
                         item['file_count'] != item['total'] for item in screen_coverage)
    summary = screen_delivery_summary(screen_coverage) if (screen_coverage and not args.result_refs and
                len(screen_coverage) == len(args.artifact_refs)) else args.summary
    return AgentToolResult(status='success', summary=summary, artifact_refs=args.artifact_refs,
        result_refs=args.result_refs, data={'delivery': published, 'completeness': 'partial' if failed or outstanding or partial_screen else 'complete',
        'verified': True, 'limitations': [value['summary'] for value in [*failed, *outstanding]],
        'screen_batch_coverage':screen_coverage,
        'continuing_job_refs': [ref for value in outstanding for ref in value.get('job_refs', [])]}).model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('workspace_get_context', Empty, context, '读取当前项目、已选资料及作业范围；不猜测对象ID。', 'read'),
        ('tools_list', ToolQuery, list_tools, '分页检索已注册工具的轻量目录；发现不会加载契约，调用前用tools_describe。', 'read'),
        ('tools_describe', ToolDescribe, describe_tools, '显式加载指定工具，下一轮可调用；完整输入契约仅在原生tools字段提供。', 'read'),
        ('models_list', Empty, models, '读取主控、OCR引擎与分类模型的角色和实际就绪状态。', 'read'),
        ('assets_search', AssetSearch, search, '在当前项目或明确已选资料中有界查询文件。', 'read'),
        ('assets_describe', AssetDescribe, describe_assets, '读取实际资料ID、版本和内容哈希，不返回本机路径。', 'read'),
        ('assets_create_set', AssetSetCreate, create_set, '将当前范围资料固定为不可变输入集，不修改人工标签。', 'derive'),
        ('ask_user', AskUser, ask_user, '仅在工作表、业务键等会实质改变结果的歧义时提问；回复续接原会话。', 'derive'),
        ('artifacts_list', AssetSearch, artifacts, '读取当前项目已有真实成果目录。', 'read'),
        ('artifacts_get', ArtifactGet, get_artifact, '读取实际成果及文件元数据。', 'read'),
        ('delivery_publish', Delivery, delivery, '核实真实结果和下载文件后交付，未完成任务不可报完整成功。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True))
