"""Read-only schema of business cells and their existing export locators."""
from __future__ import annotations


def cleaned_table_schema(columns, row_count, *, revision=None, has_source_rows=False, export_headers=None):
    if not isinstance(columns, list) or type(row_count) is not int or row_count < 0:
        raise ValueError('固定业务列目录或行数无效')
    business = [{'column_id': column['column_id'], 'header': column['header'],
                 'export_column': column['column_id'] + ' | ' + (column['header'] or column['column_id'])}
                for column in columns]
    locators = [{'name': 'source_row', 'kind': 'source_row',
                 'description': '原始输入资料的来源行号定位，含表头的CSV首条数据通常为2；不是业务数据列。'}]
    if has_source_rows:
        locators.append({'name': 'source_rows_json', 'kind': 'source_rows',
                         'description': '汇总行完整来源行集合的JSON；source_row为代表行定位，完整成员以此字段为准。'})
    headers = ['source_row', *[column['export_column'] for column in business]]
    if has_source_rows:
        headers.append('source_rows_json')
    if export_headers is not None and export_headers != headers:
        raise ValueError('固定导出列与业务列、来源定位列不一致')
    return {'status': 'verified', 'revision': revision, 'row_count': row_count, 'business_columns': business,
            'source_locator_columns': locators, 'business_column_count': len(business),
            'source_locator_column_count': len(locators), 'export_column_count': len(headers),
            'export_columns': headers, 'formats': ['csv', 'xlsx', 'parquet'],
            'description': '业务列数量不包含来源定位列；CSV/XLSX/Parquet导出列数包含来源定位列，额外定位列不是新增业务字段。'}


def schema_from_export(summary, headers):
    return cleaned_table_schema(summary['columns'], summary['rows'], revision=summary['revision'],
        has_source_rows=bool(headers and headers[-1] == 'source_rows_json'), export_headers=headers)
