from __future__ import annotations

from io import BytesIO
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .core import Context, Evidence, encoded_rgb, gray, tile_map
from .geometry import transpose_pixels


def metadata(ctx: Context) -> Evidence:
    mono = gray(ctx)
    lap = cv2.Laplacian(mono, cv2.CV_32F)
    return Evidence("metadata", "file_quality", metrics={
        "format": ctx.raw_format, "width": ctx.size[0], "height": ctx.size[1],
        "jpeg_quantization_tables_present": ctx.jpeg_quantization,
        "exif_tag_count": ctx.exif_tag_count,
        "icc_profile_present": ctx.icc_profile_present,
        "sharpness_laplacian_variance": round(float(lap.var()), 3),
        "luma_std": round(float(mono.std()), 3),
    }, limitations=["Metadata and global quality metrics do not establish provenance or authenticity."])


def noise(ctx: Context) -> Evidence:
    h, w = ctx.image.shape[:2]
    if min(h, w) < 64:
        return Evidence("noise", "noise", "skipped", reason="image shorter than 64 pixels")
    lum = gray(ctx).astype(np.float32)
    residual = lum - cv2.GaussianBlur(lum, (5, 5), 0.9)
    # Robust per-tile residual scale; texture also increases this statistic.
    def mad(block: np.ndarray) -> float:
        return float(np.median(np.abs(block - np.median(block))) * 1.4826)
    raw = tile_map(residual, 32, (h, w), mad)
    lo, hi = np.percentile(raw, [10, 90])
    # Relative deviation, not a calibrated anomaly score.
    relative = np.clip(np.abs(raw - np.median(raw)) / max(float(hi - lo), 1.0), 0, 1)
    return Evidence("noise", "noise", metrics={
        "residual_mad_median": round(float(np.median(raw)), 4),
        "residual_mad_p90": round(float(hi), 4),
        "map_kind": "relative_tile_residual_deviation",
    }, limitations=[
        "Texture, edges, denoising, screenshots and repeated resizing/compression affect the residual.",
        "Map intensity is relative within this image and is not a forgery probability.",
    ], map=relative.astype(np.float32))


def frequency(ctx: Context) -> Evidence:
    """Block DCT high-frequency energy, with DC and the low-frequency triangle removed."""
    h, w = ctx.image.shape[:2]
    if min(h, w) < 64:
        return Evidence("frequency", "noise", "skipped", reason="image shorter than 64 pixels")
    lum = gray(ctx).astype(np.float32)
    block = 16
    rows = (h + block - 1) // block
    cols = (w + block - 1) // block
    padded = cv2.copyMakeBorder(lum, 0, rows * block - h, 0, cols * block - w,
                                cv2.BORDER_REFLECT_101)
    high = np.fromfunction(lambda y, x: (y + x >= 9).astype(np.float32), (block, block))
    energy = np.zeros((rows, cols), np.float32)
    for yi in range(rows):
        for xi in range(cols):
            patch = padded[yi * block:(yi + 1) * block, xi * block:(xi + 1) * block]
            coef = cv2.dct(patch)
            energy[yi, xi] = float(np.sqrt(np.mean((coef * high) ** 2)))
    lo, hi = np.percentile(energy, [10, 90])
    relative = np.clip(np.abs(energy - np.median(energy)) / max(float(hi - lo), 1), 0, 1)
    full = cv2.resize(relative, (cols * block, rows * block), interpolation=cv2.INTER_NEAREST)[:h, :w]
    return Evidence("frequency", "noise", metrics={
        "dct_high_frequency_energy_median": round(float(np.median(energy)), 4),
        "map_kind": "relative_block_dct_high_frequency_deviation",
    }, limitations=[
        "This is correlated with noise residual; do not count both as independent votes.",
        "Letters, texture, resizing and JPEG compression change high-frequency energy.",
    ], map=full.astype(np.float32))


def jpeg(ctx: Context) -> Evidence:
    if ctx.raw_format != "JPEG" or not ctx.jpeg_quantization:
        return Evidence("jpeg", "compression", "skipped", reason="source file is not JPEG with quantization tables")
    stored = encoded_rgb(ctx)
    h, w = stored.shape[:2]
    if min(h, w) < 64:
        return Evidence("jpeg", "compression", "skipped", reason="image shorter than 64 pixels")
    original = stored.astype(np.float32)
    previews = []
    medians = {}
    for quality in (65, 75, 85, 95):
        buffer = BytesIO()
        Image.fromarray(stored).save(buffer, "JPEG", quality=quality)
        buffer.seek(0)
        with Image.open(buffer) as img:
            candidate = np.asarray(img.convert("RGB")).astype(np.float32)
        diff = np.mean(np.abs(original - candidate), axis=2)
        local = tile_map(diff, 32, (h, w), lambda a: float(np.mean(a)))
        previews.append(local)
        medians[str(quality)] = round(float(np.median(local)), 3)
    # Choose the least globally disruptive recompression; preserve chosen quality.
    selected = int(np.argmin([np.median(x) for x in previews]))
    raw = previews[selected]
    lo, hi = np.percentile(raw, [10, 90])
    relative = np.clip(np.abs(raw - np.median(raw)) / max(float(hi - lo), 1.0), 0, 1)
    return Evidence("jpeg", "compression", metrics={
        "chosen_recompression_quality": (65, 75, 85, 95)[selected],
        "median_absolute_differences": medians,
        "map_kind": "relative_recompression_difference",
        "analysis_coordinate_system": "stored_pixels",
    }, limitations=[
        "JPEG Ghost-style recompression difference does not prove double compression or tampering.",
        "Texture, color and the last forwarding step may dominate this map.",
    ], map=transpose_pixels(relative.astype(np.float32), ctx.exif_orientation))


def jpeg_grid(ctx: Context) -> Evidence:
    """Observed discontinuity at the final JPEG 8x8 block boundaries."""
    if ctx.raw_format != "JPEG" or not ctx.jpeg_quantization:
        return Evidence("jpeg_grid", "compression", "skipped", reason="source file is not JPEG")
    stored = encoded_rgb(ctx)
    h, w = stored.shape[:2]
    if min(h, w) < 64:
        return Evidence("jpeg_grid", "compression", "skipped", reason="image shorter than 64 pixels")
    lum = cv2.cvtColor(stored, cv2.COLOR_RGB2GRAY).astype(np.float32)
    dx = np.abs(np.diff(lum, axis=1))
    dy = np.abs(np.diff(lum, axis=0))
    # JPEG block boundary at pixel 7|8, 15|16, etc. Compare with adjacent edges.
    boundary_x = np.zeros((h, w), np.float32)
    boundary_y = np.zeros((h, w), np.float32)
    boundary_x[:, 8:w:8] = dx[:, 7:w-1:8]
    boundary_y[8:h:8, :] = dy[7:h-1:8, :]
    local = cv2.blur(boundary_x + boundary_y, (32, 32))
    background = cv2.blur(np.pad(dx, ((0, 0), (1, 0))) +
                          np.pad(dy, ((1, 0), (0, 0))), (32, 32))
    ratio = local / np.maximum(background / 4, 1.0)
    lo, hi = np.percentile(ratio, [10, 90])
    relative = np.clip(np.abs(ratio - np.median(ratio)) / max(float(hi - lo), .1), 0, 1)
    return Evidence("jpeg_grid", "compression", metrics={
        "median_boundary_ratio": round(float(np.median(ratio)), 4),
        "map_kind": "relative_final_jpeg_grid_discontinuity",
        "analysis_coordinate_system": "stored_pixels",
    }, limitations=[
        "This measures the final file grid, not the full forwarding history.",
        "Sharp text, grids, cropping and rescaling may produce misleading boundaries.",
    ], map=transpose_pixels(relative.astype(np.float32), ctx.exif_orientation))


def copy_move(ctx: Context) -> Evidence:
    h, w = ctx.image.shape[:2]
    if min(h, w) < 96:
        return Evidence("copy_move", "content", "skipped", reason="image shorter than 96 pixels")
    detector = cv2.ORB_create(nfeatures=1600, fastThreshold=12)
    keypoints, descriptors = detector.detectAndCompute(gray(ctx), None)
    if descriptors is None or len(keypoints) < 8:
        return Evidence("copy_move", "content", metrics={"keypoints": len(keypoints), "candidate_pairs": 0})
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors, descriptors, k=min(12, len(descriptors)))
    pairs = []
    min_distance = max(24.0, 0.08 * min(h, w))
    for candidates in matches:
        source = keypoints[candidates[0].queryIdx].pt
        distant = [m for m in candidates if m.queryIdx != m.trainIdx and
                   np.linalg.norm(np.subtract(source, keypoints[m.trainIdx].pt)) >= min_distance]
        if len(distant) < 2:
            continue
        if distant[0].distance >= 0.78 * distant[1].distance or distant[0].distance > 48:
            continue
        dest = keypoints[distant[0].trainIdx].pt
        pairs.append((source, dest))
    # Cluster by displacement, retaining at most one pair per unordered feature pair.
    unique = {}
    for a, b in pairs:
        key = tuple(sorted(((round(a[0]), round(a[1])), (round(b[0]), round(b[1])))))
        unique[key] = (a, b)
    groups: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for a, b in unique.values():
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy = -dx, -dy
        bucket = (round(dx / 16), round(dy / 16))
        groups.setdefault(bucket, []).append((a, b))
    dominant = max(groups.values(), key=len, default=[])
    endpoints = [[round(a[0]), round(a[1]), round(b[0]), round(b[1])]
                 for a, b in dominant]
    result = np.zeros((h, w), np.float32)
    if len(dominant) >= 3:
        for a, b in dominant:
            for x, y in (a, b):
                cv2.circle(result, (round(x), round(y)), 12, 1, -1)
        result = cv2.GaussianBlur(result, (0, 0), 5)
        result /= max(float(result.max()), 1e-9)
    return Evidence("copy_move", "content", metrics={
        "keypoints": len(keypoints), "candidate_pairs": len(unique),
        "largest_consistent_displacement_group": len(dominant),
        "dominant_match_pairs": endpoints,
        "map_kind": "matched_keypoint_neighborhoods",
    }, limitations=[
        "Repeated letters, logos, grids and natural repeated textures create matches.",
        "Weak matches or no matches do not exclude copy-move; this is a candidate search, not segmentation.",
    ], map=result if len(dominant) >= 3 else None)


def ocr(ctx: Context) -> Evidence:
    try:
        from rapidocr import RapidOCR
    except ImportError:
        return Evidence("ocr", "text", "skipped", reason="RapidOCR is not installed; install optional ocr dependencies")
    engine = RapidOCR()
    result = engine(ctx.image)
    texts = [{"text": str(text), "ocr_score": float(score),
              "polygon": np.asarray(poly).astype(int).tolist()}
             for poly, text, score in zip(result.boxes if result.boxes is not None else [],
                                           result.txts if result.txts is not None else [],
                                           result.scores if result.scores is not None else [])]
    return Evidence("ocr", "text", metrics={"lines": texts},
                    limitations=["OCR content and confidence are not evidence of alteration."])


REGISTRY: dict[str, Callable[[Context], Evidence]] = {
    "metadata": metadata, "noise": noise, "frequency": frequency,
    "jpeg": jpeg, "jpeg_grid": jpeg_grid, "copy_move": copy_move, "ocr": ocr,
}
