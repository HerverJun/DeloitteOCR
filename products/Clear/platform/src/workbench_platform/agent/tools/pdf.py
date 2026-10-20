"""Agent registration and compatibility exports for shared pdf operations."""
from workbench_platform.application_services.operations.pdf import *
from workbench_platform.application_services.operations.pdf import ReadPage as BusinessReadPage, read as read_complete
from workbench_platform.artifacts.text_outputs import text_page
from workbench_platform.agent.registry import ToolSpec


class ReadPage(BusinessReadPage):
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=8000, ge=1, le=32000)


def read(args, context):
    value = read_complete(args, context)
    data = value.get('data', {})
    if data.get('has_result'):
        data.update(text_page(data['text'], offset=args.text_offset, limit=args.text_limit))
    return value


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_inspect_document', InspectDocument, inspect, '读取PDF固定输入的页数并创建新attempt。其它页集或重试需要新attempt。', 'derive'),
        ('ocr_process_pages', ProcessPages, process, '按真实PDF文档ID和显式页集调用native/ocr/auto原生流程；同请求去重，等待由运行时完成。', 'derive'),
        ('ocr_read_page', ReadPage, read, '读取指定PDF页的真实内容、表数和页来源；无表不能编造Excel。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'ocr'),
            lookup_operation=lookup if name == 'ocr_process_pages' else None,
            input_kinds=('asset_set',), output_kinds=('job', 'result')))
