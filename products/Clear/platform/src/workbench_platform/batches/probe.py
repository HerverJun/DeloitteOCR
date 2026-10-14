"""Private, bounded structural preflight in the existing data runtime."""
import json
from pathlib import Path
import sys


def probe(request):
    from modules.data_workbench.importing import import_csv, import_xlsx
    from modules.data_workbench.importing.parquet import import_parquet
    path = Path(request['path'])
    options = request['options']
    if path.suffix == '.csv':
        table = import_csv(path, source_sha256=request['hash'],
                           encoding=options.get('encoding', 'utf-8-sig'),
                           delimiter=options.get('delimiter', ','), header_row=options.get('header_row', 1))
    elif path.suffix == '.xlsx':
        if not options.get('sheet_name'):
            raise ValueError('模板须明确工作表名称，不能自动选择第一张工作表')
        table = import_xlsx(path, source_sha256=request['hash'], sheet_name=options['sheet_name'],
                            header_row=options.get('header_row', 1))
    else:
        table = import_parquet(path, source_sha256=request['hash'])
    return {'columns': [{'header': column.header, 'column_id': column.column_id,
                         'kinds': sorted({row.cells[i].kind for row in table.rows
                                          if row.cells[i].kind != 'null'})}
                        for i, column in enumerate(table.columns)],
            'rows': len(table.rows), 'warnings': list(table.warnings)}


if __name__ == '__main__':
    request_path, response_path = map(Path, sys.argv[1:3])
    try:
        value = {'ok': True, **probe(json.loads(request_path.read_text(encoding='utf-8')))}
    except Exception as error:
        value = {'ok': False, 'error': str(error)[:300] if isinstance(error, ValueError)
                 else '资料结构无法读取，请检查文件与导入配置'}
    response_path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
