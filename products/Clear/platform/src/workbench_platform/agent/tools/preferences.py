"""Structured defaults use real user instructions, never inferred agent habits."""
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.preferences import Business, PreferenceValues, PreferenceSource
from workbench_platform.agent.matching import readiness_projection


class Read(AgentModel):
    business: Business = 'project'


class Write(Read):
    expected_revision: int = Field(ge=0)
    values: PreferenceValues = Field(default_factory=PreferenceValues)
    revoke_keys: list[str] = Field(default_factory=list, max_length=20)
    source: PreferenceSource


class Capabilities(AgentModel):
    names: list[str] = Field(default_factory=list, max_length=50)


def read(args, ctx):
    service = ctx.services.agent.preferences
    return AgentToolResult(status='success', summary='已读取当前业务默认及来源；当前任务沿用固定快照',
        data={**service.preview(ctx.workspace_id, args.business),
              **service.snapshots(ctx.workspace_id, ctx.session_id, ctx.run_id)}).model_dump()


def write(args, ctx):
    data = ctx.services.agent.preferences.update(ctx.workspace_id, args.business, args.expected_revision,
        args.values, args.revoke_keys, args.source, ctx.operation_id, context=ctx)
    return AgentToolResult(status='success', summary='已按真实用户保存默认指令更新偏好，当前冻结任务保持原快照',
                           data=data).model_dump()


def capabilities(args, ctx):
    return AgentToolResult(status='success', summary='已区分工具注册、服务状态和具体模型目录资格；未执行模型',
        data=readiness_projection(ctx, names=args.names or None)).model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('preferences_read', Read, read, '查看项目/业务默认、原文来源、版本与当前任务固定快照。默认修改只影响后续任务。', 'read'),
        ('preferences_update', Write, write, '仅按当前会话真实用户明确“默认/以后/记住”指令保存或撤回偏好；需event_seq与原文quote；不能自动保存单次参数。', 'versioned'),
        ('capabilities_read', Capabilities, capabilities, '分别核实注册工具、可启动/运行服务、具体模型目录状态；不启动服务或执行模型，目录就绪不代替运行时文件/GPU校验。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            resource_keys=lambda args, ctx: ('preferences:' + ctx.workspace_id,)))
