"""Read a bounded Parquet input into the same loss-aware table model."""
from datetime import date, datetime
from decimal import Decimal
import math
from pathlib import Path

import duckdb

from .tables import Cell, Column, ImportedTable, Row, TableImportError, _identity, _row_id, _verified


def import_parquet(path: Path, *, source_sha256: str) -> ImportedTable:
    _verified(path, source_sha256, '.parquet')
    revision = _identity(source_sha256, 'parquet', {})
    warnings = set()
    with duckdb.connect(config={'memory_limit': '256MB', 'threads': '2',
                                'autoinstall_known_extensions': 'false',
                                'autoload_known_extensions': 'false'}) as db:
        db.execute('SET allowed_paths = ?', [[str(path.resolve())]])
        db.execute('SET enable_external_access = false')
        cursor = db.execute('SELECT * FROM read_parquet(?) LIMIT 100001', [str(path.resolve())])
        columns = tuple(Column(f'c{i + 1}', item[0]) for i, item in enumerate(cursor.description))
        if not 1 <= len(columns) <= 256:
            raise TableImportError('Parquet 列数超过限制')
        rows = []
        while batch := cursor.fetchmany(1000):
            for values in batch:
                number = len(rows) + 1
                if number > 100_000 or number * len(columns) > 1_000_000:
                    raise TableImportError('Parquet 超过十万行或一百万单元格限制')
                cells = []
                for value in values:
                    if value is None:
                        cell = Cell('null', None, None)
                    elif isinstance(value, bool):
                        cell = Cell('boolean', str(value).lower(), value)
                    elif isinstance(value, (int, Decimal)):
                        cell = Cell('integer' if isinstance(value, int) else 'decimal', str(value), str(value))
                    elif isinstance(value, float):
                        if not math.isfinite(value):
                            raise TableImportError('Parquet 含非有限浮点值，请先明确转换规则')
                        raw = repr(value)
                        warnings.add('浮点列保留文件中的数值；无法恢复写入前的十进制精度。')
                        cell = Cell('decimal', raw, raw, warning='binary_float_source')
                    elif isinstance(value, datetime):
                        cell = Cell('text', value.isoformat(), value.isoformat())
                    elif isinstance(value, date):
                        cell = Cell('date', value.isoformat(), value.isoformat())
                    elif isinstance(value, str):
                        cell = Cell('text', value, value)
                    else:
                        raise TableImportError('Parquet 暂不接受嵌套或二进制列')
                    cells.append(cell)
                rows.append(Row(_row_id(revision, number), number, tuple(cells)))
    return ImportedTable(source_sha256, revision, 'parquet', {}, columns, tuple(rows), tuple(sorted(warnings)))
