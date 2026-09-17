"""Optional local-v3 anchors, with auditable, acyclic independent dependencies.

Only exact shared-OCR values may become anchors. A bracket preserves both
bounding row/column intervals and their distances. No box interpolation occurs.
All proposals are checked together: a tie or reuse rejects every claimant.
"""
from collections import Counter
import time


def axis_support(a, p, anchors, axis):
    start, end = axis + '_start', axis + '_end'
    direct = [(b, q) for b, q in anchors if b.id != a.id
              and (getattr(a, start), getattr(a, end)) == (getattr(b, start), getattr(b, end))
              and (getattr(p, start), getattr(p, end)) == (getattr(q, start), getattr(q, end))]
    if direct:
        return {b.id for b, _ in direct}
    before = [(b, q) for b, q in anchors if getattr(b, end) <= getattr(a, start)
              and getattr(q, end) <= getattr(p, start)]
    after = [(b, q) for b, q in anchors if getattr(b, start) >= getattr(a, end)
             and getattr(q, start) >= getattr(p, end)]
    # Nearest independently matched boundaries, never a single-sided extrapolation.
    if not before or not after:
        return set()
    lo = max(getattr(b, end) for b, _ in before)
    hi = min(getattr(b, start) for b, _ in after)
    before = [(b, q) for b, q in before if getattr(b, end) == lo]
    after = [(b, q) for b, q in after if getattr(b, start) == hi]
    if any(getattr(a, start) - getattr(b, end) != getattr(p, start) - getattr(q, end)
           for b, q in before):
        return set()
    if any(getattr(b, start) - getattr(a, end) != getattr(q, start) - getattr(p, end)
           for b, q in after):
        return set()
    return {b.id for b, _ in before + after}


def neighbor_support(a, p, anchors):
    from ocr_workbench.table_matching import _order_compatible
    if any(not _order_compatible(a, p, b, q) for b, q in anchors if b.id != a.id):
        return []
    row, col = (axis_support(a, p, anchors, axis) for axis in ('row', 'column'))
    return sorted(row | col) if any(r != c for r in row for c in col) else []


def refine_anchors(adopted, predicted, assigned, anchors, order_bad, policy, deadline):
    from ocr_workbench.table_matching import _span, _value, _order_compatible
    anchors = [(a, p) for a, p in anchors if p.cell_polygon and not p.reason_codes and p.geometry_origin != 'text_extent']
    lineage = {a.id: {'roots': [a.id], 'parents': [], 'round': 0} for a, _ in anchors}
    initial = {a.id for a, _ in anchors}
    by_value = {}
    for p in predicted:
        value = _value(p, assigned)
        if value and p.cell_polygon and not p.reason_codes and p.geometry_origin != 'text_extent':
            by_value.setdefault((value, _span(p)), []).append(p)
    for round_index in range(1, policy.get('maximum_anchor_rounds', 8) + 1):
        used = {p.id for _, p in anchors}
        matched = {a.id for a, _ in anchors}
        proposals = []
        for a in adopted:
            if time.perf_counter() > deadline:
                return anchors, lineage, 'timeout'
            if not a.matching_text or a.id in matched or a.id in order_bad:
                continue
            candidates = []
            for p in by_value.get((a.matching_text, _span(a)), []):
                if p.id in used:
                    continue
                support = neighbor_support(a, p, anchors)
                roots = sorted({root for aid in support for root in lineage[aid]['roots']})
                if len(roots) >= 2 and a.id not in roots:
                    candidates.append((a, p, support, roots))
            if len(candidates) == 1:
                proposals.append(candidates[0])
        owners = Counter(p.id for _, p, _, _ in proposals)
        conflicts = set()
        for i, (a, p, _, _) in enumerate(proposals):
            for b, q, _, _ in proposals[i + 1:]:
                if not _order_compatible(a, p, b, q):
                    conflicts.update((a.id, b.id))
        accepted = [(a, p, support, roots) for a, p, support, roots in proposals
                    if owners[p.id] == 1 and a.id not in conflicts]
        if not accepted:
            break
        for a, p, support, roots in accepted:
            anchors.append((a, p))
            lineage[a.id] = {'roots': roots, 'parents': support, 'round': round_index}
    assert all(set(record['roots']) <= initial for record in lineage.values())
    return anchors, lineage, None
