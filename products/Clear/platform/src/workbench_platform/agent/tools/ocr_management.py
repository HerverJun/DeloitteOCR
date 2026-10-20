"""Agent registration and compatibility exports for shared ocr_management operations."""
from workbench_platform.application_services.operations.ocr_management import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_edit_result', Edit, edit, '按固定OCR引用的版本逐项修改文字/单元格，before必须吻合，不重录整表；返回新引用。', 'versioned'),
        ('ocr_adopt_result', Adopt, adopt, '采用明确OCR结果修订，记录AI操作人；不冒签人工复核。', 'versioned'),
        ('ocr_read_history', Page, history, '分页读取原生OCR编辑历史与修订目录。', 'read'),
        ('ocr_list_visual_models', Models, models, '查询独立专业视觉服务的本地/外部模型目录，凭据沿用该服务已配置连接。', 'read'),
        ('ocr_visual_review', Visual, visual, '调用已查询且可用的专业视觉模型阅读现有采用OCR结果，返回真实建议；外部模型会通过原生连接发送图像。', 'derive'),
        ('ocr_read_visual_review', Page, read_visual, '分页读取专业视觉建议、证据及过期状态，可选择显式文字编辑。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            lookup_operation={'ocr_visual_review': visual_lookup, 'ocr_edit_result': edit_lookup, 'ocr_adopt_result': adopt_lookup}.get(name),
            availability=lambda services: availability(services, 'ocr')))
