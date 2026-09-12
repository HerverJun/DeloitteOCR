"""Bounded, conservative alignment of existing OCR units; no model dependency."""

from collections import Counter, defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import unicodedata

from ocr_workbench.editing import validate_edit, tables_html
from ocr_workbench.tables import parse_tables, table_source


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def cell_key(cell):
    return tuple(cell[k] for k in ("row", "column", "row_span", "column_span"))


def topology(table):
    return {"rows": table["rows"], "columns": table["columns"],
            "spans": sorted(cell_key(c) for c in table["cells"])}


def table_content(table):
    return {**topology(table), "caption": table.get("caption", ""),
            "values": sorted((cell_key(c), c["text"]) for c in table["cells"])}


def complete_structure(table):
    try:
        validate_edit({"text": "", "tables": [table]})
    except (ValueError, TypeError, KeyError):
        return False
    return sum(c["row_span"] * c["column_span"] for c in table["cells"]) == table["rows"] * table["columns"]


def valid_polygon(polygon, image):
    if not isinstance(polygon, list) or not 3 <= len(polygon) <= 32:
        return None
    width, height = image.get("width", 0), image.get("height", 0)
    if not isinstance(width, (float, int)) or not isinstance(height, (float, int)):
        return None
    if any(not isinstance(p, (list, tuple)) or len(p) != 2 or
           any(type(v) not in (float, int) or not math.isfinite(v) for v in p) or
           not (0 <= p[0] <= width and 0 <= p[1] <= height) for p in polygon):
        return None
    if max(p[0] for p in polygon) <= min(p[0] for p in polygon) or max(p[1] for p in polygon) <= min(p[1] for p in polygon):
        return None
    return deepcopy(polygon)


def iou(left, right):
    if not left or not right:
        return 0
    def box(p):
        return min(v[0] for v in p), min(v[1] for v in p), max(v[0] for v in p), max(v[1] for v in p)
    a, b = box(left), box(right)
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - overlap
    return overlap / union if union else 0


def source_tables(source):
    """Attach region evidence only through a unique exact original block match."""
    original = source["original"]
    tables = deepcopy(original.get("tables", []))
    parsed = parse_tables(original.get("text", ""), warnings=[])
    for table in tables:
        matches = [p for p in parsed if table_content(p) == table_content(table)]
        if len(matches) == 1:
            table["source"] = matches[0].get("source")
        regions = []
        for block in original.get("blocks", []):
            if "table" not in str(block.get("kind", "")).lower():
                continue
            candidates = parse_tables(block.get("text", ""), warnings=[])
            if len(candidates) == 1 and table_content(candidates[0]) == table_content(table):
                polygon = valid_polygon(block.get("polygon"), original.get("image", {}))
                if polygon:
                    regions.append(polygon)
        table["region_polygon"] = regions[0] if len(regions) == 1 else None
    return tables


def overlap_tokens(left, right):
    a, b = Counter(normalized(v) for v in left if normalized(v)), Counter(normalized(v) for v in right if normalized(v))
    return sum((a & b).values()) / max(1, min(sum(a.values()), sum(b.values())))


def table_similarity(a, b, limits):
    geometry = iou(a.get("region_polygon"), b.get("region_polygon"))
    head_a = [c["text"] for c in a["cells"] if c["row"] == 0]
    head_b = [c["text"] for c in b["cells"] if c["row"] == 0]
    header = overlap_tokens(head_a, head_b)
    context = overlap_tokens([c["text"] for c in a["cells"] if c["column"] == 0 and c["row"] > 0],
                             [c["text"] for c in b["cells"] if c["column"] == 0 and c["row"] > 0])
    caption = bool(normalized(a.get("caption", ""))) and normalized(a.get("caption", "")) == normalized(b.get("caption", ""))
    if geometry >= limits["region_iou"]:
        return 2 + geometry
    if header >= limits["header_similarity"] and (context >= limits["row_anchor_similarity"] or caption):
        return header + context + (0.25 if caption else 0)
    # A missing first row or a merged label can destroy header/first-column
    # anchors. Multiple long, unique literal body cells still identify a table;
    # this establishes table identity only, never a row/cell mapping.
    body_a = Counter(normalized(c["text"]) for c in a["cells"] if len(normalized(c["text"])) >= limits.get("body_anchor_characters", 12))
    body_b = Counter(normalized(c["text"]) for c in b["cells"] if len(normalized(c["text"])) >= limits.get("body_anchor_characters", 12))
    anchors = {value for value in body_a.keys() & body_b.keys() if body_a[value] == body_b[value] == 1}
    coverage = len(anchors) / max(1, min(len(body_a), len(body_b)))
    if len(anchors) >= limits.get("body_anchor_minimum", 3) and coverage >= limits.get("body_anchor_coverage", .6):
        return 1 + coverage
    # Exact content is meaningful identity evidence, subject to uniqueness in
    # both documents; dimensions or array position alone never establish it.
    if table_content(a) == table_content(b):
        return 2
    return 0


def match_tables(baseline, candidate, limits):
    scores = [[table_similarity(a, b, limits) for b in candidate] for a in baseline]
    mapping = {}
    for i, row in enumerate(scores):
        if not row or max(row) <= 0:
            continue
        rank = sorted(range(len(row)), key=lambda j: row[j], reverse=True)
        j = rank[0]
        if len(rank) > 1 and row[j] - row[rank[1]] < limits["match_margin"]:
            continue
        column = sorted((scores[k][j], k) for k in range(len(scores)))
        if column[-1][1] != i or (len(column) > 1 and column[-1][0] - column[-2][0] < limits["match_margin"]):
            continue
        mapping[i] = j
    return mapping


def reliable_cells(a, b, limits):
    if not complete_structure(a) or not complete_structure(b) or topology(a) != topology(b):
        return False
    # Detect row/column shifts even when dimensions and merge topology agree.
    # A changed row requires a unique unchanged anchor at that same row.
    for axis, size in (("row", a["rows"]), ("column", a["columns"])):
        if size * size > limits["max_alignment_product"]:
            return False
        buckets_a, buckets_b = defaultdict(list), defaultdict(list)
        for cell in a["cells"]:
            buckets_a[cell[axis]].append(normalized(cell["text"]))
        for cell in b["cells"]:
            buckets_b[cell[axis]].append(normalized(cell["text"]))
        groups_a = [buckets_a[n] for n in range(size)]
        groups_b = [buckets_b[n] for n in range(size)]
        for n, values in enumerate(groups_a):
            if values == groups_b[n]:
                continue
            scores = [overlap_tokens(values, other) for other in groups_b]
            if not scores or scores[n] < limits["axis_anchor_similarity"]:
                return False
            if any(score >= scores[n] for k, score in enumerate(scores) if k != n):
                return False
    return True


def canonical_edit(edit):
    """Render each saved table once, then regenerate source offsets and hashes."""
    from ocr_workbench.editing import export_markdown
    value = deepcopy(edit)
    validate_edit(value)
    # Source scores and coordinates belong to immutable evidence. A whole-table
    # choice must not carry them into the fused/editor result as its own values.
    for table in value["tables"]:
        table.pop("region_polygon", None)
        table.pop("polygon", None)
        table.pop("confidence", None)
        for cell in table["cells"]:
            cell["confidence"] = None
            cell["polygon"] = None
    text = export_markdown(value)
    parsed = parse_tables(text, warnings=[])
    used = set()
    for table in value["tables"]:
        matches = [i for i, candidate in enumerate(parsed) if i not in used and table_content(candidate) == table_content(table)]
        if matches:
            # Matching serialized occurrences within this one canonical result
            # is not cross-engine table identity matching.
            index = matches[0]
            table["source"] = parsed[index]["source"]
            used.add(index)
        else:
            table.pop("source", None)
    value["text"] = text
    return value


def field_kind(text):
    if text == "":
        return "empty"
    if re.search(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}", text):
        return "date"
    if re.search(r"(?<!\w)(?:0\d{2,}|\d{8,}|[A-Z]+[-_]\d+)(?!\w)", text):
        return "identifier"
    if re.search(r"(?:[¥￥$€£]\s*-?\d|[-−]?\d[\d,]*\.\d{2}(?!\d))", text):
        return "amount"
    if re.search(r"\d", text):
        return "number"
    return "text"


def text_alignment(base, other, limits):
    """Return explicit original spans; resource-limit fallback keeps full text."""
    if max(len(base), len(other)) > limits["max_text_characters"] or len(base) * len(other) > limits["max_alignment_product"]:
        return [{"start": 0, "end": len(base), "other_start": 0, "other_end": len(other),
                 "value": other, "operation": "resource_limit", "reliable": False}]
    left, right = base.splitlines(keepends=True), other.splitlines(keepends=True)
    if max(len(left), len(right)) > limits["max_lines"]:
        return [{"start": 0, "end": len(base), "other_start": 0, "other_end": len(other),
                 "value": other, "operation": "resource_limit", "reliable": False}]
    # Character alignment is bounded; line anchors isolate long documents into
    # independent spans. Protected numeric lines remain whole strings.
    offsets_a, offsets_b = [0], [0]
    for line in left:
        offsets_a.append(offsets_a[-1] + len(line))
    for line in right:
        offsets_b.append(offsets_b[-1] + len(line))
    output = []
    unique_left = {line: n for n, line in enumerate(left) if left.count(line) == 1}
    shared_order = [unique_left[line] for line in right if line in unique_left and right.count(line) == 1]
    reading_order_changed = shared_order != sorted(shared_order)
    for op, a, z, b, y in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        before, after = "".join(left[a:z]), "".join(right[b:y])
        start, end = offsets_a[a], offsets_a[z]
        reliable = op == "equal"
        if op == "replace" and z-a == y-b == 1 and max(len(before), len(after)) <= limits["max_span_characters"]:
            reliable = SequenceMatcher(None, before, after, autojunk=False).ratio() >= limits["text_similarity"]
            if reliable and not reading_order_changed and field_kind(before) == field_kind(after) == "text":
                # Ordinary text may combine original fragments. Numeric lines
                # remain one literal candidate, even when field boundaries vary.
                for fragment, i, j, k, l in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                    output.append({"start": start+i, "end": start+j,
                                   "other_start": offsets_b[b]+k, "other_end": offsets_b[b]+l,
                                   "value": after[k:l], "operation": fragment,
                                   "reliable": fragment in {"equal", "replace"}})
                continue
        output.append({"start": start, "end": end, "other_start": offsets_b[b], "other_end": offsets_b[y],
                       "value": after, "operation": op, "reliable": reliable and not reading_order_changed,
                       "reading_order_changed": reading_order_changed})
    return output


def text_regions(source, limits):
    """Bind real regions to unique literal spans in this original text only."""
    original = source["original"]
    if original.get("project_image_version", source["version_id"]) != source["version_id"]:
        return [], "version_mismatch"
    blocks = original.get("blocks", [])
    if len(blocks) > limits["max_lines"]:
        return [], "region_limit"
    text, regions = original["text"], []
    for index, block in enumerate(blocks):
        value = block.get("text")
        polygon = valid_polygon(block.get("polygon"), original.get("image", {}))
        if not polygon or not isinstance(value, str) or not value.strip():
            continue
        if text.count(value) != 1:
            return [], "ambiguous_region_text"
        start = text.index(value)
        regions.append({"start": start, "end": start + len(value), "polygon": polygon, "block": index})
    regions.sort(key=lambda r: r["start"])
    if any(a["end"] > b["start"] for a, b in zip(regions, regions[1:])):
        return [], "overlapping_region_text"
    return regions, "available" if regions else "coordinates_unavailable"


def text_source_alignment(baseline, candidate, limits):
    """Associate source regions before bounded line/fragment alignment.

    Missing geometry permits the existing reading-order alignment. Ambiguous,
    conflicting or reordered geometry instead keeps the whole original as an
    unresolved candidate. No region or position is inferred from repeated text.
    """
    base, other = baseline["original"]["text"], candidate["original"]["text"]
    if max(len(base), len(other)) > limits["max_text_characters"] or len(base)*len(other) > limits["max_alignment_product"]:
        return text_alignment(base, other, limits)
    left, left_reason = text_regions(baseline, limits)
    right, right_reason = text_regions(candidate, limits)

    def unresolved(reason):
        return [{"start": 0, "end": len(base), "other_start": 0, "other_end": len(other), "value": other,
                 "operation": reason, "reliable": False, "reading_order_changed": reason == "region_order_changed",
                 "region_alignment": {"baseline": left_reason, "candidate": right_reason}}]

    if any(reason not in {"available", "coordinates_unavailable"} for reason in (left_reason, right_reason)):
        return unresolved("region_ambiguous")
    if not left or not right:
        return [{**span, "region_alignment": {"baseline": left_reason, "candidate": right_reason}}
                for span in text_alignment(base, other, limits)]
    if len(left)*len(right) > limits["max_alignment_product"]:
        return unresolved("resource_limit")
    scores = [[iou(a["polygon"], b["polygon"]) for b in right] for a in left]
    mapping = []
    for i, row in enumerate(scores):
        rank = sorted(range(len(row)), key=lambda j: row[j], reverse=True)
        j = rank[0]
        column = sorted((scores[k][j], k) for k in range(len(left)))
        if (row[j] < limits["region_iou"] or
            (len(rank) > 1 and row[j]-row[rank[1]] < limits["match_margin"]) or
            column[-1][1] != i or
            (len(column) > 1 and column[-1][0]-column[-2][0] < limits["match_margin"])):
            return unresolved("region_unmatched")
        mapping.append(j)
    if len(set(mapping)) != len(right) or len(mapping) != len(right):
        return unresolved("region_unmatched")
    if mapping != sorted(mapping):
        return unresolved("region_order_changed")
    spans, cursor_a, cursor_b = [], 0, 0

    def align(a, z, b, y, evidence=None):
        for span in text_alignment(base[a:z], other[b:y], limits):
            spans.append({**span, "start": span["start"]+a, "end": span["end"]+a,
                          "other_start": span["other_start"]+b, "other_end": span["other_end"]+b,
                          "region_alignment": evidence or {"kind": "uncovered_original_span"}})

    for i, j in enumerate(mapping):
        a, b = left[i], right[j]
        align(cursor_a, a["start"], cursor_b, b["start"])
        align(a["start"], a["end"], b["start"], b["end"],
              {"kind": "real_region", "baseline_block": a["block"], "candidate_block": b["block"], "iou": scores[i][j]})
        cursor_a, cursor_b = a["end"], b["end"]
    align(cursor_a, len(base), cursor_b, len(other))
    return spans
