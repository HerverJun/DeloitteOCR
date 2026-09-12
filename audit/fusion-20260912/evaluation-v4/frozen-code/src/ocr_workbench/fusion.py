"""Offline CPU fusion of immutable OCR snapshots and explainable candidates.

Implemented independently from algorithm descriptions; no upstream OCR/UI code
is imported or copied. Raw confidence values are never averaged.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from ocr_workbench.fusion_alignment import (
    canonical_edit, cell_key, complete_structure, field_kind, fingerprint,
    match_tables, reliable_cells, source_tables, table_content, text_alignment,
    topology, valid_polygon,
)

ENGINES = ("ppocr", "paddlevl", "glm", "hunyuan")
STRUCTURAL_ENGINES = ("paddlevl", "glm", "hunyuan")
TERMINAL = {"succeeded", "failed", "cancelled"}


def default_policy(kind="print", mode="conservative"):
    """Unvalidated development policy. Shipping policy must carry its evidence."""
    if kind not in {"table", "print", "handwriting"} or mode not in {"conservative", "aggressive"}:
        raise ValueError("未知融合内容类型或策略模式")
    return {"version": "development-unvalidated", "content_type": kind, "mode": mode,
            "baseline": "glm" if kind == "table" else "hunyuan", "algorithm": "baseline_evidence",
            "weights": {engine: 1 for engine in ENGINES}, "automatic_replacement": False,
            "denominator": "expected_capable_sources", "minimum_valid_sources": 2,
            "minimum_coverage": 1.0, "minimum_support": 1.0,
            "allow_numeric_replacement": False,
            "limits": {"max_text_characters": 24000, "max_alignment_product": 4_000_000,
                       "max_span_characters": 2000, "max_lines": 1000,
                       "max_tables": 100, "max_cells": 100000, "max_total_cells": 200000,
                       "region_iou": .65, "header_similarity": .75, "row_anchor_similarity": .6,
                       "axis_anchor_similarity": .45, "match_margin": .15, "text_similarity": .65,
                       "body_anchor_characters": 12, "body_anchor_minimum": 3, "body_anchor_coverage": .6},
            "validation": {"status": "unvalidated", "independent": False}}


def load_policy(bundle, kind, mode):
    default_policy(kind, mode)  # Validate API enum values before path/key access.
    path = Path(bundle) / "config" / "fusion-policy.json"
    if not path.is_file():
        return default_policy(kind, mode)
    spec = json.loads(path.read_text("utf-8"))
    policy = deepcopy(spec["policies"][kind][mode])
    policy["file_sha256"] = fingerprint(spec)
    return policy


def validate_sources(sources):
    if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
        raise ValueError("请选择有效的原始 OCR 来源（最多 16 份）")
    identities, unique = set(), {}
    for source in sources:
        if source.get("engine") not in ENGINES or source.get("status") not in TERMINAL | {"unsupported"}:
            raise ValueError("融合来源包含未知引擎或尚未终止的任务")
        stamp = tuple(source.get(k) for k in ("image_id", "version_id", "batch"))
        if not all(stamp):
            raise ValueError("来源缺少图片、处理版本或批次关系，不能推定可比较")
        identities.add(stamp)
        if source["status"] == "succeeded":
            original = source.get("original", {})
            if original.get("engine") != source["engine"] or original.get("origin", "single-engine") != "single-engine" or original.get("fusion"):
                raise ValueError("只能使用原始单引擎输出，人工修订与融合结果不能计票")
            if not isinstance(original.get("text"), str) or not isinstance(original.get("tables"), list):
                raise ValueError("来源没有有效的统一原始输出")
            if source.get("fingerprint") and source["fingerprint"] != fingerprint(original):
                raise ValueError("原始来源指纹已改变")
        # Every engine contributes once even across retries, package variants
        # or prompts. Disagreeing duplicate originals are rejected, not voted.
        key = source["engine"]
        if key in unique and fingerprint(unique[key].get("original")) != fingerprint(source.get("original")):
            raise ValueError("同一引擎有不同原始输出，请明确选择一份来源")
        unique.setdefault(key, deepcopy(source))
    if len(identities) != 1:
        raise ValueError("融合来源必须是同一图片、同一处理版本、同一批次")
    return list(unique.values())


def candidates(values, policy):
    grouped = {}
    for engine, value in values.items():
        identity = fingerprint(value)
        candidate = grouped.setdefault(identity, {"id": identity, "value": value, "sources": [], "weight": 0})
        candidate["sources"].append(engine)
        candidate["weight"] += policy["weights"].get(engine, 1) if policy["algorithm"] == "weighted" else 1
    return sorted(grouped.values(), key=lambda c: (-c["weight"], c["id"]))


def choose(base, values, expected, policy, reliable=True):
    options = candidates(values, policy)
    valid = len(values)
    coverage = valid / max(1, len(expected))
    best = options[0] if options else None
    tied = bool(best and len(options) > 1 and abs(best["weight"] - options[1]["weight"]) < 1e-9)
    support_denominator = len(expected) if policy["denominator"] == "expected_capable_sources" else valid
    support = len(best["sources"]) / max(1, support_denominator) if best else 0
    protected = isinstance(base, str) and any(field_kind(value) not in {"text", "empty"} for value in values.values())
    accepted = bool(best and reliable and not tied and valid >= policy["minimum_valid_sources"]
                    and coverage >= policy["minimum_coverage"] and support >= policy["minimum_support"])
    replacement = bool(accepted and policy["automatic_replacement"] and (not protected or policy["allow_numeric_replacement"]))
    selected = best["value"] if replacement else base
    reason = ("alignment_unreliable" if not reliable else "tie" if tied else
              "insufficient_evidence" if not accepted else "numeric_protection" if protected and not policy["allow_numeric_replacement"] else
              "suggestions_only" if not policy["automatic_replacement"] else "evidence_rule_passed")
    return {"selected": deepcopy(selected), "baseline": deepcopy(base), "candidates": options,
            "expected_sources": expected, "valid_sources": list(values), "coverage": coverage,
            "support": support, "support_denominator": support_denominator,
            "denominator_definition": policy["denominator"], "reason": reason,
            "automatic": replacement, "human_confirmed": False,
            "needs_review": len(options) != 1 or coverage < 1 or not reliable}


def location(source, table=None, cell=None, text=None):
    original = source["original"]
    image = original.get("image", {})
    evidence = {"level": "image", "polygon": None, "version_id": source["version_id"],
                "source_result_id": source.get("result_id"), "reason": "没有可靠坐标，显示全图"}
    if original.get("project_image_version", source["version_id"]) != source["version_id"]:
        return {**evidence, "reason": "来源图像版本不匹配，显示全图"}
    if cell:
        polygon = valid_polygon(cell.get("polygon"), image)
        if polygon:
            return {**evidence, "level": "cell", "polygon": polygon, "reason": "来源提供真实单元格坐标"}
    if table and table.get("region_polygon"):
        return {**evidence, "level": "region", "polygon": table["region_polygon"], "reason": "仅有表格区域坐标"}
    if text:
        matches = [b for b in original.get("blocks", []) if b.get("text") == text]
        if len(matches) == 1:
            polygon = valid_polygon(matches[0].get("polygon"), image)
            if polygon:
                return {**evidence, "level": "region", "polygon": polygon, "reason": "文字区域坐标"}
    return evidence


def fuse(sources, policy, session_id, cancelled=lambda: False):
    start = time.perf_counter()
    sources = validate_sources(sources)
    kind, limits = policy["content_type"], policy["limits"]
    expected = [s["engine"] for s in sources if kind != "table" or s["engine"] in STRUCTURAL_ENGINES]
    usable = {s["engine"]: s for s in sources if s["status"] == "succeeded" and s["engine"] in expected}
    if not usable:
        raise ValueError("没有可用的融合来源：" + "; ".join(s["engine"] + "=" + s["status"] for s in sources))
    base_engine = policy["baseline"] if policy["baseline"] in usable else next(e for e in ENGINES if e in usable)
    baseline = usable[base_engine]
    original = baseline["original"]
    units = []
    tables_by_engine = {e: source_tables(s) for e, s in usable.items()}
    if any(len(ts) > limits["max_tables"] or any(len(t["cells"]) > limits["max_cells"] for t in ts)
           or sum(len(t["cells"]) for t in ts) > limits["max_total_cells"] for ts in tables_by_engine.values()):
        raise ValueError("来源表格规模超过融合资源上限，请拆分图片区域")
    tables = deepcopy(tables_by_engine[base_engine])
    skeleton_fallbacks = {}
    if kind == "table":
        initial_mappings = {e: match_tables(tables, ts, limits) for e, ts in tables_by_engine.items() if e != base_engine}
        for index, table in enumerate(tables):
            if complete_structure(table):
                continue
            complete = {e: tables_by_engine[e][mapping[index]] for e, mapping in initial_mappings.items()
                        if index in mapping and complete_structure(tables_by_engine[e][mapping[index]])}
            if complete:
                # Choose one existing, identity-matched legal skeleton. This is
                # an explicit initial-structure fallback, never per-cell voting.
                engine = max(complete, key=lambda e: (policy["weights"].get(e, 0), -ENGINES.index(e)))
                replacement = deepcopy(complete[engine])
                replacement["source"] = table.get("source")
                tables[index] = replacement
                skeleton_fallbacks[index] = {"engine": engine, "baseline": table}

    def check_cancel():
        if cancelled():
            from ocr_workbench.adapter import Cancelled
            raise Cancelled()

    def unit(target, evidence, loc, category, statuses=None):
        identity = fingerprint({"session": session_id, "target": target,
                                "candidates": evidence["candidates"]})
        value = {"id": identity, "target": target, "category": category, "location": loc,
                 "source_states": statuses or {s["engine"]: s["status"] for s in sources}, **evidence}
        value["basis"] = fingerprint({"target": target, "candidates": evidence["candidates"]})
        units.append(value)
        return value

    if any(not complete_structure(t) for t in tables):
        # A sparse or invalid baseline must never become a new filled-in grid
        # through HTML regeneration. Preserve its exact text for manual review.
        evidence = choose(original["text"], {e: s["original"]["text"] for e, s in usable.items()}, expected,
                          {**policy, "automatic_replacement": False}, reliable=False)
        evidence["reason"] = "baseline_structure_invalid"
        unit({"kind": "document"}, evidence, location(baseline), "structure")
        for engine, candidates_ in tables_by_engine.items():
            for index, table in enumerate(candidates_):
                if complete_structure(table):
                    unit({"kind": "unmatched_table", "source": engine, "source_table": index},
                         choose(None, {engine: table}, expected, {**policy, "automatic_replacement": False}, reliable=False),
                         location(usable[engine], table=table), "structure")
        edit = {"text": original["text"], "tables": []}
    elif kind == "table":
        mappings = {e: match_tables(tables, ts, limits) for e, ts in tables_by_engine.items() if e != base_engine}
        for i, table in enumerate(tables):
            check_cancel()
            table_id = fingerprint({"session": session_id, "source": base_engine, "table": i, "content": table_content(table)})[:24]
            table["fusion_id"] = table_id
            aligned = {base_engine: tables_by_engine[base_engine][i]}
            states = {s["engine"]: ("unsupported" if s["engine"] == "ppocr" else s["status"]) for s in sources}
            for engine, mapping in mappings.items():
                if i in mapping:
                    aligned[engine] = tables_by_engine[engine][mapping[i]]
                else:
                    states[engine] = "unmatched"
            structural = {e: t for e, t in aligned.items() if complete_structure(t)}
            structure_ok = complete_structure(table) and all(reliable_cells(table, t, limits) for t in aligned.values())
            # Never synthesize merge topology. Incompatible complete tables stay
            # whole candidates and no per-cell writes happen within this table.
            if not structure_ok or len(aligned) != len(usable):
                evidence = choose(table, structural, expected, {**policy, "automatic_replacement": False}, reliable=False)
                if i in skeleton_fallbacks:
                    evidence["baseline"] = deepcopy(skeleton_fallbacks[i]["baseline"])
                    evidence["skeleton_source"] = skeleton_fallbacks[i]["engine"]
                    evidence["reason"] = "baseline_structure_invalid"
                unit({"kind": "table", "table_id": table_id, "structure": fingerprint(topology(table))}, evidence,
                     location(baseline, table=table), "structure", states)
                continue
            aligned_cells = {engine: {cell_key(c): c["text"] for c in candidate["cells"]} for engine, candidate in aligned.items()}
            for cell in table["cells"]:
                check_cancel()
                key = cell_key(cell)
                values = {engine: cells[key] for engine, cells in aligned_cells.items()}
                evidence = choose(cell["text"], values, expected, policy)
                unit({"kind": "cell", "table_id": table_id, "row": cell["row"], "column": cell["column"],
                      "structure": fingerprint(topology(table))}, evidence, location(baseline, table=table, cell=cell),
                     field_kind(cell["text"]) if cell["text"] else "empty", states)
                cell["text"] = evidence["selected"]
                cell["confidence"] = None
                # Position is separate, version-bound source evidence, not a
                # confidence or a newly inferred fused coordinate.
                cell["polygon"] = None
        for engine, mapping in mappings.items():
            for j, table in enumerate(tables_by_engine[engine]):
                if j in mapping.values():
                    continue
                evidence = choose(None, {engine: table}, expected, {**policy, "automatic_replacement": False}, reliable=False)
                unit({"kind": "unmatched_table", "source": engine, "source_table": j}, evidence,
                     location(usable[engine], table=table), "structure")
        if not tables:
            unit({"kind": "document"}, choose(original["text"], {e: s["original"]["text"] for e, s in usable.items()}, expected,
                 {**policy, "automatic_replacement": False}, reliable=False), location(baseline), "structure")
        edit = canonical_edit({"text": original["text"], "tables": tables})
    else:
        base = original["text"]
        # Structured table spans must remain protected even in a text document.
        # Review complete original strings when tables would otherwise be mixed
        # with independently aligned text and invalidate their reading order.
        if any(tables_by_engine.values()):
            unit({"kind": "text", "start": 0, "end": len(base)},
                 choose(base, {e: s["original"]["text"] for e, s in usable.items()}, expected,
                        {**policy, "automatic_replacement": False}, reliable=False), location(baseline), "structure")
            edit = canonical_edit({"text": base, "tables": tables})
        else:
            alignments = {e: text_alignment(base, s["original"]["text"], limits) for e, s in usable.items() if e != base_engine}
            # Union overlapping changes across engines into one complete span.
            changes = sorted((p["start"], p["end"]) for spans in alignments.values() for p in spans if p["operation"] != "equal")
            ranges = []
            for a, z in changes:
                if ranges and a <= ranges[-1][1]:
                    ranges[-1] = (ranges[-1][0], max(z, ranges[-1][1]))
                else:
                    ranges.append((a, z))
            if not ranges:
                ranges = [(0, len(base))]
            parts, cursor = [], 0
            for a, z in ranges:
                check_cancel()
                values, reliable, states = {base_engine: base[a:z]}, True, {s["engine"]: s["status"] for s in sources}
                for engine, spans in alignments.items():
                    selected = [p for p in spans if (p["start"] < z and p["end"] > a) or
                                (p["start"] == p["end"] and a <= p["start"] <= z)]
                    value = []
                    for p in selected:
                        if p["operation"] == "equal":
                            value.append(base[max(a, p["start"]):min(z, p["end"])])
                        elif a <= p["start"] and p["end"] <= z:
                            value.append(p["value"])
                            reliable = reliable and p["reliable"]
                        else:
                            reliable = False
                            states[engine] = "unmatched"
                    if states[engine] != "unmatched":
                        values[engine] = "".join(value)
                evidence = choose(base[a:z], values, expected, policy, reliable=reliable)
                evidence["operations"] = {e: [p for p in spans if (p["start"] < z and p["end"] > a) or p["start"] == p["end"] == a]
                                          for e, spans in alignments.items()}
                parts.append(base[cursor:a])
                start_offset = sum(len(v) for v in parts)
                parts.append(evidence["selected"])
                unit({"kind": "text", "start": start_offset, "end": start_offset + len(evidence["selected"])},
                     evidence, location(baseline, text=base[a:z]), field_kind(base[a:z]), states)
                cursor = z
            parts.append(base[cursor:])
            edit = {"text": "".join(parts), "tables": []}
    check_cancel()
    return {"schema_version": 1, "status": "success", "origin": "fusion", "engine": "fusion",
            "text": edit["text"], "tables": edit["tables"], "blocks": [], "raw": None,
            "image": deepcopy(original.get("image", {})), "project_image_version": baseline["version_id"],
            "elapsed_seconds": time.perf_counter()-start, "load_seconds": 0,
            "fusion": {"session_id": session_id, "policy": deepcopy(policy), "policy_sha256": fingerprint(policy),
                       "created": datetime.now(timezone.utc).isoformat(), "baseline": base_engine,
                       "baseline_missing": base_engine != policy["baseline"],
                       "structure_fallbacks": [{"table": i, "engine": v["engine"]} for i, v in skeleton_fallbacks.items()],
                       "sources": [{k: v for k, v in s.items() if k != "original"} for s in sources],
                       "source_fingerprints": {s["engine"]: fingerprint(s.get("original")) for s in sources},
                       "expected_sources": expected, "valid_sources": list(usable),
                       "coverage": len(usable)/max(1, len(expected)), "units": units,
                       "unresolved": sum(u["needs_review"] for u in units), "human_confirmed": False}}
