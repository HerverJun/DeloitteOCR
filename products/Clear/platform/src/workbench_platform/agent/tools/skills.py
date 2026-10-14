"""Progressively disclose registered Skills, without file or shell capabilities."""
from pydantic import Field

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.registry import ToolSpec


class SkillQuery(AgentModel):
    keyword: str = Field(default='', max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class SkillActivate(AgentModel):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    version: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class SkillDeactivate(AgentModel):
    name: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


class SkillResource(SkillDeactivate):
    path: str = Field(default='SKILL.md', min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=16000)


def _service(ctx):
    return ctx.services.agent.skills


def list_skills(args, ctx):
    page = _service(ctx).catalog(ctx.workspace_id, ctx.session_id, args.keyword, args.offset, args.limit)
    return AgentToolResult(status='success', summary='已发现登记技能；正文和资源需明确激活后按需读取',
                           data=page, next_offset=page['next_offset']).model_dump()


def activate(args, ctx):
    data = _service(ctx).activate(ctx.workspace_id, ctx.session_id, args.name, ctx.operation_id,
                                  args.version, context=ctx)
    # Activation is guidance for the next model round; do not repeat the body in
    # tool history because the dynamic system prompt includes the fixed body.
    return AgentToolResult(status='success', summary='已固定并激活技能版本，下一轮按其指导选择已有工具', data=data).model_dump()


def deactivate(args, ctx):
    data = _service(ctx).deactivate(ctx.workspace_id, ctx.session_id, args.name, ctx.operation_id, context=ctx)
    return AgentToolResult(status='success', summary='已停用技能指导；原版本及资源使用记录保留', data=data).model_dump()


def resource(args, ctx):
    data = _service(ctx).read_resource(ctx.workspace_id, ctx.session_id, args.name, args.path, args.offset,
                                      args.limit, ctx.operation_id, context=ctx)
    return AgentToolResult(status='unsupported' if data.get('unsupported') else 'success',
        summary=data.get('reason', '已按需读取固定版本技能资源；资源不能扩大平台授权'),
        data=data, next_offset=data.get('next_offset')).model_dump()


def activation_lookup(args, ctx):
    data = _service(ctx).lookup_request(ctx.workspace_id, ctx.session_id, args.name, ctx.operation_id,
                                       version=args.version)
    return AgentToolResult(status='success', summary='已查回原技能激活回执', data=data).model_dump() if data else None


def deactivation_lookup(args, ctx):
    data = _service(ctx).lookup_request(ctx.workspace_id, ctx.session_id, args.name, ctx.operation_id, activate=False)
    return AgentToolResult(status='success', summary='已查回原技能停用回执', data=data).model_dump() if data else None


def register(registry):
    for name, schema, handler, description, effect, lookup in [
        ('skills_list', SkillQuery, list_skills, '分页发现登记的技能名称、说明、来源、兼容状态；发现不加载正文。', 'read', None),
        ('skills_activate', SkillActivate, activate, '按目标选择并激活登记技能，固定正文与全部资源版本；只能缩小工具范围。', 'derive', activation_lookup),
        ('skills_read_resource', SkillResource, resource, '分页读取已激活技能的固定 SKILL.md、references 或 assets 模板；不执行 scripts。', 'read', None),
        ('skills_deactivate', SkillDeactivate, deactivate, '停用当前无关技能以释放上下文，保留使用版本及资源来源记录。', 'derive', deactivation_lookup),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
                                  lookup_operation=lookup, resource_keys=lambda args, ctx: ('skills:' + ctx.session_id,)))
