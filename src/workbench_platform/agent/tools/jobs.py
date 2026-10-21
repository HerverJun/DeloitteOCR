"""Agent registration and compatibility exports for shared jobs operations."""
from workbench_platform.application_services.operations.jobs import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('jobs_get', JobGet, get, '读取当前会话真实任务状态。等待由运行时观察器完成，不要循环轮询。', 'read'),
        ('jobs_cancel_owned', JobGet, cancel, '只取消当前会话创建且原生支持取消的任务；取证无硬取消。', 'control'),
        ('jobs_retry_failed', JobRetry, retry, '只处理可核实失败输入，排除成功项；可根据实际错误指定新引擎或模型，保留父attempt。PDF使用显式页工具。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            lookup_operation=lookup_retry if name == 'jobs_retry_failed' else None,
            input_kinds=('job',), output_kinds=('job', 'asset_set')))
