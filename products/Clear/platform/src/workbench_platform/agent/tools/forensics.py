"""Agent registration and compatibility exports for shared forensics operations."""
from workbench_platform.application_services.operations.forensics import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('forensics_inspect', Inspect, inspect, '对图片选择已发布六算法metadata/noise/frequency/jpeg/jpeg_grid/copy_move；JPEG算法在PNG上会说明不适用；没有硬取消。', 'derive'),
        ('forensics_read_report', Report, read, '读取报告、热图清单、输入哈希和算法限制，不作机器真伪裁定。', 'read'),
        ('forensics_collect', Report, collect, '将真实取证报告和热图归集为可下载成果，不要求人工审计流程。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=availability, lookup_operation=lookup if name == 'forensics_inspect' else None,
            input_kinds=('asset',), output_kinds=('job', 'result', 'artifact')))
