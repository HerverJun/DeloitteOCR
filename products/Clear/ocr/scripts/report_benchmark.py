"""Produce measured metrics and an escaped, local-only error gallery.

Run with a build Python containing matplotlib. Public images and transcriptions
stay in the research evidence directory, never in the distributable bundle.
"""

import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import statistics
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--benchmark", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--table-review", type=Path)
    a = p.parse_args()
    data = json.loads(a.evaluation.read_text("utf-8"))
    benchmark = json.loads(a.benchmark.read_text("utf-8"))
    if not data["complete"] or data["benchmark_sha256"] != sha(a.benchmark):
        raise ValueError("A complete evaluation bound to this benchmark is required")
    a.output.mkdir(parents=True, exist_ok=True)
    samples = {s["id"]: s for s in benchmark["samples"]}
    for sample in samples.values():
        if sha(a.benchmark.parent / sample["image"]) != sample["image_sha256"]:
            raise ValueError("Benchmark image changed: " + sample["id"])
    engines = ["ppocr", "paddlevl", "glm", "hunyuan"]
    esc = html.escape
    lines = [
        "# 公开标注区域评测",
        "",
        "本次实际运行四引擎各 200 张，共 800 次；78 手写、82 印刷、40 表格。",
        "",
        "数据为 OmniDocBench 的 200 个不同真实页面中的标注区域。原始标注未经独立校正，部分框包含邻行残片。研究使用限制适用；本报告不能替代内网样本、完整手机照片或 A4000 验收。",
        "",
        "CER 按整个类别的编辑距离总和除以参考字符总和，NFC 后去空白，保留大小写与标点；可大于 100%。失败项按空输出计入。数字字段是预先定义的数字 token 或包含数字的表格单元格，不代表业务字段识别率。结构 Span F1 比较表号、行列与合并关系，不是 TEDS。",
        "",
        "| 引擎 | 印刷 CER | 手写 CER | 表格 Span F1 | 结构完全一致 | 失败数 / 200 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    gallery = [
        '<!doctype html><meta charset="utf-8"><title>公开评测错误样例</title>',
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:24px;color:#222}img{max-width:100%;max-height:360px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f5;padding:16px}article{border-top:1px solid #ccc;padding:24px 0}</style>',
        '<h1>公开评测错误样例</h1><p>各引擎每类按误差降序选取前三项；失败项优先。文字显示原始参考及未经润色的模型输出。表格显示原始标注 HTML 与统一结果。图片与标注仅限研究，不随应用包分发。</p>',
    ]
    selected = []
    pct = lambda value: "—" if value is None else f"{value:.2%}"
    for engine in engines:
        group = data["engines"][engine]
        if len(group["records"]) != 200 or {r["id"] for r in group["records"]} != set(samples):
            raise ValueError("Missing or duplicate evaluated samples")
        summary = group["summary"]
        table = summary["table"]
        failures = sum(not r["succeeded"] for r in group["records"])
        lines.append(f"| {engine} | {pct(summary['print']['cer'])} | {pct(summary['handwriting']['cer'])} | {pct(table.get('mean_span_f1'))} | {pct(table.get('structure_exact_rate'))} | {failures} |")
        for record in group["records"]:
            if record["succeeded"] and sha(Path(record["result"])) != record["result_sha256"]:
                raise ValueError("Result changed: " + record["id"])
        for kind in ["print", "handwriting", "table"]:
            if kind == "table" and engine == "ppocr":
                continue
            def error_key(record):
                m = record["metrics"]
                error = (1 - m["span_f1"]) if kind == "table" else m["edit_distance"] / max(1, m["reference_characters"])
                return (not record["succeeded"], error, record["id"])
            worst = sorted((r for r in group["records"] if r["kind"] == kind), key=error_key, reverse=True)[:3]
            for record in worst:
                sample = samples[record["id"]]
                result = json.loads(Path(record["result"]).read_text("utf-8")) if record["succeeded"] else {}
                hypothesis = json.dumps(result.get("tables", []), ensure_ascii=False, indent=2) if kind == "table" else result.get("text", "")
                # A portable research-only gallery; deliberately excluded by
                # collect_final_evidence.py from the application distribution.
                source = "data:image/png;base64," + base64.b64encode((a.benchmark.parent / sample["image"]).read_bytes()).decode("ascii")
                gallery.append(f'<article><h2>{engine} · {kind} · {record["id"]}</h2><img src="{esc(source, quote=True)}"><p>{esc(record.get("error", "推理完成，按标注计分"))}</p><h3>参考</h3><pre>{esc(sample["reference"])}</pre><h3>输出</h3><pre>{esc(hypothesis)}</pre><pre>{esc(json.dumps(record["metrics"], ensure_ascii=False, indent=2))}</pre></article>')
                selected.append({"engine": engine, "id": record["id"], "kind": kind, "metrics": record["metrics"], "succeeded": record["succeeded"]})
    lines += ["", "| 引擎 | 印刷数字字段（分母） | 手写数字字段（分母） | 表格数字字段（分母） |", "|---|---:|---:|---:|"]
    for engine in engines:
        s = data["engines"][engine]["summary"]
        cells = [f"{pct(s[k].get('numeric_field_exact_rate'))} ({s[k]['numeric_fields']})" for k in ["print", "handwriting", "table"]]
        lines.append("| " + engine + " | " + " | ".join(cells) + " |")
    if a.table_review:
        review = json.loads(a.table_review.read_text("utf-8"))
        slices = {}
        for row in review["samples"]:
            if samples[row["id"]]["image_sha256"] != row["image_sha256"]:
                raise ValueError("Table review image hash mismatch")
            for tag in row["tags"]:
                slices.setdefault(tag, []).append(row["id"])
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from ocr_workbench.tables import parse_tables
        for sample in samples.values():
            if sample["kind"] != "table":
                continue
            cells = [c for t in parse_tables(sample["reference"]) for c in t["cells"]]
            if any(c["row_span"] > 1 or c["column_span"] > 1 for c in cells):
                slices.setdefault("annotated_merged_cells", []).append(sample["id"])
            if any(not c["text"].strip() for c in cells):
                slices.setdefault("annotated_empty_cells", []).append(sample["id"])
        lines += ["", "表格类别切片可重叠。真实手写填表等视觉标签在推理前记录；合并与空格类别来自原始标注。样本量很小的切片只作实例核查，不用于稳定排名。", "", "| 类别 | 样本数 | PaddleOCR-VL F1 | GLM F1 | Hunyuan F1 |", "|---|---:|---:|---:|---:|"]
        slice_records = {}
        for tag, identifiers in slices.items():
            item = {"ids": identifiers, "engines": {}}
            for engine in engines[1:]:
                rows = [r for r in data["engines"][engine]["records"] if r["id"] in identifiers]
                item["engines"][engine] = {"mean_span_f1": statistics.mean(r["metrics"]["span_f1"] for r in rows), "failures": sum(not r["succeeded"] for r in rows)}
            slice_records[tag] = item
            lines.append(f"| {tag} | {len(identifiers)} | " + " | ".join(pct(item["engines"][e]["mean_span_f1"]) for e in engines[1:]) + " |")
        (a.output / "table-slices.json").write_text(json.dumps({"review_sha256": sha(a.table_review), "slices": slice_records}, ensure_ascii=False, indent=2), "utf-8")
    lines += ["", "| 引擎 | 冷加载壁钟秒（各次） | 单张中位秒 | 含加载/失败吞吐 张/分 | 采样设备峰值 MiB |", "|---|---:|---:|---:|---:|"]
    for engine in engines:
        group = data["engines"][engine]
        s = group["summary"]
        loads = ", ".join(f"{x['wall_seconds']:.2f}" for x in group["loads"])
        lines.append(f"| {engine} | {loads} | {s['median_inference_seconds']:.3f} | {s['images_per_minute_including_loads_and_failures']:.2f} | {group['sampled_peak_device_mib']} |")
    lines += ["", "冷加载指新的引擎进程，操作系统磁盘缓存未清空。设备显存含桌面及其他应用，0.5 秒采样可能漏过瞬时峰值。吞吐统计识别阶段包含加载与失败处理，不包含数据准备和评分耗时。", "", "GPU: `" + json.dumps(data["gpu"], ensure_ascii=False) + "`", "", "评测文件 SHA256: `" + sha(a.evaluation) + "`", "", "包清单 SHA256: `" + data["bundle_manifest_sha256"] + "`", "", "数据清单 SHA256: `" + data["benchmark_sha256"] + "`", ""]
    (a.output / "公开评测报告.md").write_text("\n".join(lines), "utf-8")
    (a.output / "failure-gallery.html").write_text("\n".join(gallery), "utf-8")
    (a.output / "failure-selection.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), "utf-8")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), layout="constrained")
    for kind, offset, color in [("print", -0.18, "#286b8c"), ("handwriting", 0.18, "#b87036")]:
        axes[0].bar([i+offset for i in range(4)], [100*data["engines"][e]["summary"][kind]["cer"] for e in engines], width=0.35, label=kind, color=color)
    axes[0].set_xticks(range(4), engines)
    axes[0].set_ylabel("Corpus CER (%) · lower is better")
    axes[0].legend()
    axes[1].bar(engines[1:], [data["engines"][e]["summary"]["table"]["mean_span_f1"] for e in engines[1:]], color="#286b8c")
    axes[1].set_ylabel("Mean exact-span F1 · higher is better")
    axes[1].set_ylim(0, 1)
    fig.suptitle("OmniDocBench: 200 annotated regions · research evaluation")
    fig.savefig(a.output / "accuracy.png", dpi=180)
    plt.close(fig)
    print(json.dumps({"report": str(a.output / "公开评测报告.md"), "gallery_samples": len(selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
