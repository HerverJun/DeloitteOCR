"""Agent registration and compatibility exports for shared classification operations."""
from workbench_platform.application_services.operations.classification import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    definitions = [
        ('classification_prepare', ClassificationPrepare, prepare_tool, '自动准备分类服务，读取当前项目的模型和标签；新项目需要明确类别且没有训练模型。', 'derive'),
        ('classification_infer', ClassificationInfer, infer, '对固定图片资料集使用已查询模型推理，auto_accept为false，绝不修改人工标签。', 'derive'),
        ('classification_read_results', ClassificationRead, read, '分页读取预测、置信度、人工标签与失败覆盖，不能把预测称为人工确认。', 'read'),
        ('classification_export', ClassificationExport, export, '归集已完成分类推理的真实成果文件。', 'derive'),
        ('assets_select_from_results', SelectPredictions, select, '按分类预测标签/分数/状态筛选资料，保留模型、条件、结果哈希与父集合来源，供OCR继续使用。', 'derive'),
    ]
    for name, schema, handler, description, effect in definitions:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'classification'),
            lookup_operation=lookup if name == 'classification_infer' else None,
            input_kinds=('asset_set', 'result'), output_kinds=('job', 'result', 'asset_set', 'artifact')))
