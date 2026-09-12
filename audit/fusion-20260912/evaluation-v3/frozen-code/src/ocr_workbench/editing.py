"""Validate user edits independently of the browser and export literal text."""

import html
from ocr_workbench.tables import parse_tables


def validate_edit(edit):
    if (
        not isinstance(edit, dict)
        or set(edit) != {"text", "tables"}
        or not isinstance(edit["text"], str)
        or len(edit["text"]) > 5_000_000
    ):
        raise ValueError("无效的文字编辑内容")
    if not isinstance(edit["tables"], list) or len(edit["tables"]) > 100:
        raise ValueError("表格数量超出限制")
    for table in edit["tables"]:
        if not isinstance(table, dict):
            raise ValueError("无效的表格内容")
        rows, columns = table.get("rows"), table.get("columns")
        if (
            type(rows) is not int
            or type(columns) is not int
            or not (1 <= rows <= 10000 and 1 <= columns <= 1000)
            or rows * columns > 100000
        ):
            raise ValueError("无效的表格行列数")
        if (
            not isinstance(table.get("caption", ""), str)
            or len(table.get("caption", "")) > 5_000_000
        ):
            raise ValueError("表格标题过长")
        if not isinstance(table.get("cells"), list) or len(table["cells"]) > rows * columns:
            raise ValueError("无效的表格单元格列表")
        occupied = set()
        for cell in table["cells"]:
            if not isinstance(cell, dict):
                raise ValueError("无效的单元格内容")
            values = [cell.get(k) for k in ["row", "column", "row_span", "column_span"]]
            if any(type(v) is not int for v in values):
                raise ValueError("单元格坐标必须为整数")
            r, c, rs, cs = values
            if min(r, c) < 0 or min(rs, cs) < 1 or r + rs > rows or c + cs > columns:
                raise ValueError("单元格超出表格边界")
            if not isinstance(cell.get("text"), str) or len(cell["text"]) > 5_000_000:
                raise ValueError("单元格文本过长或格式无效")
            for y in range(r, r + rs):
                for x in range(c, c + cs):
                    if (y, x) in occupied:
                        raise ValueError("合并单元格发生重叠")
                    occupied.add((y, x))


def tables_html(tables):
    output = []
    for table in tables:
        cells = {(c["row"], c["column"]): c for c in table["cells"]}
        covered = {
            (r, c)
            for cell in table["cells"]
            for r in range(cell["row"], cell["row"] + cell["row_span"])
            for c in range(cell["column"], cell["column"] + cell["column_span"])
        }
        lines = ["<table>"]
        if table.get("caption"):
            lines.append("<caption>" + html.escape(table["caption"]) + "</caption>")
        for r in range(table["rows"]):
            lines.append("<tr>")
            for c in range(table["columns"]):
                cell = cells.get((r, c))
                if cell:
                    lines.append(
                        f'<td rowspan="{cell["row_span"]}" colspan="{cell["column_span"]}">'
                        + html.escape(cell["text"]).replace("\n", "<br>")
                        + "</td>"
                    )
                elif (r, c) not in covered:
                    lines.append("<td></td>")
            lines.append("</tr>")
        output.append("\n".join(lines + ["</table>"]))
    return "\n\n".join(output)


def render_edited(edit, renderer):
    """Use the parser's source ranges; never discard unmatched source text."""
    tables = edit["tables"]
    text = edit["text"]
    if not tables:
        return text
    try:
        sources = [table["source"] for table in parse_tables(text, warnings=[])]
    except ValueError:
        # A manual text edit may leave incomplete markup. Keep it intact.
        sources = []
    replacements = {}
    used = set()
    legacy_order = len(sources) == len(tables) and not any(t.get("source") for t in tables)
    for index, table in enumerate(tables):
        source = table.get("source")
        if legacy_order:
            matches = [index]
        elif isinstance(source, dict):
            matches = [i for i, candidate in enumerate(sources)
                       if i not in replacements and candidate == source]
            if not matches:
                matches = [i for i, candidate in enumerate(sources)
                           if i not in replacements and candidate["sha256"] == source.get("sha256")]
        else:
            matches = []
        if len(matches) == 1:
            replacements[matches[0]] = table
            used.add(index)
    parts, offset = [], 0
    for index, source in enumerate(sources):
        if index not in replacements:
            continue
        parts.extend([text[offset:source["start"]], renderer([replacements[index]])])
        offset = source["end"]
    parts.append(text[offset:])
    remaining = [table for i, table in enumerate(tables) if i not in used]
    if remaining:
        parts.extend(["\n\n", renderer(remaining)])
    return "".join(parts)


def export_markdown(edit):
    return render_edited(edit, tables_html)


def tables_text(tables):
    output = []
    for table in tables:
        cells = {(c["row"], c["column"]): c["text"] for c in table["cells"]}
        lines = [table["caption"]] if table.get("caption") else []
        lines.extend(
            "\t".join(cells.get((r, c), "") for c in range(table["columns"]))
            for r in range(table["rows"])
        )
        output.append("\n".join(lines))
    return "\n\n".join(output)


def export_text(edit):
    return render_edited(edit, tables_text)
