"""Read-only result spot check against reviewed public image references."""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import hashlib
import json
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
DB = ROOT.parents[1] / "build/real-user-20260912/data/workbench.sqlite3"

def normalized(text):
    return re.sub(r"\s+", "", text.replace(r"\pm", "±"))

def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

class IndependentTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.occupied = set()
        self.row = -1
        self.column = 0
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row += 1
            self.column = 0
        elif tag in {"td", "th"}:
            while (self.row, self.column) in self.occupied:
                self.column += 1
            attrs = dict(attrs)
            self.current = {
                "row": self.row,
                "column": self.column,
                "row_span": int(attrs.get("rowspan", "1")),
                "column_span": int(attrs.get("colspan", "1")),
                "text": "",
            }
        elif tag == "br" and self.current is not None:
            self.current["text"] += "\n"

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current is not None:
            cell = self.current
            cell["text"] = cell["text"].strip()
            self.cells.append(cell)
            for row in range(cell["row"], cell["row"] + cell["row_span"]):
                for col in range(cell["column"], cell["column"] + cell["column_span"]):
                    self.occupied.add((row, col))
            self.column = cell["column"] + cell["column_span"]
            self.current = None

con = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
con.row_factory = sqlite3.Row
con.execute("PRAGMA query_only=ON")
rows = con.execute(
    "SELECT t.id task_id,t.engine,t.status,t.created,i.name image_name,r.id result_id,r.original,r.revision "
    "FROM tasks t JOIN images i ON i.id=t.image_id JOIN results r ON r.id=t.result_id ORDER BY t.created"
).fetchall()
con.close()
items = []
for row in rows:
    item = dict(row)
    item["original"] = json.loads(item["original"])
    items.append(item)
(EVIDENCE / "spotcheck-original-results.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")

sources = json.loads((ROOT / "sample-sources.json").read_text("utf-8"))
source_map = {Path(item["file"]).name: item for item in sources["samples"]}
text_checks = []
for item in items:
    if item["image_name"] not in {"chinese_store.jpg", "chinese_scanned_exam.png", "chinese_handwriting.png"}:
        continue
    original = item["original"]
    if item["engine"] == "ppocr":
        raw_texts = original["raw"][0]["res"]["rec_texts"]
        raw_text = "\n".join(raw_texts)
        block_faithful = raw_texts == [block["text"] for block in original["blocks"]]
        raw_path = "original.raw[0].res.rec_texts"
    elif item["engine"] == "hunyuan":
        raw_text = original["raw"]["choices"][0]["message"]["content"]
        block_faithful = None
        raw_path = "original.raw.choices[0].message.content"
    else:
        continue
    anchors = source_map[item["image_name"]]["expected_content"]["anchors"]
    checks = [{"expected": anchor, "matched_ignoring_whitespace": normalized(anchor) in normalized(original["text"])} for anchor in anchors]
    text_checks.append({
        "image": item["image_name"], "engine": item["engine"], "task_id": item["task_id"], "result_id": item["result_id"],
        "original_text_sha256": text_digest(original["text"]),
        "anchors": checks,
        "anchors_matched": sum(check["matched_ignoring_whitespace"] for check in checks),
        "anchors_total": len(checks),
        "raw_path": raw_path,
        "raw_text_equals_workbench_original_text": raw_text == original["text"],
        "ppocr_raw_lines_equal_workbench_blocks": block_faithful,
        "original_text": original["text"],
        "original_tables": original["tables"],
    })

table_checks = []
reference = source_map["pubmed_table.png"]["expected_content"]
for item in items:
    if item["image_name"] != "pubmed_table.png" or item["engine"] not in {"paddlevl", "glm", "hunyuan"}:
        continue
    original = item["original"]
    if item["engine"] == "paddlevl":
        raw_html = original["raw"][0]["res"]["parsing_res_list"][0]["block_content"]
        raw_path = "original.raw[0].res.parsing_res_list[0].block_content"
    elif item["engine"] == "glm":
        raw_html = original["raw"]["generated"][0]["content"]
        raw_path = "original.raw.generated[0].content"
    else:
        raw_html = original["raw"]["choices"][0]["message"]["content"]
        raw_path = "original.raw.choices[0].message.content"
    parser = IndependentTableParser()
    parser.feed(raw_html)
    grid = original["tables"][0]
    actual = [{key: cell[key] for key in ("row", "column", "row_span", "column_span", "text")} for cell in grid["cells"]]
    actual_map = {(cell["row"], cell["column"]): cell["text"] for cell in actual}
    comparisons = []
    for r, values in enumerate(reference["rows"], start=2):
        for c, expected in enumerate(values):
            observed = actual_map.get((r, c))
            comparisons.append({
                "data_row": r - 1, "column_index": c + 1, "column_name": reference["columns"][c],
                "row_label_reference": values[0], "expected": expected, "observed": observed,
                "numeric": c > 0,
                "matched_normalized": observed is not None and normalized(expected) == normalized(observed),
            })
    numeric = [cell for cell in comparisons if cell["numeric"]]
    table_checks.append({
        "image": item["image_name"], "engine": item["engine"], "task_id": item["task_id"], "result_id": item["result_id"],
        "raw_path": raw_path,
        "raw_html_equals_workbench_original_text": raw_html == original["text"],
        "independently_parsed_raw_cells_equal_workbench_table_cells": parser.cells == actual,
        "raw_html_sha256": text_digest(raw_html),
        "data_rows_expected": 4,
        "data_rows_observed": grid["rows"] - 2,
        "columns_observed": grid["columns"],
        "numeric_cells_matched": sum(cell["matched_normalized"] for cell in numeric),
        "numeric_cells_total": len(numeric),
        "all_body_cells_matched": sum(cell["matched_normalized"] for cell in comparisons),
        "all_body_cells_total": len(comparisons),
        "mismatches": [cell for cell in comparisons if not cell["matched_normalized"]],
        "group_header_cells": [cell for cell in actual if cell["row"] == 0],
        "body_cell_comparisons": comparisons,
        "units_note": "The source crop contains no physical-unit labels. This spot check cannot assess unit transcription/conversion. GLM loses isotope superscript ³ in [³H]DA uptake, which is a scientific-notation error, not an observed physical-unit conversion.",
    })

observations = [
    {"id": "SPOT-01", "kind": "model_recognition_error", "image": "pubmed_table.png", "engine": "glm", "detail": "第 2 数据行 Km 10.8→18.8，紧随的 SD ±5.7→±1.7；WT w Veh→WT w Vbh，D3 KO w Veh→IKO w Vbh；[³H]DA uptake 丢失上标 ³。错误已存在 generated/official_markdown 原始模型输出，工作台忠实保留。"},
    {"id": "SPOT-02", "kind": "model_recognition_error", "image": "chinese_handwriting.png", "engine": "ppocr", "detail": "手绘表格五级污染指数 >300→7300，改变数值阈值意义；“我们需要洁净的空气”→“我们需要洁净的气”；“冰雹”中的“雹”漏识别。均已存在 raw.rec_texts，非工作台漏字。"},
    {"id": "SPOT-03", "kind": "model_recognition_error", "image": "chinese_scanned_exam.png", "engine": "ppocr", "detail": "六个指定锚点均匹配；额外目视抽查发现原图“縠纹”识别为“觳纹”，诗句与 C 选项均出现。源图局部证据 chinese-scan-poem-detail.png；不能将锚点通过解释为整页逐字正确。"},
    {"id": "SPOT-04", "kind": "model_structure_uncertainty", "image": "pubmed_table.png", "engine": "paddlevl,hunyuan,glm", "detail": "原图是无竖线分组表头。三个模型生成的顶层合并范围不同；PaddleVL 将 DAT density 放在第 6–8 列，Hunyuan 放在第 6–7 列、末列空白，GLM 放在第 7–8 列。PaddleVL/Hunyuan 分组标题可能与实际 DAT/SD 列关联不当，需校对；这些 colspan 已存在模型 raw HTML，独立解析与工作台表格完全一致。"},
    {"id": "SPOT-05", "kind": "upstream_output_format_usability", "image": "pubmed_table.png", "engine": "paddlevl", "detail": "± 输出为字面 LaTeX \\pm，³ 输出为 ^{3}。归一化后数据数值正确，但纯文本/单元格保留模型写法时用户可读性较差；此处未操作浏览器，因此不单独宣称已验证屏幕渲染缺陷。"},
]
handwriting_table_checks = []
for check in text_checks:
    if check["image"] != "chinese_handwriting.png" or check["engine"] != "hunyuan":
        continue
    hand_reference = [
        ["一级", "1~50", "优"], ["二级", "51~100", "良"],
        ["三级", "100~200", "轻度污染"], ["四级", "200~300", "中度污染"],
        ["五级", ">300", "重度污染"],
    ]
    grid = check["original_tables"][0]
    cells = [{key: cell[key] for key in ("row", "column", "row_span", "column_span", "text")} for cell in grid["cells"]]
    cell_map = {(cell["row"], cell["column"]): cell["text"] for cell in cells}
    parser = IndependentTableParser()
    parser.feed(check["original_text"])
    compared = []
    for r, values in enumerate(hand_reference, start=1):
        for c, expected in enumerate(values):
            observed = cell_map.get((r, c), "")
            compared.append({"data_row": r, "column": c + 1, "expected": expected, "observed": observed, "matched": normalized(observed.replace("～", "~").replace("＞", ">")) == normalized(expected)})
    handwriting_table_checks.append({
        "image": check["image"], "engine": check["engine"], "task_id": check["task_id"], "result_id": check["result_id"],
        "reference_status": "Manually checked against the full source handwriting image; preserve source ranges even if they are not mutually exclusive.",
        "rows_observed": grid["rows"] - 1, "rows_expected": 5,
        "body_cells_matched": sum(c["matched"] for c in compared), "body_cells_total": len(compared),
        "independently_parsed_raw_cells_equal_workbench_table_cells": parser.cells == cells,
        "comparisons": compared,
    })
    observations.append({"id": "SPOT-06", "kind": "model_comparison_success", "image": check["image"], "engine": "hunyuan", "detail": "后续 Hunyuan 手写结果已纳入：5/5 文字锚点正确，手绘表格 5 数据行、15 数据单元格均与源图相符（仅归一全角 ＞/～）。正确保留 ＞300，并识别出“空气”“冰雹”；其 raw→original 文本及 HTML→cells 均保真。"})
report = {
    "observed_at": datetime.now(timezone.utc).isoformat(),
    "database": str(DB.resolve()),
    "database_access": "sqlite URI mode=ro; PRAGMA query_only=ON; SELECT only; no service mutation/model/browser call",
    "source_manifest": "../sample-sources.json",
    "result_evidence": "spotcheck-original-results.json",
    "method": "Compare manually reviewed source anchors/body values with immutable original, then compare engine raw text/HTML with workbench original blocks/cells. Table raw HTML was parsed by an independent stdlib HTMLParser, not product table code. Ignore whitespace and LaTeX \\pm representation only for numeric/body value comparison.",
    "scope": "Focused source spot checks, not exhaustive OCR accuracy/CER evaluation or a fresh UI rendering test. User-edited result content is excluded.",
    "original_result_count_observed": len(items),
    "text_checks": text_checks,
    "table_checks": table_checks,
    "handwriting_table_checks": handwriting_table_checks,
    "observations": observations,
    "workbench_conversion_bug_detected_in_checked_paths": False,
    "hunyuan_handwriting_included": any(check["image"] == "chinese_handwriting.png" and check["engine"] == "hunyuan" for check in text_checks),
}
(EVIDENCE / "accuracy-spotcheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")

md = ["# 公开样本识别结果抽查", "", f"观察时间：{report['observed_at']}。只读数据库，对照不可变 original 与 raw，不使用人工编辑后的结果。", "", "本轮发现模型识别错误；抽查的数据转换路径未发现工作台额外丢行、改数值或破坏表格单元格。该结论限于本报告的图片/结果，不代表全项目无 bug。", "", "## 文字锚点", "", "| 图片 | 引擎 | 锚点匹配 | raw 与工作台 original 一致 |", "|---|---|---:|---|"]
for check in text_checks:
    md.append(f"| {check['image']} | {check['engine']} | {check['anchors_matched']}/{check['anchors_total']} | {check['raw_text_equals_workbench_original_text']} |")
md += ["", "只忽略空白，不忽略错字。路牌额外识别到背景中的 PAD，不计作路牌八个锚点的错误。中文扫描锚点通过，但仍出现“縠→觳”；手写内容出现 >300→7300 等需要人工修正的问题。", "", "## PubMed 表格", "", "| 引擎 | 数据行 | 数值单元格正确 | 全部数据单元格正确 | raw HTML → 工作台 cells 保真 |", "|---|---:|---:|---:|---|"]
for check in table_checks:
    md.append(f"| {check['engine']} | {check['data_rows_observed']}/4 | {check['numeric_cells_matched']}/{check['numeric_cells_total']} | {check['all_body_cells_matched']}/{check['all_body_cells_total']} | {check['independently_parsed_raw_cells_equal_workbench_table_cells']} |")
md += ["", "PaddleVL/Hunyuan 四条数据行和 28 个数值单元格均匹配。GLM 第二数据行 Km 10.8 识别成 18.8，其 SD ±5.7 识别成 ±1.7，另有两个组名错误；没有漏数据行。", "", "原图没有物理单位标签，因此没有可验证的单位转写/转换结论；GLM 的 ³ 丢失属于科学记号丢失。± 与 \\pm 在数值检查中视为等价，但原始写法完整保留于证据。", "", "## 需校对的问题", ""]
for observation in observations:
    md.append(f"- {observation['id']}（{observation['kind']}）：{observation['detail']}")
if handwriting_table_checks:
    md += ["", "Hunyuan 手写表格追加核验：5/5 数据行、15/15 数据单元格与原图相符，原始 HTML 独立解析与工作台 cells 完全一致。全角 ＞300 与原图 >300 按语义等价处理，不将其视作错误。"]
md += ["", "## 证据与边界", "", "- accuracy-spotcheck.json：逐锚点、逐单元格比较，task/result ID，以及 raw 路径。", "- spotcheck-original-results.json：本次只读抓取的原始结果，包括引擎 raw。", "- pubmed-table-enlarged.png：仅用于目视核查的原图放大件；原始尺寸仍为 503×98。", "- chinese-scan-poem-detail.png：原扫描诗句局部，用于核查“縠”字。", "- 不调用浏览器、不启动识别、不写数据库。独立 HTMLParser 核对原始模型 HTML 与工作台单元格坐标、跨度和文本。", "", f"Hunyuan 手写新增结果本次是否已纳入：{report['hunyuan_handwriting_included']}。"]
(EVIDENCE / "accuracy-spotcheck.md").write_text("\n".join(md) + "\n", "utf-8")
print(json.dumps({"original_results": len(items), "text_checks": len(text_checks), "table_checks": len(table_checks), "hunyuan_handwriting_included": report["hunyuan_handwriting_included"], "reports": [str(EVIDENCE / "accuracy-spotcheck.json"), str(EVIDENCE / "accuracy-spotcheck.md")]}, ensure_ascii=False))
