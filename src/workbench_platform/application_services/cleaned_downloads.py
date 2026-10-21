"""Readable CSV presentation of a verified native export, with separate lineage."""
import csv
import hashlib
import io
import json
from urllib.parse import quote

from packages.contracts.agent import AgentRef, AgentToolResult
from packages.contracts.models import SourceLocator
from workbench_platform.artifacts.native_exports import publish
from .identity import canonical


def present(arguments, context):
    context.current()
    center = context.services.artifact_center
    bundle = center.get(context.workspace_id, arguments['bundle_id'])
    native_file = next(item for item in bundle['files'] if item['name'] == 'cleaned.csv')
    artifact = center.file(context.workspace_id, bundle['bundle_id'], native_file['name'])
    columns = arguments['columns']
    # Native exports retain a fixed source_row and cN | header layout. Its
    # exact original file remains published; the user CSV contains business
    # columns, and a sidecar carries the source positions and fixed references.
    with center.store.stage_download(context.workspace_id, artifact.artifact_id,
            expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
        if path.stat().st_size > 64 * 1024 * 1024:
            return AgentToolResult(status='success', summary='已提供原生大文件，来源定位列见说明',
                artifact_refs=[AgentRef(kind='artifact', workspace_id=context.workspace_id,
                    object_id=bundle['bundle_id'], revision=1)], data={'bundle': bundle, 'native_layout': True}).model_dump(mode='json')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        headers = [column['header'] or column['column_id'] for column in columns]
        writer.writerow(["'" + header if header.startswith(('=', '+', '-', '@', '\t', '\r')) else header for header in headers])
        lineage = []
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            native_headers = next(reader)
            expected = [column['column_id'] + ' | ' + (column['header'] or column['column_id']) for column in columns]
            positions = [native_headers.index(header) for header in expected]
            source = native_headers.index('source_row')
            for number, row in enumerate(reader, 2):
                if len(row) != len(native_headers):
                    raise ValueError('原生CSV行宽与固定表头不同')
                writer.writerow([row[index] for index in positions])
                lineage.append({'output_row': number, 'source_row': row[source],
                    **({'source_rows_json': row[native_headers.index('source_rows_json')]} if 'source_rows_json' in native_headers else {})})
        if len(lineage) != arguments['row_count']:
            raise ValueError('实际CSV行数与固定表格不一致')
    encoded = ('\ufeff' + output.getvalue()).encode('utf-8')
    asset = arguments['asset']
    source = SourceLocator(schema_version='1.0', kind='file', asset_id=asset['asset_id'],
        asset_revision=asset['revision'], blob_hash=asset['blob_hash'], precision='exact').model_dump(mode='json')
    provenance = {'version': 'clear-cleaned-csv-v1', 'input': asset, 'table_reference': arguments['table_reference'],
        'native_bundle_id': bundle['bundle_id'], 'columns': columns, 'rows': lineage,
        'csv_sha256': hashlib.sha256(encoded).hexdigest(),
        'csv_text_policy': '原生CSV值逐字保留；公式前缀的转义规则沿用原生导出，原始值见固定表格及原生XLSX。'}
    result = publish(context, asset['name'] + ' 清洗结果', {
        'cleaned.csv': (encoded, 'text/csv'),
        'provenance.json': (canonical(provenance).encode('utf-8'), 'application/json')},
        [{key: asset[key] for key in ('asset_id', 'revision', 'blob_hash')}], [source], 'clear-cleaned-csv-v1')
    return AgentToolResult(status='success', summary='CSV已核对并提供下载；业务表头保留，来源行另附',
        artifact_refs=[AgentRef(kind='artifact', workspace_id=context.workspace_id, object_id=result['bundle_id'], revision=1)],
        data={'bundle': result, 'row_count': len(lineage), 'business_column_count': len(columns),
              'csv_sha256': provenance['csv_sha256']}).model_dump(mode='json')


def downloads(context, bundle):
    base = f'/api/platform/v1/workspaces/{context.workspace_id}/artifacts/{bundle["bundle_id"]}'
    return [{**file, 'download_url': base + '/files/' + quote(file['name'])} for file in bundle['files']]
