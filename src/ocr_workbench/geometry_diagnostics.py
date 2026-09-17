"""Online rejection diagnostics. This module never accepts ground truth."""
from collections import Counter

STAGES = ('inference', 'detection', 'structure', 'table_identity', 'text_anchor',
          'local_correspondence', 'geometry', 'accepted')
REASONS = {
    'inference_failed': 'inference', 'timeout': 'inference',
    'no_table_candidates': 'detection', 'no_cell_candidates': 'detection',
    'invalid_polygon': 'geometry', 'coordinate_version_mismatch': 'geometry',
    'malformed_structure': 'structure', 'boxes_slots_out_of_sync': 'structure',
    'span_conflict': 'structure', 'topology_mismatch': 'structure',
    'table_identity_ambiguous': 'table_identity', 'prediction_table_reused': 'table_identity',
    'text_coverage_insufficient': 'text_anchor', 'no_text_tokens': 'text_anchor',
    'text_anchor_disagrees': 'text_anchor', 'cross_cell_token': 'text_anchor',
    'repeated_value_ambiguous': 'text_anchor', 'empty_cell_without_anchors': 'text_anchor',
    'token_version_mismatch': 'text_anchor', 'token_assignment_ambiguous': 'text_anchor',
    'no_local_anchors': 'local_correspondence', 'order_conflict': 'local_correspondence',
    'span_mismatch': 'local_correspondence', 'candidate_tie': 'local_correspondence',
    'candidate_budget_exceeded': 'local_correspondence', 'prediction_cell_reused': 'local_correspondence',
    'noncontiguous_group': 'geometry', 'text_extent_only': 'geometry',
    'postprocessed_box_without_unique_detection_support': 'geometry',
    'unverified_lineage': 'geometry', 'accepted': 'accepted',
}


def diagnostic(*reasons):
    codes = sorted(set(reasons or ('accepted',)), key=lambda r: (STAGES.index(REASONS.get(r, 'geometry')), r))
    return {'primary_failure_stage': REASONS.get(codes[0], 'geometry'), 'reason_codes': codes,
            'diagnostic_kind': 'matcher_rejection', 'reason': codes[0]}


def summarize(mappings):
    cells = [m for m in mappings if m['target']['kind'] == 'cell']
    return {'targets': len(cells), 'primary_failure_stages': dict(Counter(m['primary_failure_stage'] for m in cells)),
            'reason_codes_nonadditive': dict(Counter(r for m in cells for r in m['reason_codes']))}
