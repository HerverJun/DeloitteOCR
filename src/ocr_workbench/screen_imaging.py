"""Bounded, evidence based preparation of photographed spreadsheet screens.

All coordinates refer to the EXIF-normalized original pixel space.  Detection
uses pixels and optional *inference* evidence; reference tables are never inputs.
The original remains a candidate when region detection is uncertain.  A crop or
tile is not a new independent table and must not be blindly concatenated.
"""

from pathlib import Path
import hashlib
import json
import math

import numpy as np
from PIL import Image

from ocr_workbench.image_utils import normalized_rgb
from ocr_workbench.coordinates import IDENTITY, bounds, box_polygon, inverse, multiply, polygon

try:
    import cv2
except ImportError:  # numpy/Pillow-only service deployments remain usable.
    cv2 = None

POLICY_VERSION = "screen-cpu-v1"
UI_KINDS = {"toolbar", "taskbar", "formula_bar", "ruler", "window_border", "scrollbar", "ui"}
TABLE_KINDS = {"table", "spreadsheet", "table_region"}
ANCILLARY_KINDS = {"title", "caption", "unit", "note", "footnote", "text"}


def _box(item, width, height):
    value = item.get("box", item.get("bbox", item.get("coordinate")))
    if value is None and item.get("polygon"):
        value = bounds(item["polygon"])
    try:
        if len(value) != 4 or not all(math.isfinite(float(v)) for v in value):
            return None
        x0, y0, x1, y1 = map(float, value)
    except (TypeError, ValueError):
        return None
    value = [max(0., x0), max(0., y0), min(float(width), x1), min(float(height), y1)]
    return value if value[2] > value[0] and value[3] > value[1] else None


def _area(box):
    return max(0., box[2] - box[0]) * max(0., box[3] - box[1])


def _overlap(a, b):
    return max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))


def _union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _evidence_items(evidence):
    evidence = evidence or {}
    layout = evidence.get("layout", evidence.get("regions", []))
    if isinstance(layout, dict):
        layout = layout.get("regions", layout.get("blocks", []))
    text = evidence.get("text", evidence.get("blocks", evidence.get("text_boxes", [])))
    if isinstance(text, dict):
        text = text.get("blocks", text.get("text_boxes", []))
    return [v for v in layout if isinstance(v, dict)], [v for v in text if isinstance(v, dict)]


def _line_regions(image):
    """Find connected grids, requiring multiple horizontal AND vertical rules.

    A single window border is insufficient. Detection at a bounded resolution
    does not downsample the saved OCR candidate.
    """
    if cv2 is None:
        return []
    scale = min(1., 1800 / max(image.size))
    small = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    grey = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2GRAY)
    ink = cv2.adaptiveThreshold(grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(25, grey.shape[1] // 30)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(22, grey.shape[0] // 30), 1), np.uint8))
    lines = cv2.bitwise_or(horizontal, vertical)
    joined = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    # A window border encloses multiple disconnected tables. RETR_EXTERNAL
    # would discard both inner tables and turn that window into a false ROI.
    contours, _ = cv2.findContours(joined, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    result = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 65 or h < 35 or w * h < grey.size * .003:
            continue
        hp = np.mean(horizontal[y:y+h, x:x+w] > 0, axis=1)
        vp = np.mean(vertical[y:y+h, x:x+w] > 0, axis=0)
        hpositions = _runs(np.where(hp > .65)[0])
        vpositions = _runs(np.where(vp > .65)[0])
        if len(hpositions) < 3 or len(vpositions) < 3:
            continue
        result.append({"box": [x/scale, y/scale, (x+w)/scale, (y+h)/scale],
                       "sources": ["pixel_grid"], "score": .78,
                       "observed_grid_rows": len(hpositions)-1,
                       "observed_grid_columns": len(vpositions)-1,
                       "grid_reliable": bool(np.min(hp[np.rint(hpositions).astype(int)]) > .65
                                             and np.min(vp[np.rint(vpositions).astype(int)]) > .65)})
    # Window outlines can have several long interior separator lines. When a
    # larger outline encloses separate complete grids, retain those grids.
    retained = []
    for region in result:
        children = [r for r in result if r is not region and _area(r["box"]) < _area(region["box"])*.7
                    and _overlap(r["box"],region["box"]) > _area(r["box"])*.97]
        separate = any(_overlap(a["box"],b["box"])/min(_area(a["box"]),_area(b["box"])) < .05
                       for n,a in enumerate(children) for b in children[n+1:])
        if not separate:
            retained.append(region)
    return retained


def _runs(indices):
    if not len(indices):
        return []
    groups = np.split(indices, np.where(np.diff(indices) > 1)[0] + 1)
    return [float(np.mean(group)) for group in groups]


def _text_regions(items, width, height):
    """Borderless table candidates need repeated spatial column alignments."""
    items = [(item, _box(item, width, height)) for item in items]
    items = [(item, box) for item, box in items if box and item.get("kind") not in UI_KINDS]
    if len(items) < 6:
        return []
    median_height = float(np.median([b[3] - b[1] for _, b in items]))
    tolerance = max(4., median_height * .65)
    rows = []
    for item, box in sorted(items, key=lambda pair: (pair[1][1], pair[1][0])):
        center = (box[1] + box[3]) / 2
        row = next((r for r in reversed(rows[-4:]) if abs(r["y"] - center) < tolerance), None)
        if row is None:
            row = {"y": center, "items": []}
            rows.append(row)
        row["items"].append((item, box))
    clusters, current = [], []
    for row in rows:
        if len(row["items"]) < 2:
            if current:
                clusters.append(current)
                current = []
            continue
        if current and row["y"] - current[-1]["y"] > max(median_height * 4, height * .05):
            clusters.append(current)
            current = []
        current.append(row)
    if current:
        clusters.append(current)
    result = []
    for cluster in clusters:
        if len(cluster) < 3:
            continue
        anchors = []
        for row in cluster:
            for _, box in row["items"]:
                anchor = next((a for a in anchors if abs(a["x"] - box[0]) < max(8, median_height)), None)
                if anchor is None:
                    anchor = {"x": box[0], "rows": set()}
                    anchors.append(anchor)
                anchor["rows"].add(id(row))
        if sum(len(a["rows"]) >= min(3, len(cluster)) for a in anchors) < 2:
            continue
        boxes = [b for row in cluster for _, b in row["items"]]
        box = boxes[0]
        for next_box in boxes[1:]:
            box = _union(box, next_box)
        result.append({"box": box, "sources": ["aligned_text"], "score": .7,
                       "observed_text_rows": len(cluster)})
    return result


def _pixel_ancillary(image, table_box):
    """Preserve nearby title/note pixels even before a text OCR is available.

    These anonymous text runs are only crop support, not recognized content or
    proof of semantic identity. Large bars/rules and isolated icons are omitted.
    """
    if cv2 is None:
        return []
    tw, th = table_box[2]-table_box[0],table_box[3]-table_box[1]
    reach = max(45,min(110,th*.18))
    x0,x1 = max(0,int(table_box[0]-tw*.04)),min(image.width,math.ceil(table_box[2]+tw*.04))
    y0,y1 = max(0,int(table_box[1]-reach)),min(image.height,math.ceil(table_box[3]+reach))
    grey = np.asarray(image.convert("L").crop((x0,y0,x1,y1)))
    ink = cv2.adaptiveThreshold(grey,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,31,10)
    inside_y0,inside_y1 = max(0,int(table_box[1]-y0-3)),min(grey.shape[0],math.ceil(table_box[3]-y0+3))
    ink[inside_y0:inside_y1,:] = 0
    count, _,stats,_ = cv2.connectedComponentsWithStats(ink,8)
    chars = []
    for index in range(1,count):
        x,y,w,h,area = stats[index]
        if 3 <= h <= min(70,reach*.9) and 2 <= w <= 140 and 5 <= area <= w*h*.9:
            chars.append([x+x0,y+y0,x+x0+w,y+y0+h])
    rows = []
    for box in sorted(chars,key=lambda b:((b[1]+b[3])/2,b[0])):
        center = (box[1]+box[3])/2
        row = next((r for r in reversed(rows[-4:]) if abs(center-r["y"]) <= max(4,(box[3]-box[1])*.55)),None)
        if row is None:
            row = {"y":center,"boxes":[]}
            rows.append(row)
        row["boxes"].append(box)
    result = []
    for row in rows:
        if len(row["boxes"]) < 3:
            continue
        box = row["boxes"][0]
        for next_box in row["boxes"][1:]:
            box = _union(box,next_box)
        if box[2]-box[0] >= min(55,tw*.15):
            result.append({"kind":"text","box":box,"source":"near_table_pixel_text","semantic_role":"unclassified"})
    return result


def detect_business_regions(image, evidence=None, *, max_regions=8):
    width, height = image.size
    layout, text = _evidence_items(evidence)
    ui = [{"box": b, "kind": item.get("kind", item.get("label", "ui")), "source": "layout"}
          for item in layout + text
          if item.get("kind", item.get("label")) in UI_KINDS and (b := _box(item, width, height))]
    candidates = _line_regions(image) + _text_regions(text, width, height)
    for item in layout:
        kind = item.get("kind", item.get("label"))
        score = item.get("confidence", item.get("score", .8))
        box = _box(item, width, height)
        if kind in TABLE_KINDS and box and isinstance(score, (int, float)) and score >= .55:
            candidates.append({"box": box, "sources": ["layout_table"], "score": float(score)})
    # UI exclusion is spatial and explicit. Do not erase business strings by keywords.
    candidates = [c for c in candidates if not any(_overlap(c["box"], u["box"]) / _area(c["box"]) > .65 for u in ui)]
    merged = []
    for candidate in sorted(candidates, key=lambda c: (-c["score"], -_area(c["box"]))):
        match = next((r for r in merged if _overlap(r["box"], candidate["box"]) / min(_area(r["box"]), _area(candidate["box"])) > .75), None)
        if match:
            match["box"] = _union(match["box"], candidate["box"])
            match["sources"] = sorted(set(match["sources"] + candidate["sources"]))
            match["score"] = min(.99, max(match["score"], candidate["score"]) + .05)
            for key in ("observed_grid_rows", "observed_grid_columns", "grid_reliable", "observed_text_rows"):
                if key in candidate:
                    match[key] = candidate[key]
        else:
            merged.append(dict(candidate))
    warnings = []
    if len(merged) > max_regions:
        warnings.append({"code": "region_limit", "message": "检测表区超过上限，保留原图以免静默漏表。"})
    # Retain titles/units/notes close to each table. Explicit UI boxes remain out.
    regions = []
    for index, region in enumerate(sorted(merged, key=lambda r: (r["box"][1], r["box"][0]))[:max_regions]):
        table_box = region["box"][:]
        expanded = table_box[:]
        tw, th = table_box[2]-table_box[0], table_box[3]-table_box[1]
        nearby = []
        for item in layout + text + _pixel_ancillary(image,table_box):
            kind = item.get("kind", item.get("label", "text"))
            box = _box(item, width, height)
            if not box or kind not in ANCILLARY_KINDS or any(_overlap(box, u["box"])/_area(box) > .5 for u in ui):
                continue
            horizontal = max(0., min(box[2], table_box[2])-max(box[0], table_box[0])) / min(tw, box[2]-box[0])
            gap = max(table_box[1]-box[3], box[1]-table_box[3], 0.)
            if horizontal > .45 and gap <= max(30, min(height * .08, th * .22)):
                expanded = _union(expanded, box)
                nearby.append({"kind": kind, "box": box, "text": item.get("text", ""),
                               "source":item.get("source","layout_or_text_evidence")})
        # A small margin retains neighbouring unclassified caption pixels.
        pad_x, pad_y = max(8, tw * .018), max(12, min(40, th * .055))
        expanded = [max(0., expanded[0]-pad_x), max(0., expanded[1]-pad_y),
                    min(float(width), expanded[2]+pad_x), min(float(height), expanded[3]+pad_y)]
        for exclusion in ui:
            ub = exclusion["box"]
            # Shrink only the margin, never the detected table itself.
            if _overlap(expanded, ub) and _overlap(table_box, ub) == 0:
                if ub[3] <= table_box[1]:
                    expanded[1] = max(expanded[1], ub[3])
                elif ub[1] >= table_box[3]:
                    expanded[3] = min(expanded[3], ub[1])
                elif ub[2] <= table_box[0]:
                    expanded[0] = max(expanded[0], ub[2])
                elif ub[0] >= table_box[2]:
                    expanded[2] = min(expanded[2], ub[0])
        regions.append({**region, "id": f"region-{index+1}", "table_box": table_box, "box": expanded, "ancillary": nearby})
    if not regions:
        warnings.append({"code": "roi_uncertain", "message": "尚无可靠表区，使用原图并等待布局/文字证据。"})
    return regions, ui, warnings


def _quality(image, evidence, regions):
    scale = min(1., 1400 / max(image.size))
    small = np.asarray(image.convert("L").resize((max(1, round(image.width*scale)), max(1, round(image.height*scale)))), dtype=np.float32)
    gx, gy = np.diff(small, axis=1), np.diff(small, axis=0)
    contrast = float(np.percentile(small, 95)-np.percentile(small, 5))
    laplace = (small[:-2, 1:-1] + small[2:, 1:-1] + small[1:-1, :-2] + small[1:-1, 2:] - 4*small[1:-1, 1:-1]) if min(small.shape) > 3 else np.zeros(1)
    sharpness = float(np.var(laplace))
    _, texts = _evidence_items(evidence)
    heights = [b[3]-b[1] for item in texts if item.get("kind") not in UI_KINDS and (b := _box(item, image.width, image.height))]
    flags = []
    if contrast < 18 or sharpness < 4:
        flags.append({"code": "low_detail", "severity": "high", "message": "图像对比/细节不足，不能可靠还原不可见内容。"})
    elif sharpness < 35:
        flags.append({"code": "blur", "severity": "medium", "message": "存在模糊迹象；指标仅用于分流，不代表准确率。"})
    if heights and np.median(heights) < 16:
        flags.append({"code": "small_text", "severity": "high" if np.median(heights) < 10 else "medium", "message": "文字尺寸较小，应保留分辨率并按需局部识别。"})
    # Local bright smooth islands against a darker environment may be glare.
    # A white page alone is not sufficient evidence.
    glare_fraction = 0.
    if cv2 is not None:
        rgb = np.asarray(image.resize((small.shape[1], small.shape[0])))
        bright = np.all(rgb >= 249, axis=2).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
        for n in range(1, count):
            x, y, w, h, area = stats[n]
            if .008 < area / small.size < .3 and min(w, h) >= 10:
                margin = small[max(0,y-12):min(small.shape[0],y+h+12),max(0,x-12):min(small.shape[1],x+w+12)]
                if float(np.percentile(margin, 25)) < 220:
                    glare_fraction += area / small.size
        if glare_fraction > .015:
            flags.append({"code": "possible_glare", "severity": "medium", "message": "局部高亮可能遮挡文字；白底和反光需结合原图核对。"})
    # Spectral peak is only a suspicion: ruled paper also has periodic energy.
    sample = small[::max(1, small.shape[0]//512), ::max(1, small.shape[1]//512)]
    spectrum = np.abs(np.fft.rfft2((sample-sample.mean()) * np.outer(np.hanning(sample.shape[0]), np.hanning(sample.shape[1]))))
    fy, fx = np.fft.fftfreq(sample.shape[0])[:,None], np.fft.rfftfreq(sample.shape[1])[None,:]
    band = (np.abs(fy) + fx > .18) & (np.abs(fy) + fx < .8)
    energy = spectrum[band]
    periodicity = float(np.max(energy) / max(1., np.mean(energy))) if len(energy) else 0.
    if periodicity > 90 and contrast > 20:
        flags.append({"code": "possible_moire", "severity": "low", "message": "有周期纹理，需与真实表格网格区分；不自动锐化或补像。"})
    for region in regions:
        box = region["table_box"]
        if min(box[0], box[1], image.width-box[2], image.height-box[3]) <= 3:
            flags.append({"code": "edge_cutoff", "severity": "high", "region_id": region["id"], "message": "表区接触拍摄边缘，可能缺行/列。"})
    return {"flags": flags, "metrics": {"contrast": contrast, "laplacian_variance": sharpness,
             "median_text_height": float(np.median(heights)) if heights else None,
             "possible_glare_fraction": glare_fraction, "spectral_peak_ratio": periodicity,
             "edge_energy": float((np.mean(np.abs(gx))+np.mean(np.abs(gy)))/2) if gx.size and gy.size else 0.},
            "unreadable": any(f["code"] == "low_detail" or f["code"] == "small_text" and f["severity"] == "high" for f in flags)}


def _quadrilateral(image, evidence):
    supplied = (evidence or {}).get("screen_polygon")
    if supplied is not None:
        points = np.asarray(supplied, dtype=np.float32)
        source = "screen_detector"
    elif cv2 is not None:
        scale = min(1., 1200/max(image.size))
        grey = np.asarray(image.convert("L").resize((round(image.width*scale), round(image.height*scale))))
        edges = cv2.Canny(grey, 45, 140)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5,5), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        options = []
        for contour in contours:
            approx = cv2.approxPolyDP(contour, .025*cv2.arcLength(contour, True), True)
            area = abs(cv2.contourArea(approx))
            if len(approx) == 4 and cv2.isContourConvex(approx) and .35*grey.size < area < .98*grey.size:
                options.append((area, approx[:,0,:]/scale))
        if not options:
            return None
        points = max(options, key=lambda pair: pair[0])[1]
        source = "pixel_planar_boundary"
    else:
        return None
    if points.shape != (4,2) or not np.isfinite(points).all():
        return None
    # Canonical TL,TR,BR,BL order; reject concave/crossed/small quadrilaterals.
    center = points.mean(axis=0)
    points = points[np.argsort(np.arctan2(points[:,1]-center[1], points[:,0]-center[0]))]
    points = np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)
    cross = [np.cross(points[(n+1)%4]-points[n],points[(n+2)%4]-points[(n+1)%4]) for n in range(4)]
    if not all(float(c) > 0 for c in cross) or np.min(points) < 0 or np.any(points[:,0] > image.width) or np.any(points[:,1] > image.height):
        return None
    if abs(sum(points[n,0]*points[(n+1)%4,1]-points[(n+1)%4,0]*points[n,1] for n in range(4)))/2 < image.width*image.height*.15:
        return None
    return points, source


def _homography(source, destination):
    rows, values = [], []
    for (x,y),(u,v) in zip(source, destination):
        rows += [[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]]
        values += [u,v]
    return np.append(np.linalg.solve(np.asarray(rows), np.asarray(values)), 1).reshape(3,3)


def _tile_offsets(length, size, overlap):
    if length <= size:
        return [0]
    offsets = list(range(0, max(1,length-size+1), size-overlap))
    if offsets[-1] != length-size:
        offsets.append(length-size)
    return offsets


def prepare_screen_image(input_path, output_dir, evidence=None, *, max_regions=8,
                         tile_size=1600, overlap=128, max_tiles=12, perspective=True):
    """Save bounded candidates and a JSON manifest; no OCR or GPU work occurs.

    ``evidence`` accepts layout regions and OCR text boxes in normalized-original
    coordinates, and optionally a screen detector polygon. It must not contain
    reference/ideal ROIs. Candidate transforms are flat 3x3 matrices compatible
    with ``ocr_workbench.coordinates``. Caller selects per-region versus full
    image recognition and must use the quality assessor before automatic export.
    """
    if not (1 <= max_regions <= 32 and 256 <= tile_size <= 4096 and 0 <= overlap < tile_size/2 and 0 <= max_tiles <= 64):
        raise ValueError("无效的表区/分块预算")
    input_path, output_dir = Path(input_path).resolve(), Path(output_dir).resolve()
    if input_path == output_dir or input_path.is_relative_to(output_dir):
        raise ValueError("预处理输出目录不能包含原始输入")
    with Image.open(input_path) as opened:
        if opened.width*opened.height > 80_000_000:
            raise ValueError("图像像素超过安全处理上限")
        orientation = opened.getexif().get(274, 1)
        image = normalized_rgb(opened)
    output_dir.mkdir(parents=True, exist_ok=True)
    regions, ui, warnings = detect_business_regions(image, evidence, max_regions=max_regions)
    quality = _quality(image, evidence, regions)
    result = {"schema_version": 1, "policy_version": POLICY_VERSION,
              "input_path": str(input_path), "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
              "width": image.width, "height": image.height, "coordinate_space": "exif_normalized_original",
              "exif_orientation": orientation, "prepared_images": [], "business_regions": regions,
              "excluded_regions": ui, "quality": quality, "warnings": warnings, "primary_id": "original"}

    def save(candidate, key, kind, mapping, *, region_id=None, **extra):
        target = output_dir/(key+".png")
        candidate.save(target, format="PNG")
        inverted = inverse(mapping)
        item = {"id": key, "path": str(target), "kind": kind, "region_id": region_id,
                "width": candidate.width, "height": candidate.height, "source_to_prepared": list(mapping),
                "prepared_to_source": inverted, "source_polygon": polygon(inverted, box_polygon([0,0,candidate.width,candidate.height])), **extra}
        result["prepared_images"].append(item)
        return item

    save(image, "original", "original", IDENTITY)
    region_images = []
    for region in regions:
        box = [max(0,math.floor(region["box"][0])),max(0,math.floor(region["box"][1])),
               min(image.width,math.ceil(region["box"][2])),min(image.height,math.ceil(region["box"][3]))]
        cropped = image.crop(box)
        mapping = [1.,0.,-box[0],0.,1.,-box[1],0.,0.,1.]
        item = save(cropped, region["id"]+"-crop", "table_crop", mapping, region_id=region["id"], role="complete_region")
        region_images.append((cropped,item))
    if len(regions) == 1 and regions[0]["score"] >= .82 and not warnings:
        result["primary_id"] = regions[0]["id"]+"-crop"
    if perspective:
        quad = _quadrilateral(image, evidence)
        if quad:
            points, source = quad
            width = max(2,round(max(np.linalg.norm(points[1]-points[0]),np.linalg.norm(points[2]-points[3]))))
            height = max(2,round(max(np.linalg.norm(points[3]-points[0]),np.linalg.norm(points[2]-points[1]))))
            transform = _homography(points, [[0,0],[width,0],[width,height],[0,height]])
            if cv2 is not None:
                corrected = Image.fromarray(cv2.warpPerspective(np.asarray(image), transform, (width,height), flags=cv2.INTER_CUBIC, borderValue=(255,255,255)))
            else:
                coefficients = np.linalg.inv(transform).ravel()
                coefficients /= coefficients[8]
                corrected = image.transform((width,height), Image.Transform.PERSPECTIVE, coefficients[:8], Image.Resampling.BICUBIC)
            save(corrected, "perspective", "perspective", transform.ravel().tolist(), role="alternate", detection_source=source,
                 business_regions=[{**r, "polygon": polygon(transform.ravel().tolist(), box_polygon(r["box"]))} for r in regions])
    remaining = max_tiles
    for cropped, item in region_images:
        dense = max(cropped.size) > tile_size and (min(cropped.size) > 600 or any(f["code"] == "small_text" for f in quality["flags"]))
        if not dense:
            continue
        offsets = [(x,y) for y in _tile_offsets(cropped.height,tile_size,overlap) for x in _tile_offsets(cropped.width,tile_size,overlap)]
        if len(offsets) > remaining:
            warnings.append({"code": "tile_limit", "region_id": item["region_id"], "message": "分块超过预算，保留整表；不可把部分分块当作完整结果。"})
            continue
        remaining -= len(offsets)
        for number,(x,y) in enumerate(offsets,1):
            mapping = multiply([1.,0.,-x,0.,1.,-y,0.,0.,1.], item["source_to_prepared"])
            save(cropped.crop((x,y,min(x+tile_size,cropped.width),min(y+tile_size,cropped.height))),
                 item["region_id"]+f"-tile-{number}", "tile", mapping, region_id=item["region_id"],
                 role="local_evidence", overlap=overlap, parent_candidate=item["id"])
    (output_dir/"screen-preparation.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result
