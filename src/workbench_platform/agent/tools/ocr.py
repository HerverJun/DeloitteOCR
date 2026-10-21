"""Agent registration and compatibility exports for shared ocr operations."""
from workbench_platform.application_services.operations.ocr import *
from workbench_platform.application_services.operations.ocr import OcrRead as BusinessOcrRead, read as read_complete
from workbench_platform.artifacts.text_outputs import text_page
from workbench_platform.agent.registry import ToolSpec


class OcrRead(BusinessOcrRead):
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=8000, ge=1, le=32000)


def read(args, context):
    value = read_complete(args, context)
    for item in value.get('data', {}).get('items', []):
        item.update(text_page(item['text'], offset=args.text_offset, limit=args.text_limit))
    return value


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_prepare', OcrPrepare, prepare_tool, '自动准备已安装OCR服务，复用当前范围项目，查询真实引擎目录。', 'derive'),
        ('ocr_run', OcrRun, run, '对固定图片集运行已查询引擎，可显式旋转/调整对比度；原图和处理版本保留。PDF需按页工具。', 'derive'),
        ('ocr_read_results', OcrRead, read, '分页读取真实OCR内容、结构表数量、结果版本与失败覆盖。推理和人工采用分别引用。', 'read'),
        ('ocr_export', OcrExport, export, '从已完成原生OCR结果生成可下载托管成果，不能伪造未识别的表格。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'ocr'),
            lookup_operation=lookup if name == 'ocr_run' else None,
            input_kinds=('asset_set',), output_kinds=('result', 'artifact', 'job'),
            resource_keys=lambda args, ctx: ('ocr:' + getattr(args, 'link_id', ctx.workspace_id),)))
