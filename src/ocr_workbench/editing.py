"""Validate user edits independently of the browser and export literal text."""

import html
import re


def validate_edit(edit):
    if (
        set(edit) != {"text", "tables"}
        or not isinstance(edit["text"], str)
        or len(edit["text"]) > 5_000_000
    ):
        raise ValueError("无效的文字编辑内容")
    if not isinstance(edit["tables"], list) or len(edit["tables"]) > 100:
        raise ValueError("表格数量超出限制")
    for table in edit["tables"]:
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
            or len(table.get("caption", "")) > 32767
        ):
            raise ValueError("表格标题过长")
        occupied = set()
        for cell in table["cells"]:
            values = [cell.get(k) for k in ["row", "column", "row_span", "column_span"]]
            if any(type(v) is not int for v in values):
                raise ValueError("单元格坐标必须为整数")
            r, c, rs, cs = values
            if min(r, c) < 0 or min(rs, cs) < 1 or r + rs > rows or c + cs > columns:
                raise ValueError("单元格超出表格边界")
            if not isinstance(cell.get("text"), str) or len(cell["text"]) > 32767:
                raise ValueError("单元格文本超过 Excel 限制")
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
    """Replace table markup in document order, preserving surrounding text."""
    tables = edit["tables"]
    index = 0
    if not tables:
        return edit["text"]

    def replace(match):
        nonlocal index
        if index >= len(tables):
            return ""
        value = renderer([tables[index]])
        index += 1
        return value

    pattern = r"<table\b[^>]*>.*?</table\s*>|^[ \t]*\|[^\n]+\|[ \t]*\n[ \t]*\|[ :|\-]+\|[ \t]*\n(?:[ \t]*\|[^\n]*\|[ \t]*(?:\n|$))*"
    text = re.sub(pattern, replace, edit["text"], flags=re.I | re.S | re.M)
    if index < len(tables):
        text += "\n\n" + renderer(tables[index:])
    return text


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
