"""Generate new document/template-grouped screen simulations without model calls."""
from __future__ import annotations
import argparse
from copy import deepcopy
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import random
import sys

sys.dont_write_bytecode = True
from PIL import Image, ImageDraw, ImageFont
from common import ROOT, FORMAT, digest, fingerprint, fpath, frozen_bytes, frozen_json, read_json

SEED = 2026100702
OLD = ROOT / "output/ocr-screen-benchmark-20261007/dataset-v2"
SCREEN_GENERATOR = ROOT / "scripts/screen_benchmark/generate.py"
FAMILIES = {
    "development": ["procurement_delivery_v3", "reimbursement_matrix_v3", "project_hours_v3", "payment_schedule_v3"],
    "validation": ["stock_reconcile_v3", "quarterly_budget_v3", "contract_detail_v3", "receivable_aging_v3"],
    "holdout": ["grant_allocation_v3", "asset_transfer_v3", "ledger_adjustments_v3", "expense_crosswalk_v3"],
}
UI_MARKERS = ["Excel", "SUM(C2:C11)", "C12", "粘贴", "排序和筛选", "保存更改", "文件", "开始", "状态：就绪"]


def save_image(path, image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    frozen_bytes(path, stream.getvalue())


def font(size, bold=False):
    return ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc", size)


def wrap(value, face, width):
    lines = []
    for line in str(value).split("\n"):
        current = ""
        for char in line:
            if current and face.getlength(current + char) > width:
                lines.append(current)
                current = ""
            current += char
        lines.append(current)
    return lines


def table(values, widths, caption, unit, note, merges=(), size=19):
    """Explicit anchor cells and spans; wrapping never clips visible business text."""
    face, bold = font(size), font(size, True)
    anchors = {(r, c): (rs, cs, str(value)) for r, c, rs, cs, value in merges}
    covered = {(r + dr, c + dc) for r, c, rs, cs, _ in merges
               for dr in range(rs) for dc in range(cs) if dr or dc}
    row_height = max(46, 14 + (size + 6) * max(
        len(wrap(anchors.get((r, c), (1, 1, str(value)))[2], bold if r == 0 else face,
                 sum(widths[c:c + anchors.get((r, c), (1, 1, ""))[1]]) - 16))
        for r, row in enumerate(values) for c, value in enumerate(row) if (r, c) not in covered))
    width = sum(widths) + 48
    height = 102 + row_height * len(values) + 100
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 12), caption, font=font(24, True), fill="#252b29")
    draw.text((24, 54), unit, font=font(18), fill="#252b29")
    context = [{"kind": "title", "text": caption, "box": [24, 10, width - 24, 44]},
               {"kind": "unit", "text": unit, "box": [24, 51, width - 24, 79]}]
    xs = [24]
    for w in widths:
        xs.append(xs[-1] + w)
    cells = []
    occupied = set()
    for r, row in enumerate(values):
        if len(row) != len(widths):
            raise ValueError("Synthetic row width differs")
        for c, value in enumerate(row):
            if (r, c) in covered:
                continue
            rs, cs, value = anchors.get((r, c), (1, 1, str(value)))
            positions = {(r + dr, c + dc) for dr in range(rs) for dc in range(cs)}
            if occupied & positions or r + rs > len(values) or c + cs > len(widths):
                raise ValueError("Synthetic overlapping/out-of-range span")
            occupied |= positions
            box = [xs[c], 102 + r * row_height, xs[c + cs], 102 + (r + rs) * row_height]
            draw.rectangle(box, fill="#e2ebe6" if r == 0 else "white", outline="#6c7d73", width=1)
            lines = wrap(value, bold if r == 0 else face, box[2] - box[0] - 16)
            if 14 + len(lines) * (size + 6) > box[3] - box[1]:
                raise ValueError("Synthetic text would be clipped")
            for number, line in enumerate(lines):
                draw.text((box[0] + 8, box[1] + 7 + number * (size + 6)), line,
                          font=bold if r == 0 else face, fill="#202923")
            cells.append({"row": r, "column": c, "row_span": rs, "column_span": cs,
                          "text": value, "box": box,
                          "tags": ["critical"] if any(ch.isdigit() for ch in value) else []})
    y = 118 + row_height * len(values)
    for i, line in enumerate(wrap(note, font(17), width - 48)):
        draw.text((24, y + i * 24), line, font=font(17), fill="#252b29")
    context.append({"kind": "note", "text": note, "box": [24, y - 2, width - 24, height - 8]})
    return image, {"rows": len(values), "columns": len(widths), "cells": cells, "caption": ""}, context


def combine(parts):
    width = max(p[0].width for p in parts)
    height = sum(p[0].height for p in parts) + 60 * (len(parts) - 1)
    image = Image.new("RGB", (width, height), "white")
    tables, context, y = [], [], 0
    for ti, (part, source_table, targets) in enumerate(parts):
        image.paste(part, (0, y))
        target = deepcopy(source_table)
        for cell in target["cells"]:
            cell["box"] = [cell["box"][0], cell["box"][1] + y, cell["box"][2], cell["box"][3] + y]
        tables.append(target)
        for item in targets:
            context.append({**item, "table_index": ti,
                            "box": [item["box"][0], item["box"][1] + y, item["box"][2], item["box"][3] + y]})
        y += part.height + 60
    return image, tables, context


def chinese(family, index, split):
    # New values and structures are independent of old FX03 fixture semantics.
    base = Decimal(1200 + index * 37) / 100
    a, b = f"{base:.2f}", f"{-base / 10:.2f}"
    ident, long_id = f"{index + 1007:08d}", f"20261007{index + 5700:016d}"
    note = "表注：括号表示冲回；空格为未填，重复业务保留原文。"
    unit = "单位：元；数量单位：件"
    caption = f"审计模拟 {split} {family} {index + 1:02d}"
    name = family.removesuffix("_v3")
    parts = []
    if name == "procurement_delivery":
        values = [["采购单", "供应商", "已验收", "待验收", "单价", "合同流水"],
                  [ident, "青禾设备", "12", "3", a, long_id], ["", "另批交付", "0", "8", b, long_id],
                  ["备注", "保留交付批次", "", "", "=0.8", ""]]
        merges = [(1, 0, 2, 1, ident), (3, 1, 1, 3, "保留交付批次")]
        widths = [155, 175, 125, 125, 135, 310]
    elif name == "reimbursement_matrix":
        values = [["报销事项", "经办核对", "", "财务核对", ""], ["", "原报", "核准", "凭证", "处理"],
                  ["住宿", a, a, ident, "未付款"], ["差旅", "(20.00)", "−20.00", ident, "冲回"]]
        merges = [(0, 0, 2, 1, "报销事项"), (0, 1, 1, 2, "经办核对"), (0, 3, 1, 2, "财务核对")]
        widths = [195, 165, 165, 175, 160]
    elif name == "project_hours":
        values = [["项目组", "人员", "计划工时", "实际工时", "执行率", "核验日"],
                  ["研发甲组", "张澄", "40", "32", "80%", "2026-10-07"], ["", "陈嘉", "40", "40", "100%", "2026-10-07"],
                  ["研发乙组", "孙宁", "20", "0", "0%", "2026-10-07"]]
        merges = [(1, 0, 2, 1, "研发甲组")]
        widths = [175, 145, 160, 160, 150, 190]
    elif name == "payment_schedule":
        values = [["合同", "付款期", "约定", "已付", "差额"], [ident, "首期", a, "0.00", a],
                  ["", "尾期", "1,234.56", "1,200.00", "34.56"], ["提示", "+SUM(A1:A2)", "", "", ""]]
        merges = [(1, 0, 2, 1, ident), (3, 1, 1, 4, "+SUM(A1:A2)")]
        widths = [180, 230, 190, 190, 190]
    elif name == "stock_reconcile":
        values = [["物料", "账面", "盘点", "损溢", "仓位", "流水"], ["配件甲", "100", "99", "-1", "A-08", long_id],
                  ["配件乙", "0", "0", "0", "B-09", ident], ["配件丙", "", "3", "待查", "C-01", ident]]
        merges, widths = [], [185, 120, 120, 140, 150, 310]
    elif name == "quarterly_budget":
        values = [["科目", "第一季", "", "第二季", "", "全年"], ["", "预算", "执行", "预算", "执行", ""],
                  ["维修", a, b, "30.00", "20.00", "50.00"], ["设备", "1,234.56", "0.00", "0.00", "0.00", "1,234.56"]]
        merges = [(0, 0, 2, 1, "科目"), (0, 1, 1, 2, "第一季"), (0, 3, 1, 2, "第二季"), (0, 5, 2, 1, "全年")]
        widths = [175] * 6
    elif name == "contract_detail":
        values = [["合同信息", "", "", ""], ["合同编号", long_id, "签订日", "2026-10-07"],
                  ["结算单位", "星河服务", "履约比例", "32.64%"], ["付款依据", "验收记录\n完整原文保留", "金额", a],
                  ["附件", "", "备注", "B / C 是业务代码"]]
        merges, widths = [(0, 0, 1, 4, "合同信息")], [165, 340, 165, 245]
    elif name == "receivable_aging":
        values = [["单位", "0–30天", "31–60天", "61–90天", "90天以上", "调整", "余额"],
                  ["单位甲", a, "0.00", "", "20.00", b, "20.00"], ["单位乙", "0.00", "1,234.56", "0.00", "", "−20.00", "1,214.56"],
                  ["合计", a, "1,234.56", "0.00", "20.00", b, "按原值"]]
        merges, widths = [], [180, 145, 155, 155, 160, 150, 165]
    elif name == "grant_allocation":
        values = [["项目", "拨款", "", "", "核验", ""], ["", "计划", "实际", "", "编号", "状态"],
                  ["", "", "到账", "差额", "", ""], ["专项甲", a, a, "0.00", ident, "待审核"],
                  ["专项乙", "1,234.56", "1,214.56", "20.00", ident, "冲回待核"]]
        merges = [(0, 0, 3, 1, "项目"), (0, 1, 1, 3, "拨款"), (0, 4, 1, 2, "核验"),
                  (1, 1, 2, 1, "计划"), (1, 2, 1, 2, "实际"), (1, 4, 2, 1, "编号"), (1, 5, 2, 1, "状态")]
        widths = [195, 175, 175, 170, 200, 200]
    elif name == "asset_transfer":
        parts.append(table([["移出单位", "资产编号", "数量", "原值"], ["单位甲", long_id, "2", a],
                            ["单位乙", ident, "0", "0.00"]], [215, 335, 130, 180], caption + " 移出", unit, note))
        values = [["接收项目", "对应编号", "验收", "账面"], ["实验项目", long_id, "已验收", a],
                  ["后勤项目", ident, "待补", "(20.00)"]]
        merges, widths = [], [215, 335, 160, 180]
        caption += " 移入"
    elif name == "ledger_adjustments":
        values = [["月份", "核对事项", "借方", "贷方", "流水", "备注"], ["10月", "暂估冲回", "−20.00", "", ident, "保留负号"],
                  ["", "补录", "", a, long_id, "按原值"], ["", "补录", "", a, long_id, "同项重复"],
                  ["11月", "未填写", "", "", ident, ""], ["", "零额", "0.00", "0.00", ident, "真实零"]]
        merges = [(1, 0, 3, 1, "10月"), (4, 0, 2, 1, "11月")]
        widths = [135, 205, 150, 150, 310, 190]
    elif name == "expense_crosswalk":
        values = [["科目", "部门", "项目", "编号", "预算", "发生", "比例", "币种", "说明"],
                  ["维修", "行政", "设备", ident, "1,234.56", a, "32.64%", "CNY", "保留原文\n分行说明"],
                  ["服务", "技术", "接口", ident, "0.00", b, "0%", "CNY", "冲回"],
                  ["服务", "技术", "接口", ident, "0.00", b, "0%", "CNY", "重复业务"]]
        merges, widths = [], [125, 125, 145, 185, 165, 150, 150, 125, 240]
        unit = "金额单位：万元；比例为百分数"
    else:
        raise ValueError(f"Unknown independent family: {family}")
    parts.append(table(values, widths, caption, unit, note, merges))
    return combine(parts)


def public_pool(old_registry):
    used_groups = {s["group_id"] for s in old_registry["sources"] if s["cohort"] == "public"}
    used_hashes = {s["source_sha256"] for s in old_registry["sources"]}
    sources = []
    for directory in ["table-matching-v2", "tableformer-expanded-20260914"]:
        base = ROOT / "build" / directory / "dataset"
        manifest, annotation = base / "inputs/test.json", base / "sealed/test.annotations.json"
        labels = {s["id"]: s for s in read_json(annotation)["samples"]}
        for item in read_json(manifest)["samples"]:
            if item["group_id"] in used_groups or item["sha256"] in used_hashes:
                continue
            path = fpath(manifest.parent / item["image"])
            label = labels[item["id"]]
            if digest(path) != item["sha256"] or label["sha256"] != item["sha256"]:
                raise ValueError("Public source bytes/annotation binding differ")
            cells = deepcopy(label["targets"])
            sources.append({"id": item["id"], "group_id": item["group_id"], "cohort": "public",
                            "path": str(path), "source_sha256": item["sha256"],
                            "source_manifest": str(manifest), "source_manifest_sha256": digest(manifest),
                            "annotation_path": str(annotation), "annotation_sha256": digest(annotation),
                            "source_annotation_sha256": fingerprint(label),
                            "tables": [{"rows": max(c["row"] + c["row_span"] for c in cells),
                                        "columns": max(c["column"] + c["column_span"] for c in cells),
                                        "caption": "", "cells": cells}], "context": [],
                            "metadata_truth_scope": "crop has no external business metadata; public caption/footnote outside source unavailable"})
    if len({s["group_id"] for s in sources}) != len(sources):
        raise ValueError("Public pool must contain one table per distinct original document")
    sources.sort(key=lambda s: fingerprint([SEED, s["group_id"]]))
    if len(sources) < 180:
        raise ValueError("Insufficient document-independent public sources")
    return sources


def generate(output):
    output = fpath(output)
    output.mkdir(parents=True, exist_ok=True)
    old_registry = read_json(OLD / "source-registry.json")
    pool = public_pool(old_registry)
    public = {"development": pool[:36], "validation": pool[36:60], "holdout": pool[60:180]}
    sources = []
    plans = {"development": [], "validation": [], "holdout": [], "challenge": []}
    for split in public:
        for number, source in enumerate(public[split]):
            source = {**source, "split": split, "family": None}
            sources.append(source)
            plans[split].append((source, "normal" if number % 2 == 0 else "hard", None))
    counts = {"development": 2, "validation": 2, "holdout": 15}
    for split, families in FAMILIES.items():
        for family in families:
            for index in range(counts[split]):
                base, tables, context = chinese(family, index, split)
                source_id = f"zh3-{split}-{family}-{index:02d}"
                source_path = output / "sources" / (source_id + ".png")
                save_image(source_path, base)
                source = {"id": source_id, "group_id": "zh3-template-" + family, "cohort": "chinese",
                          "split": split, "family": family, "path": str(source_path), "source_sha256": digest(source_path),
                          "source_annotation_sha256": fingerprint({"tables": tables, "context": context}),
                          "tables": tables, "context": context, "semantic_source": "independent literal v3 generator",
                          "metadata_truth_scope": "all synthetic title/unit/note targets, text/kind/order annotated"}
                sources.append(source)
                levels = ["clean", "normal", "hard"] if split == "development" else ["normal", "hard"]
                plans[split].extend((source, level, None) for level in levels)
    for ordinal in range(6):
        family = FAMILIES["holdout"][ordinal % 4]
        base, tables, context = chinese(family, 100 + ordinal, "holdout")
        source_id = f"zh3-challenge-{family}-{ordinal:02d}"
        source_path = output / "sources" / (source_id + ".png")
        save_image(source_path, base)
        source = {"id": source_id, "group_id": "zh3-template-" + family, "cohort": "chinese",
                  "split": "holdout", "family": family, "path": str(source_path), "source_sha256": digest(source_path),
                  "source_annotation_sha256": fingerprint({"tables": tables, "context": context}),
                  "tables": tables, "context": context, "semantic_source": "independent literal v3 generator",
                  "metadata_truth_scope": "visible synthetic title/unit/note annotated; challenge uncertainty explicit"}
        sources.append(source)
        plans["challenge"].extend((source, "hard", kind) for kind in ["occlusion", "clipping", "tiny", "no_table"])
    grouped = {split: {s["group_id"] for s in sources if s["split"] == split} for split in public}
    if any(grouped[left] & grouped[right] for left, right in [("development", "validation"), ("development", "holdout"), ("validation", "holdout")]):
        raise ValueError("Source/template split leakage")
    spec = importlib.util.spec_from_file_location("existing_screen_simulator", SCREEN_GENERATOR)
    screen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(screen)
    registry = {"format": FORMAT, "seed": SEED, "families": FAMILIES,
                "old_registry": str(OLD / "source-registry.json"), "old_registry_sha256": digest(OLD / "source-registry.json"),
                "split_policy": "Split original public document and new synthetic template family before transformations. Challenge shares holdout families, has separate source images.",
                "prior_exposure": "Disjoint from all dataset-v2 public documents, hashes and Chinese family names. Public sources existed in historical non-screen TableFormer projects; no first-ever/model-training blind claim.",
                "scope": "Simulated screens; not real mobile screen-photo accuracy. Public source crops lack external title/note truth.",
                "unselected_public_documents": [s["group_id"] for s in pool[180:]],
                "sources": [{k: v for k, v in s.items() if k not in {"tables", "context"}} for s in sources]}
    frozen_json(output / "source-registry.json", registry)
    for split, plan in plans.items():
        inputs, truth = [], []
        for ordinal, (source, level, challenge) in enumerate(plan):
            sample_id = f"{split}-{ordinal:03d}-{source['id']}-{challenge or level}"
            sample_seed = SEED + int(fingerprint(sample_id)[:8], 16)
            with Image.open(source["path"]) as original:
                base = original.convert("RGB")
            im, tables, context, ui, occ, visibility, params, matrix, screen_matrix = screen.render_screen(
                base, source["tables"], source["context"], level, random.Random(sample_seed), challenge)
            image_path = output / split / "images" / (sample_id + ".png")
            occ_path = output / split / "masks" / (sample_id + "-occlusion.png")
            vis_path = output / split / "masks" / (sample_id + "-visibility.png")
            for target, image in [(image_path, im), (occ_path, occ), (vis_path, visibility)]:
                save_image(target, image)
            for target in context:
                polygon = target["polygon"]
                if challenge == "tiny":
                    status = "readability_uncertain"
                elif any(x < 0 or y < 0 or x > im.width or y > im.height for x, y in polygon):
                    status = "clipped"
                else:
                    mask = Image.new("L", im.size, 0)
                    ImageDraw.Draw(mask).polygon([tuple(p) for p in polygon], fill=255)
                    import numpy as np
                    status = "occluded" if ((np.asarray(mask) > 0) & (np.asarray(occ) > 0)).any() else "visible"
                target["visibility"] = status
            # Add horizontal ruler geometry missing from the historical UI region list.
            ruler = [[48, 217], [params['screen_size'][0], 217], [params['screen_size'][0], 239], [48, 239]]
            ui.append({"kind": "column_ruler", "text": "A B C D E F G H I J K L M N O P Q R S T", "polygon": screen.points(screen_matrix, ruler)})
            inputs.append({"id": sample_id, "image": str(image_path), "image_sha256": digest(image_path),
                           "input_kind": "full-screen-photo-simulation", "width": im.width, "height": im.height})
            truth.append({"id": sample_id, "image_sha256": digest(image_path), "cohort": source["cohort"],
                          "split": split, "group_id": source["group_id"], "source_id": source["id"], "family": source["family"],
                          "source_sha256": source["source_sha256"], "level": level, "challenge": challenge,
                          "tables": tables, "context_targets": context, "ui_regions": ui, "ui_markers": UI_MARKERS,
                          "metadata_truth_scope": source["metadata_truth_scope"], "source_to_image": matrix, "screen_to_image": screen_matrix,
                          "parameters": params, "occlusion_mask": str(occ_path), "occlusion_mask_sha256": digest(occ_path),
                          "visibility_mask": str(vis_path), "visibility_mask_sha256": digest(vis_path),
                          "expected_risk": challenge not in {None, "no_table"}, "expect_no_table": challenge == "no_table"})
        frozen_json(output / split / "inputs.json", {"format": FORMAT, "name": split, "seed": SEED,
                    "truth_available_to_inference": False, "samples": inputs})
        frozen_json(output / split / "truth.json", {"format": FORMAT, "name": split, "samples": truth})
        frozen_json(output / split / "freeze.json", {"format": FORMAT, "count": len(inputs), "seed": SEED,
                    "inputs_sha256": digest(output / split / "inputs.json"), "truth_sha256": digest(output / split / "truth.json"),
                    "source_registry_sha256": digest(output / "source-registry.json"), "generator_sha256": digest(__file__),
                    "screen_renderer_sha256": digest(SCREEN_GENERATOR), "source_count": len({t["source_id"] for t in truth}),
                    "group_count": len({t["group_id"] for t in truth}),
                    "cohorts": {c: sum(t["cohort"] == c for t in truth) for c in ["public", "chinese"]},
                    "purpose": "development/validation choose policy; holdout/challenge validate once after source/policy freeze"})
        print(json.dumps({"split": split, "count": len(inputs), "inputs": str(output / split / "inputs.json")}), flush=True)
    frozen_json(output / "freeze.json", {"format": FORMAT, "source_registry_sha256": digest(output / "source-registry.json"),
                "sets": {name: {"freeze_sha256": digest(output / name / "freeze.json"), "count": len(plan)} for name, plan in plans.items()},
                "old_sets": {name: {"path": str(OLD / name / "freeze.json"), "freeze_sha256": digest(OLD / name / "freeze.json"),
                                      "role": "known-error regression only; do not rerun old 240/24 here"} for name in ["final", "challenge"]}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    generate(parser.parse_args().output)
