"""Seal actual perspective geometry, unchanged content and explicit limits."""
from pathlib import Path

from common import fpath,frozen_json,read_json
from controlled_recovery import bound,require_environment
from quality import paired_report


def main():
    require_environment()
    root=fpath('F:/OCR/output/platform-specialized-agent-20261007')
    names={
        'native_run':'runs/m1-h-rectified-validation-direct-20261011-008/result.json',
        'native_exit':'runs/m1-h-rectified-validation-direct-prepare-20261011-008/native-exit.json',
        'independent_export':'evaluation/m1-h-rectified-validation-direct-independent-008-001/review.json',
        'native_geometry':'evaluation/rectified-grid-native008-review-001/result.json',
        'native_coordinates':'evaluation/rectified-grid-native008-coordinate-review-001/review.json',
        'native_quality':'evaluation/m1-h-rectified-validation-direct-quality-008-001.json',
        'prior_native_quality':'evaluation/m1-h-math-validation-direct-quality-007-001.json',
        'development_geometry':'evaluation/rectified-grid-development60-002/result.json',
        'development_coordinates':'evaluation/rectified-grid-development60-coordinate-review-003/review.json',
        'validation_geometry':'evaluation/rectified-grid-validation40-001/result.json',
        'validation_coordinates':'evaluation/rectified-grid-validation40-coordinate-review-001/review.json',
        'header_role_rejection':'evaluation/header-role-counterexample-001/review.json',
        'fallback_budget_audit':'evaluation/m1-precision-fallback-budget-audit-002/report.json',
        'grid_tests':'evaluation/rectified-grid-tests-002/junit.xml',
        'native_regression':'evaluation/rectified-grid-native-regression-001/junit.xml',
        'final_geometry_component':'evaluation/rectified-grid-final-component-003/junit.xml',
        'receipt_tamper_gates':'evaluation/rectified-grid-receipt-gates-001/receipt.json',
        'frontend_tests':'evaluation/rectified-grid-frontend-001/junit.xml',
        'frontend_typecheck':'evaluation/rectified-grid-frontend-001/typescript.log',
        'frontend_build':'evaluation/rectified-grid-frontend-001/build.log',
        'final_geometry_policy':'evaluation/rectified-grid-policy-003/policy.json',
        'actual_common_source':'source-snapshots/rectified-grid-common-20261011-001/freeze.json',
        'latest_backend_frontend_source':'source-snapshots/rectified-grid-ui-common-20261011-002/freeze.json',
        'preparation_preflight_failure':'evaluation/rectified-grid-prepare-preflight-failure-001.json',
        'initial_test_bootstrap_failure':'evaluation/rectified-grid-tests-001/junit.xml',
        'initial_inapplicable_full_cell_score':'evaluation/rectified-grid-development60-coordinate-review-001/review.json',
        'corrected_reference_semantics_score':'evaluation/rectified-grid-development60-coordinate-review-002/review.json'}
    evidence={key:bound(root/name) for key,name in names.items()}
    records={key:read_json(root/names[key]) for key in ('native_run','independent_export','native_geometry',
        'native_coordinates','native_quality','prior_native_quality','development_geometry','development_coordinates',
        'header_role_rejection','fallback_budget_audit','actual_common_source','latest_backend_frontend_source')}
    native,quality,geometry=records['native_run'],records['native_quality'],records['native_geometry']
    after=quality['summary']['totals']
    delta=paired_report(records['prior_native_quality'],quality)['totals']
    if (native['counts']['needs_review']!=40 or native['counts']['pending']!=0
            or records['independent_export']['status']!='passed'
            or records['independent_export']['xlsx_readback']['workbooks']!=40
            or geometry['after_cells']!=72 or geometry['localized_issue_items']!=0
            or records['native_coordinates']['counts']['correct_cells']!=72
            or any(delta[key]!=0 for key in ('net_visible_cell_repairs','new_visible_error_cells','review_reference_cells_delta'))
            or not records['header_role_rejection']['header_v2_adoption_rejected']):
        raise ValueError('Actual source/content/geometry boundary differs')
    for name,sha in read_json(root/names['final_geometry_policy'])['files'].items():
        if records['actual_common_source']['sources']['ocr']['files'][name]!=sha or records[
                'latest_backend_frontend_source']['sources']['ocr']['files'][name]!=sha:
            raise ValueError('Native and latest source geometry differ')
    report={'format':'source-rectified-grid-actual-closure-v1','evidence':evidence,
        'sources':{'main_helpers':'cfe59a1','ocr_backend':'8d38afa','ocr_current_with_frontend':'6668bdd',
            'platform':'2a533d0','classification':'d90707b'},
        'development':{'scope_inputs':60,'before_images':6,'after_images':12,'before_cells':147,'after_cells':246,
            'new_full_cell_coordinates_correct':99,'new_full_cell_coordinates_total':99,
            'all_synthetic_full_cell_coordinates_correct':182,'all_synthetic_full_cell_coordinates_total':182,
            'existing_public_text_bounds_passed':58,'existing_public_text_bounds_total':64,
            'existing_public_text_reference_discrepancies':6,'blanket_coordinate_qualification':False},
        'native_validation':{'scope_inputs':40,'model_success':40,'context_success':40,'xlsx':40,
            'before_images':2,'after_images':3,'before_cells':48,'after_cells':72,
            'independently_correct_full_cell_coordinates':72,'new_full_cell_coordinates_correct':24,
            'all_captured_geometry_reproduced':True,'localized_issue_items':0,
            'wall_seconds':native['wall_seconds'],'first_native_result':native['first_native_result'],
            'exact_table_images':after['visible_table_exact_images'],'visible_exact_cells':after['visible_exact_cells'],
            'visible_reference_cells':after['visible_reference_cells'],'critical_exact_cells':after['visible_critical_exact_cells'],
            'critical_reference_cells':after['visible_critical_cells'],'metadata_exact':after['metadata_exact_targets'],
            'metadata_total':after['metadata_visible_targets'],'metadata_extra':after['metadata_extra_parts'],
            'ui_leaks':after['ui_marker_leaks'],'review_images':40,'review_cells':2711,
            'actual_content_delta_against_H007':delta,'human_review_time_measured':False},
        'ui':'Verified source-grid bases are accepted and perspective polygons retained;25 tests/typecheck/build passed. No final browser qualification.',
        'rejected_header_v2':'Synthetic no-header vocabulary matrix releases3 data cells; first-row glossary/topology is insufficient semantic role proof. No native policy adopted.',
        'technical_fallback':'Original GLM006 failed-image remaining budgets are0.633/175.478/176.819 seconds. Blanket3-image fallback is infeasible; no old run revived or budget extended. New optional route remains unimplemented/unqualified.',
        'preserved_failures':'Bootstrap collection failure; preparation before source freeze completed; inapplicable full-cell equality score on public text boxes; remaining6 public reference discrepancies.',
        'limits':['Development/previously exposed validation only.','Geometry gain is not character repair or measured review relief.',
            'Actual H008 includes backend8d38afa; subsequent UI6668bdd has component/build evidence only.',
            'Wall times across changed sources are descriptive; no speedup claim.',
            'M1/M2 and final M3/M4 gates remain open.'],
        'owned_native_or_model_processes_remaining':0,'formal_current':'v0.9.3','holdout_read':False,
        'native_original_databases_opened':False,'completed_plan':False,'goal_status':'active'}
    frozen_json(root/'evaluation/rectified-grid-closure-20261011-001.json',report)
    print({'native_xlsx':40,'correct_source_cells':72,'all_images_still_reviewed':40,'completed_plan':False})


if __name__=='__main__':main()
