"""Observable OCR risks, bounded candidate selection and traceable suggestions.

Risk is a prioritization score, never a calibrated accuracy or human approval.
No arithmetic, majority vote or model self-confidence rewrites numeric values.
Geometry evidence refers to the EXIF-normalized original, as in screen_imaging.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re
import unicodedata

from ocr_workbench.coordinates import bounds, polygon
from ocr_workbench.tables import parse_tables
from ocr_workbench.screen_diagnostics import (diagnose_screen_structure, observed_box,
    observe_screen_grid_cells, localized_grid_box)

POLICY_VERSION = "screen-quality-v4"
ID_WORDS = re.compile(r"编号|账号|账户|代码|流水|证件|序号|identifier|\b(id|code|account|reference)\b", re.I)
NUMBER_WORDS = re.compile(r"金额|数量|单价|余额|税额|税率|百分|amount|quantity|price|balance|total|rate", re.I)
TOTAL_WORDS = re.compile(r"^(合计|总计|小计|总额|total|subtotal|grand total)[:：]?\s*$", re.I)
NUMERICISH = re.compile(r"^[\s￥¥$€£+\-−–()（）\d.,%％OoIl]+(?:万|元|万元)?$")
UI_KINDS = {"toolbar", "taskbar", "formula_bar", "ruler", "window_border", "scrollbar", "ui"}


def _normal(value):
    return "".join(unicodedata.normalize('NFC',str(value)).split())


def _box(item):
    # Existing source-bound full-cell geometry can live in structure_source.
    # This reads an actual polygon; it does not divide a table into guessed cells.
    supplied = observed_box(item)
    if supplied:
        return supplied
    value = item.get("box", item.get("bbox", item.get("coordinate")))
    if value is None and item.get("polygon"):
        try:
            value = bounds(item["polygon"])
        except (ValueError, TypeError):
            return None
    try:
        if len(value) == 4 and all(math.isfinite(float(v)) for v in value) and value[2] > value[0] and value[3] > value[1]:
            return list(map(float, value))
    except (TypeError, ValueError):
        pass
    return None


def _area(box):
    return (box[2]-box[0])*(box[3]-box[1])


def _intersection(a, b):
    return max(0., min(a[2],b[2])-max(a[0],b[0]))*max(0., min(a[3],b[3])-max(a[1],b[1]))


def _evidence(text_evidence):
    if isinstance(text_evidence, dict):
        return text_evidence.get("blocks",text_evidence.get("text",text_evidence.get("text_boxes",[])))
    return text_evidence or []


def _reliable_region_count(image_info):
    """An observed, uncapped set of distinct strong business regions only.

    Weak text clusters and overlapping or capped detections cannot establish an
    output table count. This is a review signal, never permission to delete data.
    """
    regions = image_info.get("business_regions", [])
    if not isinstance(regions, list) or not regions or any(w.get("code") in {"region_limit", "roi_uncertain"}
                          for w in image_info.get("warnings", []) if isinstance(w, dict)):
        return None
    boxes = []
    for region in regions:
        if not isinstance(region, dict):
            return None
        box = _box({"box": region.get("table_box", region.get("box"))})
        score = region.get("score", region.get("confidence"))
        sources = region.get("sources") or []
        strong = region.get("grid_reliable") is True or (
            isinstance(sources, list) and "layout_table" in sources
            and type(score) in {int, float} and math.isfinite(score) and score >= .8)
        if not box or not strong or any(_intersection(box, old)/min(_area(box), _area(old)) > .1 for old in boxes):
            return None
        boxes.append(box)
    return len(boxes)


def _raw(raw):
    if isinstance(raw, str):
        warnings = []
        tables = parse_tables(raw, warnings=warnings)
        return {"text": raw, "tables": tables, "warnings": warnings}
    if not isinstance(raw, dict):
        return {"text": "", "tables": [], "warnings": [{"code": "invalid_result"}]}
    output = deepcopy(raw)
    if "tables" not in output:
        output["warnings"] = list(output.get("warnings",[]))
        output["tables"] = parse_tables(str(output.get("text","")), warnings=output["warnings"])
    return output


def map_screen_result_to_source(raw, candidate):
    """Map only actual supplied cell/table/block geometry; never invent boxes."""
    result = _raw(raw)
    transform = candidate.get("prepared_to_source")
    if not transform:
        raise ValueError("缺少预处理到原图坐标映射")
    for item in result.get("blocks",[]) + result.get("tables",[]):
        _map_geometry(item,transform)
    for table in result.get("tables",[]):
        for cell in table.get("cells",[]):
            _map_geometry(cell,transform)
    result["coordinate_space"] = "exif_normalized_original"
    result["screen_preparation"] = {"candidate_id":candidate.get("id"),"kind":candidate.get("kind"),"region_id":candidate.get("region_id")}
    return result


def _map_geometry(item, transform):
    source = item.get("polygon")
    box = _box(item)
    if source is None and box:
        source = [[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]]
    if source:
        item["polygon"] = polygon(transform,source)
        item["box"] = bounds(item["polygon"])


def _field_kind(table, cell):
    explicit = cell.get("field_type",cell.get("data_type"))
    if explicit in {"id","identifier","account","code"}:
        return "id"
    if explicit in {"number","numeric","amount","decimal","percent"}:
        return "numeric"
    header = " ".join(str(c.get("text","")) for c in table.get("cells",[]) if c.get("column") == cell.get("column")
                      and (c.get("is_header") or c.get("row") == 0) and c is not cell)
    if ID_WORDS.search(header):
        return "id"
    if NUMBER_WORDS.search(header):
        return "numeric"
    text = str(cell.get("text",""))
    if re.fullmatch(r"0\d{3,}|\d{16,}",text.strip()):
        return "id"
    if text.strip() and NUMERICISH.fullmatch(text) and any(ch.isdigit() for ch in text):
        return "numeric"
    return "text"


def _decimal(text):
    """Strict visible decimal parse: no ambiguous locale, unit or ID guessing."""
    text = str(text).strip().replace("−","-")
    if re.search(r"[万%％]",text):
        return None
    text = re.sub(r"^[￥¥$€£]", "",text)
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if "," in text:
        if not re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?",text):
            return None
        text = text.replace(",","")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?",text) or re.fullmatch(r"0\d+",text):
        return None
    try:
        value = Decimal(text)
        return -value if negative else value
    except InvalidOperation:
        return None


def assess_screen_result(raw, image_info=None, text_evidence=None):
    """Assess observable issues without asserting that an unflagged value is true.

    Return issues and upgrade kind (text/structure/visual/None), separate
    automatic_delivery and human_review state. Missing text/geometry is explicit
    in checks: an absent evidence source does not count as a passed check.
    """
    raw = _raw(raw)
    image_info = image_info or {}
    tables = raw.get("tables",[])
    evidence = [v for v in _evidence(text_evidence) if isinstance(v,dict)]
    grid_localization=observe_screen_grid_cells(raw,image_info,text_evidence)
    issues = []
    seen = set()
    checks = {"structure":"checked","text_coverage":"unavailable" if not evidence else "checked",
              "region_coverage":"checked" if image_info.get("business_regions") else "unavailable",
              "arithmetic":"not_applicable", "visual_readability":"checked" if image_info.get("quality") else "unavailable"}

    def issue(code, severity, message, kind, **details):
        key = (code,details.get("table"),details.get("row"),details.get("column"),details.get("evidence_index"),details.get("region_id"))
        if key not in seen:
            seen.add(key)
            issues.append({"code":code,"severity":severity,"message":message,"upgrade_kind":kind,**details})

    if raw.get("status") in {"failed","error","cancelled","timeout"}:
        issue("recognition_failed","high","识别任务未成功完成。","visual",status=raw.get("status"))
    if not isinstance(tables,list) or not tables:
        issue("no_table","high","未得到可交付业务表格；不能生成空白伪成功 Excel。","structure")
        tables = []
    for warning in raw.get("warnings",[]):
        code = warning.get("code","") if isinstance(warning,dict) else str(warning)
        if code in {"table_parse_failed","table_structure_invalid","invalid_result"}:
            issue("invalid_or_truncated_table","high","输出表格无法完整解析。","structure",detail=warning)
    finish = raw.get("finish_reason", raw.get("raw",{}).get("finish_reason") if isinstance(raw.get("raw"),dict) else None)
    if finish in {"length","max_tokens","token_limit"} or raw.get("truncated"):
        issue("output_cutoff","high","模型输出达到上限，表格可能缺失。","structure")
    if isinstance(raw.get("text"),str) and raw["text"].count("<table") > raw["text"].count("</table>"):
        issue("output_cutoff","high","HTML 表格未闭合，原始文本保留。","structure")

    all_cells = []
    for ti, table in enumerate(tables):
        if not isinstance(table,dict):
            issue("invalid_grid","high","表格结果类型无效。","structure",table=ti)
            continue
        rows, columns, cells = table.get("rows"),table.get("columns"),table.get("cells",[])
        if type(rows) is not int or type(columns) is not int or not (1 <= rows <= 10000 and 1 <= columns <= 1000) or rows*columns > 500000 or not isinstance(cells,list):
            issue("invalid_grid","high","行列尺寸不合法。","structure",table=ti)
            continue
        occupied = set()
        valid = []
        for ci, cell in enumerate(cells):
            if not isinstance(cell,dict):
                issue("invalid_grid","high","单元格结果类型无效。","structure",table=ti)
                continue
            r,c,rs,cs = cell.get("row"),cell.get("column"),cell.get("row_span",1),cell.get("column_span",1)
            if any(type(v) is not int for v in (r,c,rs,cs)) or r < 0 or c < 0 or rs < 1 or cs < 1 or r+rs > rows or c+cs > columns:
                issue("invalid_span","high","单元格坐标或合并跨度超出表格。","structure",table=ti,cell=ci)
                continue
            slots = {(ri,co) for ri in range(r,r+rs) for co in range(c,c+cs)}
            if slots & occupied:
                issue("overlapping_cells","high","单元格合并范围发生重叠。","structure",table=ti,row=r,column=c)
            occupied |= slots
            valid.append(cell)
            all_cells.append((ti,cell))
            value = cell.get("text")
            if not isinstance(value,str):
                issue("non_text_value","high","原始识别值不是字符串，可能损失前导零或长编号。","text",table=ti,row=r,column=c)
                value = str(value or "")
            kind = _field_kind(table,cell)
            if cell.get("unreadable") or cell.get("unknown") or value.strip() in {"[无法识别]","[unreadable]","<?>"}:
                issue("unknown_cell","high","单元格无法读清；不能把未知记为空白。","visual",table=ti,row=r,column=c)
            if kind in {"id","numeric"}:
                if re.search(r"[OoIl]",value) and any(ch.isdigit() for ch in value):
                    issue("numeric_character_ambiguity","high","金额或编号含易混字符；需原图局部复核。","text",table=ti,row=r,column=c,value=value)
                if re.search(r"\d(?:\.\d*)?[eE][+-]?\d|\.\.\.|…",value):
                    issue("number_shortening","high","金额/编号可能被科学记数或省略。","text",table=ti,row=r,column=c,value=value)
            if len(value) > 32767:
                issue("xlsx_value_limit","high","单元格超过 Excel 文本长度限制。","text",table=ti,row=r,column=c)
        holes = rows*columns-len(occupied)
        if holes:
            issue("missing_grid_slots","high","格位未分配；与实际空白格分别记录。","structure",table=ti,missing_slots=holes)
        # Exact duplicate populated rows can also be legitimate data: suspicion only.
        row_values = []
        for ri in range(rows):
            row = sorted((c for c in valid if c["row"] == ri),key=lambda c:c["column"])
            values = tuple(_normal(c.get("text","")) for c in row)
            if len(values) >= 2 and sum(bool(v) for v in values) >= 2 and all(c.get("row_span",1) == 1 for c in row):
                row_values.append((ri,values))
        frequencies = Counter(v for _,v in row_values)
        duplicate = [ri for ri,v in row_values if frequencies[v] > 1]
        if duplicate:
            issue("duplicate_row_suspicion","low","重复整行可能为真实数据，保留原值并检查定位。","structure",table=ti,rows=duplicate)
        # Arithmetic is a detector of doubt, not a digit recovery mechanism.
        for total in valid:
            if total.get("column") != 0 or not TOTAL_WORDS.fullmatch(str(total.get("text","")).strip()) or total["row"] < 2:
                continue
            for column in range(1,columns):
                total_cell = next((c for c in valid if c["row"] == total["row"] and c["column"] == column),None)
                if not total_cell or _field_kind(table,total_cell) == "id":
                    continue
                sources = [c for c in valid if 0 < c["row"] < total["row"] and c["column"] == column]
                if len(sources) != total["row"]-1 or any(c.get("row_span",1) != 1 or c.get("column_span",1) != 1 for c in sources):
                    continue
                values, expected = [_decimal(c.get("text","")) for c in sources], _decimal(total_cell.get("text",""))
                if expected is None or any(v is None for v in values):
                    continue
                checks["arithmetic"] = "checked"
                calculated = sum(values,Decimal(0))
                if calculated != expected:
                    issue("arithmetic_mismatch","medium","可见合计与明细十进制求和不同，仅记录疑点，不改原数。","text",table=ti,row=total["row"],column=column,
                          visible_total=str(expected),calculated_total=str(calculated))

    regions = image_info.get("business_regions",[])
    if len(tables) < len(regions):
        issue("missing_table","high","检测到的业务表区多于输出表格。","structure",detected=len(regions),returned=len(tables))
    region_count = _reliable_region_count(image_info)
    checks["region_table_count"] = "checked" if region_count is not None else "unavailable"
    if region_count is not None and len(tables) > region_count:
        issue("unsupported_extra_table","high","输出表格数量多于可靠独立业务表区，额外表格缺少表区支持，保留原结果待复核。","structure",
              detected=region_count, returned=len(tables), region_ids=[r.get("id") for r in regions])
    geometry_tables = [(ti,_box(table)) for ti,table in enumerate(tables) if isinstance(table,dict) and _box(table)]
    for region in regions:
        rb = region.get("table_box",region.get("box"))
        matches = [(ti,b) for ti,b in geometry_tables if rb and _intersection(rb,b)/_area(rb) > .6]
        if len(matches) == 1:
            ti,_ = matches[0]
            if region.get("grid_reliable") and region.get("observed_grid_rows",0) > tables[ti].get("rows",0):
                issue("missing_observed_rows","high","可靠可见网格行数多于输出行数。","structure",table=ti,region_id=region.get("id"),
                      observed=region["observed_grid_rows"],returned=tables[ti].get("rows"))
        elif geometry_tables:
            issue("uncovered_table_region","high","表区没有唯一对应的输出定位。","structure",region_id=region.get("id"))
    for index,item in enumerate(evidence):
        if item.get("kind") in UI_KINDS or not str(item.get("text","")).strip():
            continue
        box = _box(item)
        if not box:
            continue
        if any(_intersection(box,ex["box"])/_area(box) > .5 for ex in image_info.get("excluded_regions",[]) if ex.get("box")):
            continue
        inside = [r for r in regions if r.get("table_box",r.get("box")) and _intersection(box,r.get("table_box",r.get("box")))/_area(box) >= .65]
        if regions and not inside:
            continue  # caption/notes are checked separately from cell coverage
        spatial = [(ti,c) for ti,c in all_cells if (cb := _box(c) or localized_grid_box(grid_localization,ti,c))
                   and _intersection(box,cb)/_area(box) >= .7]
        if len(spatial) == 1:
            ti,cell = spatial[0]
            recognized, actual = _normal(cell.get("text","")), _normal(item.get("text",""))
            critical = _field_kind(tables[ti],cell) in {"id","numeric"} or _field_kind(tables[ti],{**cell,"text":item.get("text","")}) in {"id","numeric"}
            # A shorter numeric reading (1 vs 10, a missing zero or minus sign)
            # is a disagreement, even when it is a substring of the candidate.
            disagrees = actual != recognized if critical else actual not in recognized
            if disagrees:
                severity = "high" if critical else "medium"
                issue("localized_text_disagreement",severity,"独立文字证据与该格转录不同。","text",table=ti,row=cell["row"],column=cell["column"],
                      evidence_index=index,visible_text=item.get("text"),value=cell.get("text"),evidence_source=item.get("source",item.get("engine")))
        elif all_cells and (any(_box(c) for _,c in all_cells) or any(
                table.get('status')=='verified' and table.get('region_id') in {region.get('id') for region in inside}
                for table in grid_localization['tables'])):
            issue("unassigned_visible_text","high","表区内可见文字未唯一归入单元格。","structure",evidence_index=index,text=item.get("text"),box=box)
        elif _normal(item.get("text","")) not in _normal(" ".join(str(c.get("text","")) for _,c in all_cells)):
            issue("uncovered_text_without_geometry","medium","独立文字未见于结果，缺少可靠单格定位。","structure",evidence_index=index,text=item.get("text"),box=box)
            checks["text_coverage"] = "partial_without_cell_geometry"
    for flag in image_info.get("quality",{}).get("flags",[]):
        if flag.get("severity") in {"high","medium"}:
            issue("image_"+flag.get("code","quality"),flag["severity"],flag.get("message","图像质量疑点。"),"visual",region_id=flag.get("region_id"))
    if image_info.get("quality",{}).get("unreadable"):
        issue("unreadable_source","high","源图像细节不足，保持待复核；不推测遮挡/不可读内容。","visual")
    for warning in image_info.get("warnings",[]):
        if warning.get("code") == "region_limit":
            issue("region_budget_incomplete","high","表区数量超过预算，可能漏表。","structure")
    observed = diagnose_screen_structure(raw,image_info,text_evidence,grid_localization=grid_localization)
    checks.update(observed['checks'])
    for diagnostic in observed['issues']:
        details = {key:value for key,value in diagnostic.items() if key not in {'code','severity','message','upgrade_kind'}}
        issue(diagnostic['code'],diagnostic['severity'],diagnostic['message'],diagnostic['upgrade_kind'],**details)
    # Risk is bounded and deterministic for ordering, not a probability.
    weights = {"low":3,"medium":16,"high":35}
    risk = min(100,sum(weights[i["severity"]] for i in issues))
    blocking = [i for i in issues if i["severity"] in {"high","medium"}]
    kinds = {i["upgrade_kind"] for i in blocking}
    upgrade = "visual" if any(i["code"] == "unreadable_source" for i in blocking) else ("structure" if "structure" in kinds else "text" if "text" in kinds else "visual" if "visual" in kinds else None)
    return {"policy_version":POLICY_VERSION,"issues":issues,"risk":risk,"automatic_delivery":bool(tables) and not blocking,
            "review_required":bool(blocking),"human_review":"not_performed","upgrade_kind":upgrade,"checks":checks,
            "grid_localization":grid_localization,
            "summary":{"tables":len(tables),"cells":len(all_cells),"high_risk":sum(i["severity"]=="high" for i in issues),
                       "medium_risk":sum(i["severity"]=="medium" for i in issues),"low_risk":sum(i["severity"]=="low" for i in issues)}}


def _cell_matches(a, b):
    """Pair only same topology with anchors or unique real cell geometry."""
    output = []
    at,bt = a.get("tables",[]),b.get("tables",[])
    if len(at) != len(bt):
        return output
    for ti,(left,right) in enumerate(zip(at,bt)):
        lc,rc = left.get("cells",[]),right.get("cells",[])
        topology = lambda table: (table.get("rows"),table.get("columns"),sorted((c.get("row"),c.get("column"),c.get("row_span",1),c.get("column_span",1)) for c in table.get("cells",[])))
        if topology(left) != topology(right):
            continue
        anchor_values = Counter(_normal(c.get("text","")) for c in lc if c.get("text"))
        lookup = {(c.get("row"),c.get("column")):c for c in rc}
        agreement = sum(bool(_normal(c.get("text",""))) and _normal(c.get("text","")) == _normal(lookup[(c["row"],c["column"])].get("text","")) for c in lc)/max(1,len(lc))
        for cell in lc:
            other = lookup[(cell["row"],cell["column"])]
            box,ob = _box(cell),_box(other)
            geometry = bool(box and ob and _intersection(box,ob)/max(_area(box),_area(ob)) > .8)
            row_anchor = any(c["row"]==cell["row"] and c["column"]!=cell["column"] and anchor_values[_normal(c.get("text",""))]==1
                             and _normal(c.get("text","")) == _normal(lookup[(c["row"],c["column"])].get("text","")) for c in lc)
            column_anchor = any(c["column"]==cell["column"] and c["row"]!=cell["row"] and anchor_values[_normal(c.get("text",""))]==1
                                and _normal(c.get("text","")) == _normal(lookup[(c["row"],c["column"])].get("text","")) for c in lc)
            if geometry or agreement >= .6 and row_anchor and column_anchor:
                output.append((ti,cell,other,"geometry" if geometry else "unique_text_anchors"))
    return output


def select_screen_candidates(candidates, image_info=None, text_evidence=None, *, allow_text_fixes=False):
    """Select by observed quality, retain disagreements; never use majority vote.

    Candidates are ``{id, result, engine?}`` dicts. A permitted text fix needs a
    unique cell box, >=.97 independent text confidence, direct detector support,
    and a low-confidence/empty current transcription. IDs and numbers are always
    review suggestions. Default leaves all existing values intact.
    """
    if not candidates:
        raise ValueError("没有可选择的识别候选")
    ids = [str(c.get("id",f"candidate-{i}")) for i,c in enumerate(candidates)]
    if len(set(ids)) != len(ids):
        raise ValueError("候选 ID 必须唯一")
    results = [_raw(c.get("result",c.get("raw",c))) for c in candidates]
    assessments = {key:assess_screen_result(result,image_info,text_evidence) for key,result in zip(ids,results)}
    def available(result):
        tables = result.get("tables")
        return (result.get("status") not in {"failed", "error", "cancelled", "timeout"}
                and isinstance(tables, list)
                and any(isinstance(table, dict) and isinstance(table.get("cells"), list) and table["cells"]
                        for table in tables))
    # A successful table remains usable for review even when risk saturates.
    # Equal observed risk retains input order; more tables are not evidence of quality.
    chosen = min(range(len(ids)),key=lambda i:(not available(results[i]),assessments[ids[i]]["risk"],i))
    selected = deepcopy(results[chosen])
    conflicts,modifications = [],[]
    for index,result in enumerate(results):
        if index == chosen:
            continue
        if result.get("status") in {"failed","error","cancelled","timeout"}:
            continue  # a failed attempt supplies no contradictory reading
        if len(result.get("tables",[])) != len(selected.get("tables",[])):
            conflicts.append({"code":"table_count_conflict","severity":"high","candidate_id":ids[index],"selected_id":ids[chosen]})
        matches = _cell_matches(selected,result)
        matched_slots = {(ti,c["row"],c["column"]) for ti,c,_,_ in matches}
        for ti,(left,right) in enumerate(zip(selected.get("tables",[]),result.get("tables",[]))):
            topology = lambda table: (table.get("rows"),table.get("columns"),sorted((c.get("row"),c.get("column"),c.get("row_span",1),c.get("column_span",1)) for c in table.get("cells",[])))
            if topology(left) != topology(right):
                conflicts.append({"code":"candidate_structure_conflict","severity":"high","table":ti,
                                  "candidate_id":ids[index],"selected_id":ids[chosen],"resolution":"pending_review"})
                continue
            lookup = {(c["row"],c["column"]):c for c in right.get("cells",[])}
            unmapped = []
            for cell in left.get("cells",[]):
                other = lookup[(cell["row"],cell["column"])]
                if (ti,cell["row"],cell["column"]) not in matched_slots and _normal(cell.get("text","")) != _normal(other.get("text","")):
                    unmapped.append((cell,other))
            if unmapped:
                critical = any(_field_kind(left,c) in {"id","numeric"} or _field_kind(right,o) in {"id","numeric"} for c,o in unmapped)
                conflicts.append({"code":"unmapped_candidate_difference","severity":"high" if critical else "medium","table":ti,
                                  "candidate_id":ids[index],"selected_id":ids[chosen],"resolution":"pending_review",
                                  "message":"转录不同且没有可靠单格对应，不能仅凭行列索引替换。","difference_count":len(unmapped)})
        for ti,cell,other,mapping in matches:
            value,alternative = str(cell.get("text","")),str(other.get("text",""))
            if _normal(value) == _normal(alternative):
                continue
            kind = _field_kind(selected["tables"][ti],cell)
            alternative_kind = _field_kind(result["tables"][ti],other)
            conflicts.append({"code":"candidate_value_conflict","severity":"high" if kind in {"id","numeric"} or alternative_kind in {"id","numeric"} else "medium",
                              "table":ti,"row":cell["row"],"column":cell["column"],"value":value,"alternative":alternative,
                              "candidate_id":ids[index],"selected_id":ids[chosen],"mapping":mapping,"resolution":"pending_review"})
    if allow_text_fixes:
        for ti,table in enumerate(selected.get("tables",[])):
            for cell in table.get("cells",[]):
                cb = _box(cell)
                if not cb or _field_kind(table,cell) != "text" or cell.get("geometry_origin") != "direct_detection":
                    continue
                evidence = [item for item in _evidence(text_evidence) if isinstance(item,dict) and (eb:=_box(item))
                            and _intersection(cb,eb)/_area(eb) >= .85 and item.get("confidence",0) >= .97
                            and item.get("source",item.get("engine")) and item.get("kind") not in UI_KINDS]
                if len(evidence) != 1:
                    continue
                item = evidence[0]
                other_matches = [other for other in table["cells"] if other is not cell and (ob:=_box(other))
                                 and _intersection(ob,_box(item))/_area(_box(item)) >= .2]
                current = str(cell.get("text",""))
                value = str(item.get("text",""))
                # Mixed codes are still numeric evidence even when the column
                # classifier calls them text. Protect both existing and proposed
                # Unicode digits, including superscripts, from automatic edits.
                if any(char.isdigit() for char in current) or any(char.isdigit() for char in value):
                    continue
                if other_matches or not value.strip() or _field_kind(table,{**cell,"text":value}) != "text" or _normal(value) == _normal(current):
                    continue
                if current.strip() and not (isinstance(cell.get("confidence"),(int,float)) and cell["confidence"] < .5):
                    continue
                cell["text"] = value
                modification = {"table":ti,"row":cell["row"],"column":cell["column"],"before":current,"after":value,
                                "source":item.get("source",item.get("engine")),"kind":"localized_text_fix", "policy_version":POLICY_VERSION,
                                "evidence_box":_box(item),"evidence_confidence":item["confidence"],"human_review":"not_performed"}
                cell.setdefault("provenance",[]).append(modification)
                modifications.append(modification)
                for conflict in conflicts:
                    if (conflict.get("table"),conflict.get("row"),conflict.get("column")) == (ti,cell["row"],cell["column"]) and conflict.get("severity") != "high":
                        conflict["resolution"] = "localized_text_evidence_applied"
    assessment = assess_screen_result(selected,image_info,text_evidence)
    unresolved = [c for c in conflicts if c.get("resolution","pending_review") == "pending_review"]
    if modifications:
        # Tables become authoritative; raw transcript remains immutable evidence.
        selected["original_text"] = selected.get("text","")
        selected["screen_text_modifications"] = modifications
    selected["screen_selection"] = {"policy_version":POLICY_VERSION,"candidate_id":ids[chosen],"human_review":"not_performed"}
    return {"policy_version":POLICY_VERSION,"selected_id":ids[chosen],"selected_result":selected,
            "selection_basis":"successful_table_then_lowest_risk_then_input_order",
            "assessments":[{"id":key,**assessments[key]} for key in ids],"assessment_by_id":assessments,
            "selected_assessment":assessment,"conflicts":conflicts,"modifications":modifications,
            "automatic_delivery":assessment["automatic_delivery"] and not unresolved,"review_required":assessment["review_required"] or bool(unresolved),
            "human_review":"not_performed","upgrade_kind":assessment["upgrade_kind"] or ("visual" if unresolved else None)}
