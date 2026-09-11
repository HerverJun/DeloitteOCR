"""Disk-backed exports: separate image files by default, optional workbook."""

import json
from pathlib import Path
import shutil
import tempfile
import zipfile
from ocr_workbench.editing import validate_edit, export_markdown, export_text
from ocr_workbench.tables import export_xlsx


def build_export(store, keys, format, aggregate=False):
    if not isinstance(keys, list) or not keys or len(keys) > 1000:
        raise ValueError("请选择需要导出的结果（最多 1000 个）")
    if not all(isinstance(key, str) for key in keys):
        raise ValueError("结果编号无效")
    if format not in {"txt", "md", "json", "xlsx"}:
        raise ValueError("未知导出格式")
    if not isinstance(aggregate, bool):
        raise ValueError("汇总选项必须为布尔值")
    keys = list(dict.fromkeys(keys))
    parent = store.root / "exports"
    parent.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="export-", dir=parent))

    def read(key):
        result = store.result(key)
        validate_edit(result["edited"])
        return result

    def write(result, path):
        if format == "xlsx":
            if not result["edited"]["tables"]:
                raise ValueError("选中结果没有结构化表格，请选择其他格式或表格引擎")
            export_xlsx(result["edited"]["tables"], path)
        else:
            content = (
                json.dumps(result, ensure_ascii=False, indent=2)
                if format == "json"
                else (
                    export_markdown(result["edited"])
                    if format == "md"
                    else export_text(result["edited"])
                )
            )
            path.write_text(content, encoding="utf-8")

    try:
        if format == "xlsx" and aggregate:
            # Only the requested tables are retained; raw model output is not accumulated.
            def tables():
                for key in keys:
                    yield from read(key)["edited"]["tables"]

            target = folder / "OCR-tables.xlsx"
            export_xlsx(tables(), target)
        elif len(keys) == 1:
            target = folder / ("OCR-result." + format)
            write(read(keys[0]), target)
        else:
            target = folder / "OCR-results.zip"
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for index, key in enumerate(keys, 1):
                    # Numeric names cannot inherit user filenames or path separators.
                    item = folder / f"{index:04d}.{format}"
                    write(read(key), item)
                    archive.write(item, item.name)
                    item.unlink()
        return target
    except BaseException:
        shutil.rmtree(folder)
        raise
