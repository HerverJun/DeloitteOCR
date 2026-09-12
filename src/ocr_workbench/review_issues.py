"""Stable review targets, incremental invalidation and literal candidate edits."""

from copy import deepcopy
from difflib import SequenceMatcher
import json

from ocr_workbench.fusion_alignment import canonical_edit, complete_structure, fingerprint, topology


def target_value(edit, target):
    kind = target["kind"]
    if target.get("unlocatable"):
        return edit["text"], False
    if kind in {"cell", "table"}:
        tables = [t for t in edit["tables"] if t.get("fusion_id") == target["table_id"]]
        if len(tables) != 1:
            return None, False
        table = tables[0]
        if kind == "table":
            return table, True
        if fingerprint(topology(table)) != target["structure"]:
            return None, False
        cells = [c for c in table["cells"] if c["row"] == target["row"] and c["column"] == target["column"]]
        return (cells[0]["text"], True) if len(cells) == 1 else (None, False)
    if kind == "text":
        a, z = target["start"], target["end"]
        if not 0 <= a <= z <= len(edit["text"]):
            return None, False
        return edit["text"][a:z], True
    if kind == "document":
        return edit["text"], True
    if kind == "unmatched_table":
        return None, True
    return None, False


def comparable(value):
    """Canonical offsets and unrelated metadata do not invalidate a decision."""
    if isinstance(value, dict) and "cells" in value:
        from ocr_workbench.fusion_alignment import table_content
        return table_content(value)
    return value


def relocate_text(old, new, target):
    if old == new:
        return target, True
    a, z = target["start"], target["end"]
    # Cheap prefix/suffix handles large ordinary edits without an unbounded DP.
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new))-prefix and old[-suffix-1] == new[-suffix-1]:
        suffix += 1
    if z <= prefix and a != z:
        return target, True
    if a >= len(old)-suffix and a != z:
        shift = len(new)-len(old)
        return {**target, "start": a+shift, "end": z+shift}, True
    # For the changed span itself preserve a well-defined replacement range so
    # manual re-review remains possible, but never preserve its decision.
    if a == prefix and z == len(old)-suffix:
        return {**target, "start": prefix, "end": len(new)-suffix}, False
    if len(old)*len(new) <= 4_000_000:
        for op, i, j, k, l in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if op == "equal" and i <= a <= z <= j and a != z:
                return {**target, "start": k+a-i, "end": k+z-i}, True
    return {**target, "unlocatable": True}, False


def reconcile(db, result_id, before, after, *, deciding=None):
    """Only changed targets expire; issue IDs and immutable evidence survive."""
    from ocr_workbench.store import encoded, now
    rows = db.execute("SELECT * FROM fusion_issues WHERE result_id=?", (result_id,)).fetchall()
    for row in rows:
        target = json.loads(row["target"])
        old_value, old_valid = target_value(before, target)
        stable = True
        if target["kind"] == "text":
            target, stable = relocate_text(before["text"], after["text"], target)
        new_value, new_valid = target_value(after, target)
        same = old_valid and new_valid and stable and comparable(old_value) == comparable(new_value)
        state = row["state"] if same or row["id"] == deciding else "stale"
        db.execute("UPDATE fusion_issues SET target=?,state=?,current_value=?,updated=? WHERE id=?",
                   (encoded(target), state, encoded(new_value), now(), row["id"]))


def apply_choice(edit, target, value, placement=None):
    value_edit = deepcopy(edit)
    _, valid = target_value(value_edit, target)
    if not valid or target.get("unlocatable"):
        raise ValueError("疑点位置已变化，请先在普通编辑器中定位并复核当前内容")
    kind = target["kind"]
    if kind == "cell":
        if not isinstance(value, str):
            raise ValueError("单元格候选必须是完整字符串")
        table = next(t for t in value_edit["tables"] if t.get("fusion_id") == target["table_id"])
        cell = next(c for c in table["cells"] if c["row"] == target["row"] and c["column"] == target["column"])
        cell["text"] = value
        cell["confidence"], cell["polygon"] = None, None
    elif kind == "table":
        if not isinstance(value, dict) or "cells" not in value or not complete_structure(value):
            raise ValueError("请选择完整候选表")
        for i, table in enumerate(value_edit["tables"]):
            if table.get("fusion_id") == target["table_id"]:
                candidate = deepcopy(value)
                candidate["fusion_id"] = table["fusion_id"]
                candidate["source"] = table.get("source")
                value_edit["tables"][i] = candidate
                break
    elif kind == "unmatched_table":
        if placement not in {"start", "end"}:
            raise ValueError("缺表没有可靠阅读位置，请明确选择文档开头或末尾")
        if not isinstance(value, dict) or not complete_structure(value):
            raise ValueError("缺表候选没有完整合法结构，请在普通编辑器中核对")
        from ocr_workbench.editing import tables_html
        from ocr_workbench.tables import parse_tables
        candidate = deepcopy(value)
        candidate["fusion_id"] = fingerprint({"inserted": target})[:24]
        rendered = tables_html([candidate])
        offset = 0 if placement == "start" else len(value_edit["text"]) + 2
        if placement == "start":
            # Keep existing source offsets bound to their unchanged text.
            for table in value_edit["tables"]:
                if table.get("source"):
                    for key in ("start", "end"):
                        table["source"][key] += len(rendered)+2
            value_edit["text"] = rendered + "\n\n" + value_edit["text"]
            value_edit["tables"].insert(0, candidate)
        else:
            value_edit["text"] += "\n\n" + rendered
            value_edit["tables"].append(candidate)
        candidate["source"] = parse_tables(rendered)[0]["source"]
        for key in ("start", "end"):
            candidate["source"][key] += offset
    elif kind in {"text", "document"}:
        if not isinstance(value, str):
            raise ValueError("文字候选必须是字符串")
        if value_edit["tables"]:
            raise ValueError("此片段含表格，请在普通编辑器中核对结构后保留当前")
        a, z = (target["start"], target["end"]) if kind == "text" else (0, len(edit["text"]))
        value_edit["text"] = edit["text"][:a] + value + edit["text"][z:]
    return canonical_edit(value_edit)
