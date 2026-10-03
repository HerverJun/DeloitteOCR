from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import __version__


@dataclass
class Context:
    path: Path
    image: np.ndarray  # RGB, EXIF-transposed; the coordinate space for all tools
    raw_format: str | None
    original_size: tuple[int, int]
    sha256: str
    jpeg_quantization: bool
    exif_orientation: int | None
    exif_tag_count: int
    icc_profile_present: bool

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]


@dataclass
class Evidence:
    tool: str
    family: str
    status: str = "completed"
    metrics: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    map: np.ndarray | None = None
    regions: list[list[int]] = field(default_factory=list)
    reason: str | None = None
    version: str = __version__
    elapsed_ms: float | None = None

    def record(self, map_path: str | None = None) -> dict[str, Any]:
        return {
            "tool": self.tool, "family": self.family, "status": self.status,
            "metrics": self.metrics, "limitations": self.limitations,
            "regions": self.regions, "reason": self.reason,
            "version": self.version, "map_path": map_path,
            "elapsed_ms": self.elapsed_ms,
        }


def load_image(path: str | Path) -> Context:
    path = Path(path).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path) as source:
        raw_format = source.format
        quant = bool(getattr(source, "quantization", None))
        original_size = source.size
        exif = source.getexif()
        orientation = exif.get(274)
        exif_count = len(exif)
        icc_present = bool(source.info.get("icc_profile"))
        image = np.asarray(ImageOps.exif_transpose(source).convert("RGB")).copy()
    if image.shape[0] < 1 or image.shape[1] < 1:
        raise ValueError("empty image")
    return Context(path, image, raw_format, original_size, digest, quant,
                   orientation, exif_count, icc_present)


def gray(context: Context) -> np.ndarray:
    return cv2.cvtColor(context.image, cv2.COLOR_RGB2GRAY)


def tile_map(values: np.ndarray, block: int, shape: tuple[int, int], func: Callable[[np.ndarray], float]) -> np.ndarray:
    h, w = values.shape
    cells = np.zeros(((h + block - 1) // block, (w + block - 1) // block), np.float32)
    for yi, y in enumerate(range(0, h, block)):
        for xi, x in enumerate(range(0, w, block)):
            cells[yi, xi] = func(values[y:min(y + block, h), x:min(x + block, w)])
    return cv2.resize(cells, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)


def save_map(data: np.ndarray, target: Path) -> None:
    if data.ndim != 2 or not np.isfinite(data).all():
        raise ValueError("map must be a finite, single-channel array")
    clipped = np.clip(data, 0.0, 1.0)
    Image.fromarray(np.round(clipped * 255).astype(np.uint8), mode="L").save(target)
    if not target.is_file():
        raise OSError(f"failed to write map: {target}")


def run(path: str | Path, output: str | Path, tools: list[str] | None = None,
        model_config: str | Path | None = None) -> dict[str, Any]:
    from .algorithms import REGISTRY
    from .model import run_external_model

    context = load_image(path)
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(context.image).save(out / "preview-exif-normalized.png")
    chosen = tools if tools is not None else ["metadata", "noise", "frequency", "jpeg", "jpeg_grid", "copy_move"]
    if unknown := set(chosen) - set(REGISTRY):
        raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")
    evidence: list[Evidence] = []
    for name in chosen:
        start = time.perf_counter()
        try:
            item = REGISTRY[name](context)
        except Exception as exc:
            item = Evidence(name, "unknown", "error", reason=f"{type(exc).__name__}: {exc}")
        item.elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        evidence.append(item)
    if model_config is not None:
        start = time.perf_counter()
        item = run_external_model(context, Path(model_config), out / "external-model")
        item.elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        evidence.append(item)

    # OCR supplies context for self-matches, not independent alteration proof.
    copied = next((x for x in evidence if x.tool == "copy_move" and x.status == "completed"), None)
    text = next((x for x in evidence if x.tool == "ocr" and x.status == "completed"), None)
    if copied is not None and text is not None:
        pairs = copied.metrics.get("dominant_match_pairs", [])
        lines = text.metrics.get("lines", [])
        if pairs and lines:
            bounds = []
            for line in lines:
                points = np.asarray(line.get("polygon", []))
                if points.shape == (4, 2):
                    bounds.append((points[:, 0].min(), points[:, 1].min(),
                                   points[:, 0].max(), points[:, 1].max()))
            same_line = sum(any(x1 <= ax <= x2 and y1 <= ay <= y2 and
                                x1 <= bx <= x2 and y1 <= by <= y2
                                for x1, y1, x2, y2 in bounds)
                            for ax, ay, bx, by in pairs)
            fraction = same_line / len(pairs)
            copied.metrics["same_ocr_line_pair_fraction"] = round(fraction, 3)
            if fraction >= .8:
                copied.limitations.append(
                    "Most matched points are inside the same OCR text line; repeated glyphs are a plausible benign cause.")

    records: list[dict[str, Any]] = []
    for index, item in enumerate(evidence):
        map_path = None
        if item.map is not None and item.status == "completed":
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", item.tool)[:48]
            target = out / f"{index:02d}-{safe_name}.png"
            try:
                save_map(item.map, target)
                map_path = target.name
            except Exception as exc:
                item.status = "error"
                item.reason = f"invalid map: {exc}"
        records.append(item.record(map_path))
    report = {
        "schema_version": "0.1", "input": {
            "path": str(context.path), "sha256": context.sha256,
            "source_format": context.raw_format,
            "source_exif_orientation": context.exif_orientation,
            "source_exif_tag_count": context.exif_tag_count,
            "source_icc_profile_present": context.icc_profile_present,
            "original_width": context.original_size[0], "original_height": context.original_size[1],
            "width": context.size[0], "height": context.size[1],
            "preview_path": "preview-exif-normalized.png",
            "coordinate_system": "EXIF-transposed full RGB image, origin at upper left",
        },
        "verdict": None, "evidence": records,
        "notice": "Diagnostic evidence only. Maps are not probabilities; do not sum their scores.",
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    from .review import render
    render(report, out / "review.html")
    return report
