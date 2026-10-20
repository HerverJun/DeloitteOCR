"""Agent registration and compatibility exports for shared data operations."""
from workbench_platform.application_services.operations.data import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('data_create_session', DataCreate, create, '在当前项目创建幂等数据作业，返回真实作业 ID 和版本。', 'derive'),
        ('data_inspect_input', DataInspect, inspect, '导入前探查资料的工作表、列与最多5行样本。多个工作表应确认选择。', 'read'),
        ('data_import', DataImport, import_data, '将当前项目的已选资料按明确工作表和表头导入左右表；需当前作业版本。', 'versioned'),
        ('data_read_table', DataTable, read_table, '读取固定业务表分页、列ID、类型与来源行，每页最多50行。table_schema区分business_columns与source_locator_columns，并给业务列数/导出列数/总行数；source_row是原始行号定位，不属于业务列。', 'read'),
        ('data_apply_step', DataStep, apply_step, '按真实列ID清洗、duplicates检测、filter_compare类型化大小/区间筛选、group_summary分组计数与精确Decimal金额合计。金额须先明确转decimal；未知金额会标错误，不生成部分合计。汇总保留全部source_rows。', 'versioned'),
        ('data_compare', DataCompare, compare, '按已查到的列ID进行精确比对。金额阈值用十进制字符串，保留编号和来源。', 'versioned'),
        ('data_read_result', DataResult, read_result, '读取比对结果的真实差异、覆盖和来源，每页最多50行。', 'read'),
        ('data_export', DataExport, export, '导出已有比对结果或清洗表为真实CSV/XLSX/Parquet。清洗表table_schema明确业务列与来源定位列：额外source_row为原始行号，不是新增业务列；汇总还保留source_rows_json。核对业务列数/导出列数应分别使用business_column_count/export_column_count。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect,
            availability=available, input_kinds=('asset', 'table', 'result'), output_kinds=('table', 'result', 'artifact'),
            resource_keys=lambda args, ctx: ('data:' + getattr(args, 'data_session_id', ctx.workspace_id),),
            lookup_operation=lookup if schema in (DataImport, DataStep, DataCompare, DataExport) else None,
            idempotent=True))
