"""Retain a paired, CPU-only diagnostic comparison of the two classic toolboxes.

Every response is a relative statistic, never a calibrated tampering probability.
No image-level authenticity decision is derived from these diagnostic maps.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import traceback

import cv2
import numpy as np
from PIL import Image, ImageOps, __version__ as pillow_version

from scripts.forensics_stability.metrics import pixel_metrics

TOOLS = ["metadata", "noise", "frequency", "jpeg", "jpeg_grid", "copy_move"]
RUN = Path("F:/OCR/output/forensics-stability-20261014")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def f_path(path):
    path = Path(path).absolute()
    resolved = path.resolve()
    if path.drive.upper() != "F:" or resolved.drive.upper() != "F:":
        raise ValueError(f"Path must stay on local F: {path}")
    for parent in (path, *path.parents):
        if parent.exists() and getattr(parent.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Reparse-point path is not admitted: {parent}")
    return resolved


def json_write(path, payload):
    path = f_path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_identity(root):
    folder = root / "modules/image_forensics/forensic_toolbox"
    files = [{"name": path.name, "sha256": sha(path)} for path in sorted(folder.glob("*.py"))]
    if not files:
        raise ValueError(f"Toolbox source is missing: {folder}")
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"root": str(root), "files": files, "identity_sha256": identity,
            "worker_sha256": sha(root / "modules/image_forensics/worker.py")}


def load_toolbox(root, label):
    folder = root / "modules/image_forensics/forensic_toolbox"
    name = f"classical_comparison_{label}"
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import {folder}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(name + ".core"), importlib.import_module(name + ".algorithms")


def display_pixels(array, orientation):
    image = Image.fromarray(array)
    image.getexif()[274] = orientation
    return np.asarray(ImageOps.exif_transpose(image)).copy()


def dataset_file(root, relative):
    path = f_path(root / relative)
    if not path.is_relative_to(root):
        raise ValueError("Dataset file leaves the manifest directory")
    return path


def validate_sample(root, sample):
    sample_id = sample["sample_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sample_id):
        raise ValueError("Invalid sample identifier")
    source = dataset_file(root, sample["input"])
    if sha(source) != sample["input_sha256"]:
        raise ValueError("Input SHA-256 differs from the manifest")
    with Image.open(source) as image:
        if list(image.size) != sample["stored_size"]:
            raise ValueError("Stored dimensions differ from the manifest")
        orientation = image.getexif().get(274, 1)
        if orientation != sample["exif_orientation"]:
            raise ValueError("EXIF orientation differs from the manifest")
        stored = np.asarray(image.convert("RGB")).copy()
        display = np.asarray(ImageOps.exif_transpose(image).convert("RGB")).copy()
    truth = None
    mask = None
    if sample.get("mask"):
        mask = dataset_file(root, sample["mask"])
        if sha(mask) != sample["mask_sha256"]:
            raise ValueError("Mask SHA-256 differs from the manifest")
        if sample.get("mask_coordinate_system") != "stored_pixels":
            raise ValueError("Ground-truth mask must use stored pixels")
        with Image.open(mask) as image:
            if list(image.size) != sample["stored_size"]:
                raise ValueError("Ground-truth dimensions differ from the stored input")
            truth = display_pixels(np.asarray(image.convert("L")), orientation) > 0
    return source, mask, stored, display, truth, orientation


def evaluate_version(core, algorithms, source, mask, stored, display, truth, orientation, sample, output):
    output = f_path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to replace previous evidence: {output}")
    started = time.perf_counter()
    record = {"status": "running", "tools": []}
    try:
        report = core.run(source, output, tools=TOOLS)
        record["run_seconds"] = round(time.perf_counter() - started, 6)
        preview_path = output / report["input"]["preview_path"]
        with Image.open(preview_path) as preview:
            preview_pixels = np.asarray(preview.convert("RGB"))
        record["preview_matches_exif_display"] = bool(np.array_equal(preview_pixels, display))
        record["reported_display_size_correct"] = [report["input"]["width"], report["input"]["height"]] == [display.shape[1], display.shape[0]]
        record["reported_input_sha256_correct"] = report["input"]["sha256"] == sample["input_sha256"]
        raw_context = replace(core.load_image(source), image=stored, exif_orientation=1)
        for item in report["evidence"]:
            tool = {"tool": item["tool"], "status": item["status"], "version": item["version"],
                    "elapsed_ms": item["elapsed_ms"], "reason": item["reason"], "map_path": item["map_path"]}
            try:
                if item["map_path"]:
                    with Image.open(output / item["map_path"]) as image:
                        response_bytes = np.asarray(image.convert("L")).copy()
                        tool["map_size"] = list(image.size)
                    tool["map_dimensions_match_preview"] = response_bytes.shape == display.shape[:2]
                    if not tool["map_dimensions_match_preview"]:
                        raise ValueError("Response dimensions differ from normalized preview")
                    if truth is not None:
                        tool["pixel"] = pixel_metrics(response_bytes.astype(np.float32) / 255, truth)
                    if item["tool"] in {"jpeg", "jpeg_grid"}:
                        # This independent stored-coordinate reference checks only registration.
                        # It does not turn a classic statistic into manipulation evidence.
                        reference = algorithms.REGISTRY[item["tool"]](raw_context)
                        if reference.status != "completed" or reference.map is None:
                            raise ValueError("Stored-coordinate JPEG reference did not complete")
                        expected = display_pixels(np.round(np.clip(reference.map, 0, 1) * 255).astype(np.uint8), orientation)
                        reference_name = f"registration-reference-{item['tool']}.png"
                        Image.fromarray(expected).save(output / reference_name)
                        difference = np.abs(response_bytes.astype(np.int16) - expected.astype(np.int16))
                        tool["stored_coordinate_registration"] = {
                            "reference_path": reference_name,
                            "exact_match": bool(np.array_equal(response_bytes, expected)),
                            "mean_absolute_gray_difference": float(difference.mean()),
                            "max_absolute_gray_difference": int(difference.max()),
                        }
            except Exception as exc:
                tool["evaluation_error"] = f"{type(exc).__name__}: {exc}"
                tool["evaluation_traceback"] = traceback.format_exc()
            record["tools"].append(tool)
        record["status"] = "evaluated"
    except Exception as exc:
        record.update(status="error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc(),
                      run_seconds=round(time.perf_counter() - started, 6))
    record["original_input_unchanged"] = sha(source) == sample["input_sha256"]
    record["original_mask_unchanged"] = mask is None or sha(mask) == sample["mask_sha256"]
    record["files"] = [{"name": str(path.relative_to(output)).replace("\\", "/"),
                        "sha256": sha(path), "size_bytes": path.stat().st_size}
                       for path in sorted(output.rglob("*")) if path.is_file()] if output.exists() else []
    return record


def mean(values):
    return float(np.mean(values)) if values else None


def summarize(records):
    summary = {}
    for version in ("baseline", "current"):
        runs = [row[version] for row in records if version in row]
        by_tool = {}
        for name in TOOLS:
            entries = [(row, tool) for row in records for tool in row.get(version, {}).get("tools", []) if tool["tool"] == name]
            modified = [(row, tool) for row, tool in entries if row["is_modified"] and "pixel" in tool]
            normal = [tool for row, tool in entries if not row["is_modified"] and "pixel" in tool]
            elapsed = [tool["elapsed_ms"] for _, tool in entries if tool.get("elapsed_ms") is not None]
            registrations = [tool["stored_coordinate_registration"] for _, tool in entries if "stored_coordinate_registration" in tool]
            by_tool[name] = {
                "statuses": dict(Counter(tool["status"] for _, tool in entries)),
                "evaluation_errors": sum("evaluation_error" in tool for _, tool in entries),
                "maps": sum(bool(tool.get("map_path")) for _, tool in entries),
                "map_dimension_failures": sum(tool.get("map_dimensions_match_preview") is False for _, tool in entries),
                "stored_coordinate_registration_checks": len(registrations),
                "stored_coordinate_registration_failures": sum(not item["exact_match"] for item in registrations),
                "mean_elapsed_ms": mean(elapsed),
                "p95_elapsed_ms": float(np.percentile(elapsed, 95)) if elapsed else None,
                "modified_mask_images": len(modified),
                "diagnostic_pixel": {key: mean([tool["pixel"][key] for _, tool in modified if tool["pixel"][key] is not None]) for key in ("f1", "iou", "auprc")},
                "normal_response_area_fraction_at_0_5": mean([tool["pixel"]["normal_predicted_area_fraction"] for tool in normal]),
                "by_split": {split: {key: mean([tool["pixel"][key] for row, tool in modified if row["split"] == split and tool["pixel"][key] is not None]) for key in ("f1", "iou", "auprc")} for split in ("development", "calibration", "final_test")},
            }
        summary[version] = {
            "runs": len(runs), "run_errors": sum(item["status"] == "error" for item in runs),
            "preview_failures": sum(item.get("preview_matches_exif_display") is False for item in runs),
            "reported_size_failures": sum(item.get("reported_display_size_correct") is False for item in runs),
            "reported_input_hash_failures": sum(item.get("reported_input_sha256_correct") is False for item in runs),
            "input_integrity_failures": sum(item.get("original_input_unchanged") is False for item in runs),
            "mask_integrity_failures": sum(item.get("original_mask_unchanged") is False for item in runs),
            "mean_run_seconds": mean([item["run_seconds"] for item in runs]), "tools": by_tool,
        }
    return summary


def markdown(report):
    lines = ["# 经典工具几何稳定性对照", "", f"已完成 {len(report['records'])}/{report['expected_samples']} 个合成输入；每个输入分别真实运行两版六工具。", "",
             "经典热图是相对统计响应，不是篡改概率。下表像素指标仅用于相同合成样本上的诊断对照；0.5 是固定响应阈值，未校准为业务判断，不输出整图真假。", "",
             "| 工具 | 基线/当前 JPEG 存储坐标配准失败 | 基线/当前改动样本平均 AUPRC | 基线/当前图尺寸失败 |", "|---|---:|---:|---:|"]
    def fmt(value):
        return "—" if value is None else f"{value:.4f}"
    for tool in TOOLS:
        old = report["summary"]["baseline"]["tools"][tool]
        new = report["summary"]["current"]["tools"][tool]
        lines.append(f"| {tool} | {old['stored_coordinate_registration_failures']}/{new['stored_coordinate_registration_failures']} | {fmt(old['diagnostic_pixel']['auprc'])}/{fmt(new['diagnostic_pixel']['auprc'])} | {old['map_dimension_failures']}/{new['map_dimension_failures']} |")
    lines += ["", "配准检查：对同一解码 JPEG 在存储坐标计算原公式，并用 Pillow EXIF 精确像素置换生成参考；比较保存后的 8 位热图，无插值。参考 PNG 与每个报告、实际热图和 SHA-256 一并保留。配准通过不证明统计响应能够识别篡改。", "",
              f"样本校验失败：{sum('validation_error' in row for row in report['records'])}。基线运行错误：{report['summary']['baseline']['run_errors']}；当前运行错误：{report['summary']['current']['run_errors']}。", "",
              "输入、真值和两个源码身份见 report.json；各 sample 记录列出全部文件的字节哈希。历史报告与冻结基线没有改写。完整逐样本结果保留在 records 目录。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=RUN / "evaluation/dataset-001/manifest.json")
    parser.add_argument("--baseline", type=Path, default=RUN / "baseline/platform")
    parser.add_argument("--current", type=Path, default=RUN / "worktrees/platform")
    parser.add_argument("--output", type=Path, default=RUN / "evidence/classical-comparison-001")
    args = parser.parse_args()
    manifest_path, baseline, current, output = (f_path(path) for path in (args.manifest, args.baseline, args.current, args.output))
    if shutil.disk_usage(output.parent).free < 2 * 1024 ** 3:
        raise RuntimeError("F: requires at least 2 GiB free before evaluation")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Output already contains evidence; choose a new F: output directory")
    output.mkdir(parents=True, exist_ok=True)
    (output / "records").mkdir()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = manifest["samples"]
    if len({sample["sample_id"] for sample in samples}) != len(samples):
        raise ValueError("Duplicate sample identifiers")
    old_core, old_algorithms = load_toolbox(baseline, "baseline")
    new_core, new_algorithms = load_toolbox(current, "current")
    report = {
        "format": "classical-forensics-stability-comparison-v1", "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(), "expected_samples": len(samples),
        "dataset_manifest": str(manifest_path), "dataset_manifest_sha256": sha(manifest_path),
        "source_identities": {"baseline": source_identity(baseline), "current": source_identity(current)},
        "evaluator_sha256": sha(Path(__file__)), "metrics_sha256": sha(Path(__file__).with_name("metrics.py")),
        "runtime": {"python": sys.version, "executable": sys.executable, "numpy": np.__version__, "cv2": cv2.__version__, "pillow": pillow_version},
        "pixel_response_threshold": 0.5,
        "notice": "Relative classic diagnostic responses, not calibrated tampering probabilities; no image-level verdict. Synthetic controls do not qualify business accuracy.",
        "records": [],
    }
    log_path = output / "progress.log"
    for index, sample in enumerate(samples, 1):
        row = {key: sample[key] for key in ("sample_id", "base_id", "family", "split", "variant", "is_modified", "input_sha256", "mask_sha256") if key in sample}
        try:
            source, mask, stored, display, truth, orientation = validate_sample(manifest_path.parent, sample)
            row.update(stored_size=list(stored.shape[1::-1]), display_size=list(display.shape[1::-1]), exif_orientation=orientation)
            for label, core, algorithms in (("baseline", old_core, old_algorithms), ("current", new_core, new_algorithms)):
                row[label] = evaluate_version(core, algorithms, source, mask, stored, display, truth, orientation, sample, output / label / sample["sample_id"])
        except Exception as exc:
            row.update(validation_error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        report["records"].append(row)
        json_write(output / "records" / f"{sample['sample_id']}.json", row)
        if index % 40 == 0 or index == len(samples):
            report["summary"] = summarize(report["records"])
            json_write(output / "report.json", report)
            free = shutil.disk_usage(output).free
            message = f"{datetime.now(timezone.utc).isoformat()} {index}/{len(samples)} samples; baseline/current errors {report['summary']['baseline']['run_errors']}/{report['summary']['current']['run_errors']}; F free {free / 1024 ** 3:.2f} GiB"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(message + "\n")
            print(message, flush=True)
            if free < 2 * 1024 ** 3:
                report["status"] = "stopped_low_disk_space"
                json_write(output / "report.json", report)
                raise RuntimeError("Stopped before F: free space falls below 2 GiB; evidence retained")
    report["status"] = "completed_with_failures" if any("validation_error" in row or any(row.get(label, {}).get("status") == "error" or any("evaluation_error" in tool or tool.get("status") == "error" for tool in row.get(label, {}).get("tools", [])) for label in ("baseline", "current")) for row in report["records"]) else "completed"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = summarize(report["records"])
    report["source_identities_unchanged"] = {label: source_identity(path) == report["source_identities"][label] for label, path in (("baseline", baseline), ("current", current))}
    json_write(output / "report.json", report)
    (output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "samples": len(samples)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "completed" and all(report["source_identities_unchanged"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
