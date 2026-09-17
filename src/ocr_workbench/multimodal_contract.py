"""Literal, bounded review targets and model responses, independent of providers."""

from copy import deepcopy
import re

from ocr_workbench.editing import table_bindings, validate_edit
from ocr_workbench.geometry_contract import checked_polygon, fingerprint

CONTRACT_VERSION = 1
MAX_TARGETS = 256
MAX_TARGET_CHARS = 4000
MAX_TOTAL_CHARS = 40000


def target_value(edit, target):
    if target.get("kind") == "text":
        a, z = target.get("start"), target.get("end")
        if type(a) is not int or type(z) is not int or not 0 <= a < z <= len(edit["text"]):
            raise ValueError("文字审校范围无效")
        return edit["text"][a:z]
    if target.get("kind") == "cell":
        ti, row, column = (target.get(k) for k in ("table", "row", "column"))
        if any(type(v) is not int or v < 0 for v in (ti, row, column)) or ti >= len(edit["tables"]):
            raise ValueError("单元格审校范围无效")
        cells = [c for c in edit["tables"][ti]["cells"] if c["row"] == row and c["column"] == column]
        if len(cells) != 1:
            raise ValueError("请选择实际单元格；合并格应选择其左上角")
        return cells[0]["text"]
    raise ValueError("仅支持文字片段和逻辑单元格审校")


def validate_target(edit, target):
    if not isinstance(target, dict):
        raise ValueError("请提供审校目标")
    keys = {"kind", "start", "end"} if target.get("kind") == "text" else {"kind", "table", "row", "column"}
    if set(target) != keys:
        raise ValueError("审校目标字段无效")
    value = target_value(edit, target)
    if target["kind"] == "text":
        parsed, _, _ = table_bindings(edit)
        if any(target["start"] < t["source"]["end"] and target["end"] > t["source"]["start"] for t in parsed):
            raise ValueError("文字范围包含表格源码，请选择具体单元格")
    if len(value) > MAX_TARGET_CHARS:
        raise ValueError(f"单次片段最多 {MAX_TARGET_CHARS} 字符，请缩小审校范围")
    return deepcopy(target)


def _page_targets(edit):
    parsed, replacements, _ = table_bindings(edit)
    if len(parsed) != len(replacements):
        raise ValueError("页面包含尚未绑定的表格，请先整理表格或选择局部文字审校")
    spans, offset = [], 0
    for table in parsed:
        source = table["source"]
        spans.append((offset, source["start"]))
        offset = source["end"]
    spans.append((offset, len(edit["text"])))
    for start, end in spans:
        for match in re.finditer(r"[^\r\n]+", edit["text"][start:end]):
            a, z = start + match.start(), start + match.end()
            if not edit["text"][a:z].strip():
                continue
            for left in range(a, z, MAX_TARGET_CHARS):
                yield {"kind": "text", "start": left, "end": min(left + MAX_TARGET_CHARS, z)}
    for ti, table in enumerate(edit["tables"]):
        for cell in table["cells"]:
            yield {"kind": "cell", "table": ti, "row": cell["row"], "column": cell["column"]}


def build_targets(edit, original, version, scope, target=None, geometry=()):
    """Exclude table markup and bind only actual evidence; absence stays explicit."""
    validate_edit(edit)
    if scope not in ("page", "target"):
        raise ValueError("审校范围必须为 page 或 target")
    if scope == "page" and target is not None:
        raise ValueError("整页审校不可同时指定局部目标")
    candidates = [validate_target(edit, target)] if scope == "target" else _page_targets(edit)
    output, total = [], 0
    for proposed in candidates:
        proposed = validate_target(edit, proposed)
        before = target_value(edit, proposed)
        total += len(before)
        if len(output) >= MAX_TARGETS or total > MAX_TOTAL_CHARS:
            raise ValueError("本页超过审校预算（256 个目标 / 40000 字符），请选择局部范围")
        evidence = {"polygon": None, "level": "page", "version_id": version["id"],
                    "source": "full_image", "reason": "没有可靠局部定位，使用全图审校"}
        for entry in geometry:
            if entry.get("target") != proposed or entry.get("version_id") != version["id"] or entry.get("image_sha256") != version["sha256"]:
                continue
            details = entry.get("details", {})
            if entry.get("source") != "manual" and details.get("anchor_snapshot_current") is False:
                continue
            try:
                polygon = checked_polygon(entry.get("polygon"), version["width"], version["height"])
            except (ValueError, TypeError, KeyError):
                continue
            evidence = {"polygon": deepcopy(polygon), "level": details.get("level", "region"),
                        "version_id": version["id"], "source": entry.get("source"),
                        "reason": details.get("display_reason") or details.get("reason") or "已有定位证据",
                        "evidence_id": entry.get("id"), "range_semantics": details.get("range_semantics")}
            break
        if evidence["polygon"] is None and proposed["kind"] == "text" and before and edit["text"] == original.get("text"):
            matches = [b for b in original.get("blocks", []) if b.get("text") == before]
            if len(matches) == 1 and original["text"].count(before) == 1:
                block = matches[0]
                try:
                    polygon = checked_polygon(block.get("polygon"), version["width"], version["height"])
                except (ValueError, TypeError, KeyError):
                    pass
                else:
                    evidence = {"polygon": deepcopy(polygon), "level": "text", "version_id": version["id"],
                                "source": "original_ocr_block", "reason": "唯一精确匹配的原始文字框"}
        if proposed["kind"] == "text":
            context = {"preceding": edit["text"][max(0, proposed["start"]-160):proposed["start"]],
                       "following": edit["text"][proposed["end"]:proposed["end"]+160]}
        else:
            table = edit["tables"][proposed["table"]]
            context = {"table_caption": table.get("caption", "")[:300], "row": proposed["row"], "column": proposed["column"],
                       "row_cells": [{"column": c["column"], "text": c["text"][:160]} for c in table["cells"]
                                     if c["row"] == proposed["row"]][:20]}
        output.append({"id": "target-" + fingerprint(proposed)[:20], "target": proposed,
                       "before": before, "evidence": evidence, "context": context})
    if not output:
        raise ValueError("当前范围没有可审校的文字或单元格")
    return output


def validate_response(response, targets):
    if not isinstance(response, dict) or set(response) - {"items", "summary", "usage", "model", "runtime", "identity", "evidence", "timing", "artifact"}:
        raise ValueError("模型审校响应格式无效")
    summary = response.get("summary", "")
    if not isinstance(summary, str) or len(summary) > 4000:
        raise ValueError("模型审校摘要无效")
    items = response.get("items")
    expected = {t["id"]: t for t in targets}
    if not isinstance(items, list) or len(items) != len(expected):
        raise ValueError("模型响应必须逐项覆盖已提交的审校目标")
    checked, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"target_id", "decision", "after", "reason"}:
            raise ValueError("模型审校条目字段无效")
        key = item["target_id"]
        if not isinstance(key, str) or key not in expected or key in seen:
            raise ValueError("模型返回了未知或重复审校目标")
        seen.add(key)
        if item["decision"] not in ("keep", "replace", "uncertain"):
            raise ValueError("模型审校决定无效")
        if not isinstance(item["after"], str) or len(item["after"]) > MAX_TARGET_CHARS:
            raise ValueError("模型候选必须为完整字符串，且不得超过 4000 字符")
        if not isinstance(item["reason"], str) or not item["reason"].strip() or len(item["reason"]) > 2000:
            raise ValueError("模型必须提供不超过 2000 字符的审校理由")
        if item["decision"] in ("keep", "uncertain") and item["after"] != expected[key]["before"]:
            raise ValueError("保留或存疑条目不得携带替换内容")
        if item["decision"] == "replace" and item["after"] == expected[key]["before"]:
            raise ValueError("替换条目的内容未变化")
        checked.append(deepcopy(item))
    return {"items": checked, "summary": summary}


def apply_literal(edit, target, after):
    target = validate_target(edit, target)
    value = deepcopy(edit)
    if target["kind"] == "cell":
        cell = next(c for c in value["tables"][target["table"]]["cells"] if c["row"] == target["row"] and c["column"] == target["column"])
        cell["text"], cell["confidence"] = after, None
    else:
        a, z = target["start"], target["end"]
        value["text"] = edit["text"][:a] + after + edit["text"][z:]
        delta = len(after)-(z-a)
        for table in value["tables"]:
            source = table.get("source")
            if isinstance(source, dict) and source.get("start", -1) >= z:
                source["start"] += delta
                source["end"] += delta
        if len(table_bindings(value)[0]) != len(table_bindings(edit)[0]):
            raise ValueError("文字替换会改变表格结构，请在普通编辑器中核对")
    validate_edit(value)
    return value


def rebase_target(target, changed, after):
    """Only independently verifiable non-overlapping positions may survive."""
    target = deepcopy(target)
    if changed is None:
        return target
    if changed["kind"] == "cell":
        return None if target == changed else target
    if target["kind"] == "cell":
        return target
    a, z = changed["start"], changed["end"]
    if target["end"] <= a:
        return target
    if target["start"] >= z:
        delta = len(after)-(z-a)
        target["start"] += delta
        target["end"] += delta
        return target
    return None
