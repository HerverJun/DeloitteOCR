"""Agent registration and compatibility exports for shared ocr_processing operations."""
from workbench_platform.application_services.operations.ocr_processing import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, model, handler, description, effect in [
        ('ocr_list_processing_options', Models, options, '查询既有旋转/对比度预处理参数及融合策略。预处理传给ocr_run。', 'read'),
        ('ocr_fuse_results', Fusion, submit, '将同一图片、版本和批次的原始多引擎结果交给既有CPU融合器，保存来源与冲突；不融合后续编辑值。', 'derive'),
        ('ocr_read_issues', Issues, issues, '分页读取既有OCR/融合结果的冲突及来源依据，可继续显式OCR编辑。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, model, handler, effect=effect, idempotent=True,
            lookup_operation=lookup if name == 'ocr_fuse_results' else None,
            availability=lambda services: availability(services, 'ocr')))
