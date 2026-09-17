"""Reviewable structure evidence; no model text votes and no reference labels.

All skeletons use the same source tokens and the existing local-v3 ownership
policy. Missing slots stay missing. Original IDs, geometry and numbering survive.
"""
from collections import Counter
from copy import deepcopy
import time

from ocr_workbench.coordinates import bounds
from ocr_workbench.editing import validate_edit
from ocr_workbench.fusion_alignment import complete_structure, topology
from ocr_workbench.geometry_contract import (
    as_json, fingerprint, intersection_area, matching_text, polygon_iou,
    signed_area, tokens_from_blocks,
)
from ocr_workbench.geometry_providers import adapt_prediction
from ocr_workbench.table_matching import assign_tokens, policy_for_algorithm


VERSION = "structure-review-v1"


def span(cell):
    return tuple(cell[k] for k in ("row", "column", "row_span", "column_span"))


def table_value(table):
    """Ignore display offsets but include headers, literal text and provenance."""
    return {k: v for k, v in table.items() if k not in ("source", "region_polygon")}


def prepare_candidates(prediction, blocks, *, source_result, image_version, width, height):
    policy = policy_for_algorithm("local-v3")
    tokens, rejected = tokens_from_blocks(blocks, source_result=source_result,
        image_version=image_version, width=width, height=height)
    tables = adapt_prediction(prediction, width, height)
    membership = {t.id: [c["id"] for c in tables if c["polygon"] and
        intersection_area(t.polygon, c["polygon"]) / abs(signed_area(t.polygon)) >= .8] for t in tokens}
    output = []
    for table in tables:
        started = time.perf_counter()
        local = [t for t in tokens if membership[t.id] == [table["id"]]]
        ambiguous = [t.id for t in tokens if table["id"] in membership[t.id] and len(membership[t.id]) > 1]
        cells = table["cells"]
        if len(cells) > policy["maximum_cells"]:
            records, owned, error = [], {}, "candidate_budget_exceeded"
        else:
            records, owned, error = assign_tokens(local, cells, policy,
                deadline=started + policy["table_budget_ms"] / 1000)
        parsed = {(c["row"], c["column"]): c for c in (table.get("structure") or {}).get("cells", [])}
        skeleton = {"rows": max((c.row_end for c in cells), default=0),
                    "columns": max((c.column_end for c in cells), default=0), "cells": []}
        for cell in cells:
            group = owned.get(cell.id, [])
            item = {"row": cell.row_start, "column": cell.column_start,
                "row_span": cell.row_end-cell.row_start, "column_span": cell.column_end-cell.column_start,
                "text": " ".join(t.raw_text for t in group), "confidence": None,
                "structure_source": {"provider": cell.provider, "model_version": cell.model_version,
                    "candidate_cell_id": cell.id, "original_cell_id": cell.original_cell_id,
                    "token_ids": [t.id for t in group], "text_source_result": source_result,
                    "geometry_origin": cell.geometry_origin, "cell_polygon": cell.cell_polygon,
                    "original_polygon": cell.original_polygon, "transform": cell.transform,
                    "index_mapping": cell.index_mapping, "derivation": cell.derivation,
                    "text_state": "sourced" if group else "unverified_empty"}}
            header = parsed.get((cell.row_start, cell.column_start), {}).get("is_header")
            if header is not None:
                item["is_header"] = header
                role = parsed.get((cell.row_start,cell.column_start),{}).get('header_role')
                if role:
                    item['header_role'] = role
            skeleton["cells"].append(item)
        issues = list(table.get("reason_codes", []))
        if not complete_structure(skeleton):
            issues.append("incomplete_grid")
        if error:
            issues.append(error)
        assigned = {r.token_id for r in records if r.adopted_cell_id}
        unassigned = [t.id for t in local if t.id not in assigned] + ambiguous
        output.append({"id": table["id"], "provider": table["provider"], "region_id": table.get("region_id"),
            "polygon": table["polygon"], "skeleton": skeleton, "original_cells": [as_json(c) for c in cells],
            "assignments": [as_json(r) for r in records], "unassigned_token_ids": unassigned,
            "ambiguous_table_token_ids": ambiguous, "reason_codes": sorted(set(issues)),
            "timing_ms": (time.perf_counter()-started)*1000})
    return {"version": VERSION, "image_version": image_version, "policy": policy,
        "token_pool_sha256": fingerprint([as_json(t) for t in tokens]), "tokens": [as_json(t) for t in tokens],
        "rejected_tokens": rejected, "tables": output, "contributes_to_votes": False}


def identify_tables(current, candidates):
    """One-to-one table identity; equal text alone cannot identify repeated tables."""
    rankings = []
    for table in current:
        values = Counter(matching_text(c["text"]) for c in table["cells"] if matching_text(c["text"]))
        rank = []
        for index, candidate in enumerate(candidates):
            other = Counter(matching_text(c["text"]) for c in candidate["skeleton"]["cells"] if matching_text(c["text"]))
            anchors = sorted(v for v in values if values[v] == other[v] == 1)
            region = table.get("region_polygon")
            overlap = polygon_iou(region, candidate["polygon"]) if region and candidate["polygon"] else 0
            same_region = bool(table.get("region_id") and table["region_id"] == candidate.get("region_id"))
            if (same_region or overlap >= .6) or len(anchors) >= 2:
                # Geometry can veto a text match to a different table on this page.
                if region and candidate["polygon"] and overlap < .15:
                    continue
                score = (2 if same_region else overlap*2) + len(anchors)/max(1, min(sum(values.values()), sum(other.values())))
                rank.append((score, index, {"unique_anchors": anchors, "region_iou": overlap, "same_region": same_region}))
        rankings.append(sorted(rank, reverse=True, key=lambda v: v[0]))
    chosen = [rank[0] if rank and (len(rank) == 1 or rank[0][0]-rank[1][0] >= .2) else None for rank in rankings]
    count = Counter(item[1] for item in chosen if item)
    return [item if item and count[item[1]] == 1 else None for item in chosen]


def differences(current, proposed):
    output = []
    for axis in ("rows", "columns"):
        if current[axis] != proposed[axis]:
            output.append({"kind": "missing_"+axis if current[axis] < proposed[axis] else "extra_"+axis,
                "current": current[axis], "candidate": proposed[axis],
                "cells": [{"row": c["row"], "column": c["column"]} for c in proposed["cells"]]})
    before = {(c["row"], c["column"]): c for c in current["cells"]}
    after = {(c["row"], c["column"]): c for c in proposed["cells"]}
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        if a is None or b is None or span(a) != span(b):
            output.append({"kind": "span" if a and b else "cell_partition", "row": key[0], "column": key[1],
                           "current": list(span(a)) if a else None, "candidate": list(span(b)) if b else None})
        if a and b and (a.get("is_header", False),a.get('header_role')) != (b.get("is_header", False),b.get('header_role')):
            output.append({"kind": "header", "row": key[0], "column": key[1],
                           "current": {'is_header':a.get("is_header", False),'role':a.get('header_role')},
                           "candidate": {'is_header':b.get("is_header", False),'role':b.get('header_role')}})
    return output


def preserve_values(current, proposed, original, tokens, manual_bindings=()):
    """Retain literal accepted/manual values only through exclusive source identity.

    Changed spans may split/merge existing values only when the exact shared
    tokens account for them. A manual edit without a unique destination blocks
    application; the original value remains visible in the proposal conflicts.
    """
    output = deepcopy(proposed)
    by_id = {t["id"]: t for t in tokens}
    original_by_span = {span(c): c for c in (original or {}).get("cells", [])}
    topology_stable = original and topology(current) == topology(original)
    original_counts = Counter(matching_text(c["text"]) for c in (original or current)["cells"])
    next_counts = Counter(matching_text(c["text"]) for c in output["cells"])
    conflicts, retained = [], []
    reserved = set()
    for old in current["cells"]:
        original_cell = original_by_span.get(span(old)) if topology_stable else None
        source_value = (original_cell or old)["text"]
        changed = original_cell is not None and old["text"] != original_cell["text"]
        token_ids = set(old.get("structure_source", {}).get("token_ids", []))
        targets = []
        for i, cell in enumerate(output["cells"]):
            ids = set(cell.get("structure_source", {}).get("token_ids", []))
            if token_ids and token_ids == ids:
                targets.append(i)
            elif not token_ids and matching_text(source_value) and original_counts[matching_text(source_value)] == next_counts[matching_text(source_value)] == 1 and matching_text(source_value) == matching_text(cell["text"]):
                targets.append(i)
        # An existing manual box is independent evidence for repeated/manual values.
        bindings = [b for b in manual_bindings if b["target"].get("row") == old["row"] and b["target"].get("column") == old["column"]]
        if len(bindings) == 1:
            poly = bindings[0]["polygon"]
            matches = [i for i, c in enumerate(output["cells"]) if c.get("structure_source", {}).get("cell_polygon") and polygon_iou(poly, c["structure_source"]["cell_polygon"]) >= .8]
            if len(matches) == 1:
                targets = matches
        if len(targets) == 1 and targets[0] not in reserved:
            target = targets[0]
            reserved.add(target)
            cell = output["cells"][target]
            cell["text"] = old["text"]
            cell.setdefault("structure_source", {})["retained_from"] = {"row": old["row"], "column": old["column"], "manual_value": changed}
            if bindings:
                cell["structure_source"]["manual_binding"] = bindings[0]
            retained.append({"from": [old["row"], old["column"]], "to": [cell["row"], cell["column"]], "manual": changed})
            continue
        if not old["text"]:
            # Manual clearing must not be silently refilled.
            if changed:
                conflicts.append({"kind": "manual_value_unmapped", "row": old["row"], "column": old["column"], "text": ""})
            continue
        # Splitting/merging is allowed only with exact literal source coverage.
        ids = token_ids
        if not ids and original_cell and original_cell.get("structure_source"):
            ids = set(original_cell["structure_source"].get("token_ids", []))
        # Existing slots delimit a local merge/split; surrounding cells were
        # independently used to identify the table. Never split a token itself.
        if not ids and not changed and old["row_span"]*old["column_span"] > 1:
            local = [c for c in output["cells"] if old["row"] <= c["row"] and
                c["row"]+c["row_span"] <= old["row"]+old["row_span"] and old["column"] <= c["column"] and
                c["column"]+c["column_span"] <= old["column"]+old["column_span"]]
            literal = "".join(c["text"] for c in local)
            ids_list = [t for c in local for t in c.get("structure_source", {}).get("token_ids", [])]
            if len(local) > 1 and ids_list and len(ids_list) == len(set(ids_list)) and matching_text(literal) == matching_text(old["text"]):
                ids = set(ids_list)
        if ids and not changed:
            represented = [t for c in output["cells"] for t in c.get("structure_source", {}).get("token_ids", []) if t in ids]
            if Counter(represented) == Counter(ids) and matching_text("".join(by_id[t]["raw_text"] for t in represented if t in by_id)) == matching_text(old["text"]):
                continue
        # Several unmodified source cells may become one merged cell. Preserve
        # each token once; a manual value cannot pass this source-text check.
        if not changed:
            joined = [c for c in output["cells"] if c["row"] <= old["row"] and old["row"]+old["row_span"] <= c["row"]+c["row_span"] and
                c["column"] <= old["column"] and old["column"]+old["column_span"] <= c["column"]+c["column_span"]]
            if len(joined) == 1:
                merged = joined[0]
                local = [c for c in current["cells"] if merged["row"] <= c["row"] and c["row"]+c["row_span"] <= merged["row"]+merged["row_span"] and
                    merged["column"] <= c["column"] and c["column"]+c["column_span"] <= merged["column"]+merged["column_span"]]
                if len(local) > 1 and merged.get("structure_source", {}).get("token_ids") and matching_text("".join(c["text"] for c in local)) == matching_text(merged["text"]):
                    continue
        # Equal topology is a valid positional preservation of unmodified cells,
        # even when values repeat; it does not assert geometric cell identity.
        same = [i for i, c in enumerate(output["cells"]) if span(c) == span(old)]
        if topology(current) == topology(output) and len(same) == 1 and same[0] not in reserved:
            cell = output["cells"][same[0]]
            if matching_text(source_value) == matching_text(cell["text"]):
                cell["text"] = old["text"]
                reserved.add(same[0])
                continue
        conflicts.append({"kind": "manual_value_unmapped" if changed else "current_value_unmapped",
                          "row": old["row"], "column": old["column"], "text": old["text"]})
    return output, conflicts, retained


def local_variants(current, proposed):
    """Rectangular local merge/split/header patches; no row/column compression."""
    if current["rows"] != proposed["rows"] or current["columns"] != proposed["columns"]:
        for axis, size, other in (("row", "rows", "columns"),("column", "columns", "rows")):
            if proposed[size] <= current[size] or proposed[other] != current[other] or any(c[axis+"_span"] != 1 for c in current["cells"]+proposed["cells"]):
                continue
            def band(table, at):
                return [(c["column" if axis == "row" else "row"], c["column_span" if axis == "row" else "row_span"], c["text"], c.get("is_header",False))
                        for c in table["cells"] if c[axis] == at]
            before = [band(current,i) for i in range(current[size])]
            after = [band(proposed,i) for i in range(proposed[size])]
            if any(not b or before.count(b) != 1 or after.count(b) != 1 for b in before):
                continue
            positions = [after.index(b) for b in before]
            if positions != sorted(positions):
                continue
            inserted = [i for i in range(proposed[size]) if i not in positions]
            if inserted == list(range(inserted[0],inserted[-1]+1)):
                return [{"kind":"insert_"+size,"range": [inserted[0],inserted[-1]+1],"table":deepcopy(proposed)}]
        return []
    def slots(c):
        return {(r, col) for r in range(c["row"], c["row"]+c["row_span"])
                for col in range(c["column"], c["column"]+c["column_span"])}
    output, seen = [], set()
    for seed in current["cells"] + proposed["cells"]:
        region = slots(seed)
        old = [c for c in current["cells"] if slots(c) <= region]
        new = [c for c in proposed["cells"] if slots(c) <= region]
        if not old or not new or set().union(*(slots(c) for c in old)) != region or set().union(*(slots(c) for c in new)) != region:
            continue
        if not differences({"rows": current["rows"], "columns": current["columns"], "cells": old},
                           {"rows": current["rows"], "columns": current["columns"], "cells": new}):
            continue
        key = tuple(sorted(region))
        if key in seen:
            continue
        seen.add(key)
        table = deepcopy(current)
        table["cells"] = sorted([deepcopy(c) for c in current["cells"] if c not in old] + deepcopy(new), key=lambda c: (c["row"], c["column"]))
        validate_edit({"text": "", "tables": [table]})
        output.append({"kind": "merge" if len(new) < len(old) else "split" if len(new) > len(old) else "header",
                       "range": [min(r for r, c in region), min(c for r, c in region), max(r for r, c in region)+1, max(c for r, c in region)+1], "table": table})
    return output
