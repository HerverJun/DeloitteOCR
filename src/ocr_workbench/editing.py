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
            if 'is_header' in cell and type(cell['is_header']) is not bool:
                raise ValueError("表头标记必须为布尔值")
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
                    tag = 'th' if cell.get('is_header') else 'td'
                    lines.append(
                        f'<{tag} rowspan="{cell["row_span"]}" colspan="{cell["column_span"]}">'
                        + html.escape(cell["text"]).replace("\n", "<br>")
                        + f"</{tag}>"
                    )
                elif (r, c) not in covered:
                    lines.append("<td></td>")
            lines.append("</tr>")
        output.append("\n".join(lines + ["</table>"]))
    return "\n\n".join(output)


def table_bindings(edit):
    """Share exact source matching between the editor presentation and exports."""
    tables = edit["tables"]
    text = edit["text"]
    try:
        parsed = parse_tables(text, warnings=[])
    except ValueError:
        parsed = []
    sources = [table["source"] for table in parsed]
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
            replacements[matches[0]] = index
            used.add(index)
    return parsed, replacements, used


def text_sources(edit):
    """Describe source spans in browser UTF-16 offsets without changing storage."""
    parsed, replacements, _ = table_bindings(edit)
    output, offset, utf16 = [], 0, 0
    for index, table in enumerate(parsed):
        source = table["source"]
        start, end = source["start"], source["end"]
        utf16 += len(edit["text"][offset:start].encode("utf-16-le")) // 2
        width = len(edit["text"][start:end].encode("utf-16-le")) // 2
        try:
            validate_edit({"text": "", "tables": [table]})
        except ValueError:
            pass  # Unusable model structure stays literal, never expanded in UI.
        else:
            output.append({"start": utf16, "end": utf16 + width,
                           "table_index": replacements.get(index), "original_table": table})
        offset, utf16 = end, utf16 + width
    return output


def present_result(result):
    return {**result, "text_sources": text_sources(result["edited"])}


def render_edited(edit, renderer, *, include_unbound=False):
    """Use the parser's source ranges; never discard unmatched source text."""
    text, tables = edit["text"], edit["tables"]
    if not tables and not include_unbound:
        return text
    parsed, replacements, used = table_bindings(edit)
    parts, offset = [], 0
    for index, parsed_table in enumerate(parsed):
        source = parsed_table["source"]
        if index not in replacements and not include_unbound:
            continue
        value = tables[replacements[index]] if index in replacements else parsed_table
        if include_unbound and index not in replacements:
            try:
                validate_edit({"text": "", "tables": [value]})
            except ValueError:
                continue
        parts.extend([text[offset:source["start"]], renderer([value])])
        offset = source["end"]
    parts.append(text[offset:])
    remaining = [table for i, table in enumerate(tables) if i not in used]
    if remaining:
        parts.extend(["\n\n", renderer(remaining)])
    return "".join(parts)


def export_markdown(edit):
    return render_edited(edit, tables_html)


def tsv_value(value):
    # Match spreadsheet TSV parsing, including literal leading quotes.
    return '"' + value.replace('"', '""') + '"' if any(c in value for c in '\t\r\n"') else value


def tables_text(tables):
    output = []
    for table in tables:
        cells = {(c["row"], c["column"]): c["text"] for c in table["cells"]}
        lines = [table["caption"]] if table.get("caption") else []
        lines.extend(
            "\t".join(tsv_value(cells.get((r, c), "")) for c in range(table["columns"]))
            for r in range(table["rows"])
        )
        output.append("\n".join(lines))
    return "\n\n".join(output)


def export_text(edit):
    return render_edited(edit, tables_text, include_unbound=True)
