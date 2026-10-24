"""Diagnostics locate only supported geometry and never rewrite OCR values."""

from copy import deepcopy
import unittest

from ocr_workbench.screen_diagnostics import build_screen_diagnostics,compare_screen_candidates
from ocr_workbench.screen_quality import assess_screen_result


def table(rows=2,columns=2,origin=None):
    cells=[{'row':row,'column':column,'row_span':1,'column_span':1,'text':str(10+row+column),
            'box':[100+column*100,200+row*50,200+column*100,250+row*50]}
           for row in range(rows) for column in range(columns)]
    if origin:
        for cell in cells:cell['geometry_origin']=origin
    return {'project_image_version':'image-1','tables':[{'rows':rows,'columns':columns,'cells':cells,'box':[100,200,300,300]}]}


def preparation(rows=2,columns=2):
    return {'width':800,'height':600,'business_regions':[{'id':'r1','table_box':[100,200,300,300],
        'sources':['pixel_grid'],'grid_reliable':True,'observed_grid_rows':rows,'observed_grid_columns':columns,
        'observed_grid':{'horizontal_lines':[200,250,300],'vertical_lines':[100,200,300],
                         'source':'pixel_grid','coordinate_space':'exif_normalized_original'}}]}


class ScreenDiagnosticTests(unittest.TestCase):
    def test_missing_rows_columns_detected_without_inventing_cell_locations(self):
        raw=table();raw['tables'][0].pop('box')
        for cell in raw['tables'][0]['cells']:cell.pop('box')
        before=deepcopy(raw)
        report=build_screen_diagnostics(raw,preparation(3,4))
        codes={item['code'] for item in report['items']}
        self.assertTrue({'missing_observed_rows','missing_observed_columns'}<=codes)
        self.assertTrue(all(item['location']['level']=='table' for item in report['items']))
        self.assertEqual(report['checks']['observed_spans'],'unavailable')
        self.assertEqual(raw,before)

    def test_empty_candidate_has_independent_region_reason_but_true_no_table_is_not_fabricated(self):
        empty={'tables':[]}
        report=build_screen_diagnostics(empty,preparation())
        issue=next(item for item in report['items'] if item['code']=='empty_table_with_region_evidence')
        self.assertEqual(issue['location']['level'],'table')
        self.assertEqual(issue['evidence'][0]['kind'],'observed_business_region')
        no_table=build_screen_diagnostics(empty,{})
        self.assertNotIn('empty_table_with_region_evidence',{item['code'] for item in no_table['items']})
        self.assertEqual(empty,{'tables':[]})

    def test_observed_merge_and_misalignment_use_direct_cell_edges_only(self):
        raw=table(origin='direct_detection')
        raw['tables'][0]['cells'][0]['box']=[100,200,300,250]
        raw['tables'][0]['cells'][1].pop('geometry_origin')
        raw['tables'][0]['cells'][2]['box']=[200,250,300,300]
        before=deepcopy(raw)
        report=build_screen_diagnostics(raw,preparation())
        spans=next(item for item in report['items'] if item['code']=='observed_span_disagreement')
        self.assertEqual(spans['location']['level'],'cell')
        self.assertEqual(spans['details']['observed_span'],[0,0,1,2])
        self.assertTrue(any(item['code']=='observed_cell_misalignment' for item in report['items']))
        self.assertEqual(raw,before)
        for cell in raw['tables'][0]['cells']:cell['geometry_origin']='text_extent'
        weak=build_screen_diagnostics(raw,preparation())
        self.assertNotIn('observed_span_disagreement',{item['code'] for item in weak['items']})
        self.assertEqual(weak['checks']['observed_spans'],'unavailable')

    def test_existing_logical_cell_hint_degrades_to_table_when_cell_box_is_unverified(self):
        raw=table();issue={'code':'numeric_conflict','severity':'high','message':'金额不同','upgrade_kind':'text','table':0,'row':1,'column':0}
        report=build_screen_diagnostics(raw,preparation(),assessment={'issues':[issue]})
        location=report['items'][0]['location']
        self.assertEqual(location['level'],'table')
        self.assertEqual(location['unlocalized_cell'],{'row':1,'column':0})
        self.assertNotIn('local_reread',{action['action'] for action in report['items'][0]['applicable_actions']})

    def test_budget_and_unverified_capability_block_compute_but_keep_every_issue(self):
        raw=table(origin='direct_detection')
        issue={'code':'numeric_conflict','severity':'high','message':'金额不同','upgrade_kind':'text','table':0,'row':1,'column':0}
        report=build_screen_diagnostics(raw,preparation(),assessment={'issues':[issue]},remaining_seconds=10,
                                        capabilities={'local_reread':True,'precision_subbatch':True})
        actions={item['action']:item for item in report['items'][0]['applicable_actions']}
        self.assertTrue(actions['review_original']['enabled'])
        self.assertFalse(actions['local_reread']['enabled'])
        self.assertEqual(actions['local_reread']['reason'],'remaining_budget_insufficient')
        unknown=build_screen_diagnostics(raw,preparation(),assessment={'issues':[issue]})
        self.assertFalse(next(action for action in unknown['items'][0]['applicable_actions'] if action['action']=='local_reread')['enabled'])
        self.assertEqual(len(report['items']),1)

    def test_precision_uses_remaining_allowance_above_minimum_and_caps_reservation(self):
        issue={'code':'numeric_conflict','severity':'high','upgrade_kind':'text','table':0,'row':1,'column':0}
        for remaining,enabled,reserved in ((0,False,0),(9.999,False,9.999),(10,True,10),
                                          (45,True,45),(180,True,180),(300,True,180),(None,True,180)):
            with self.subTest(remaining=remaining):
                report=build_screen_diagnostics(table(),preparation(),assessment={'issues':[issue]},
                    remaining_seconds=remaining,capabilities={'precision_subbatch':True})
                action=next(item for item in report['items'][0]['applicable_actions']
                            if item['action']=='precision_subbatch')
                self.assertEqual(action['enabled'],enabled)
                self.assertEqual(action['budget_seconds'],reserved)
                self.assertEqual(action['reason'],None if enabled else 'remaining_budget_insufficient')

    def test_existing_structure_compare_needs_capability_but_no_model_budget(self):
        for capability,enabled in ((True,True),(False,False),(None,False)):
            with self.subTest(capability=capability):
                report=build_screen_diagnostics(table(),preparation(2,3),remaining_seconds=0,
                                                capabilities={'structure_compare':capability})
                issue=next(item for item in report['items'] if item['code']=='missing_observed_columns')
                action=next(item for item in issue['applicable_actions'] if item['action']=='structure_compare')
                self.assertEqual(action['enabled'],enabled)
                self.assertEqual(action['budget_seconds'],0)
                self.assertEqual(action['reason'],None if enabled else 'capability_not_verified')

    def test_missing_zero_or_sign_is_not_silenced_by_substring_match(self):
        raw=table(origin='native_cell')
        raw['tables'][0]['cells'][0]['text']='10'
        evidence=[{'text':'1','box':[110,210,190,240],'source':'PP-OCRv6-auxiliary'}]
        assessment=assess_screen_result(raw,preparation(),evidence)
        self.assertIn('localized_text_disagreement',{item['code'] for item in assessment['issues']})
        raw['tables'][0]['cells'][0]['text']='-10'
        evidence[0]['text']='10'
        self.assertIn('localized_text_disagreement',{item['code'] for item in assess_screen_result(raw,preparation(),evidence)['issues']})

    def test_independent_span_candidate_is_comparison_not_authoritative_truth(self):
        raw=table();raw['tables'][0]['region_id']='r1'
        other=deepcopy(raw['tables'][0]);other['cells'][0]['column_span']=2;other['cells'].pop(1)
        evidence={'image_version':'image-1','tables':[{'id':'s1','region_id':'r1','provider':'SLANeXt',
                    'polygon':[[100,200],[300,200],[300,300],[100,300]],'skeleton':other}]}
        before=deepcopy(raw)
        report=build_screen_diagnostics(raw,preparation(),structure_evidence=evidence)
        self.assertTrue(any(item['code']=='independent_structure_span' for item in report['items']))
        self.assertTrue(all(item['automatic_apply'] is False for item in report['items']))
        self.assertEqual(raw,before)
        evidence['image_version']='different'
        wrong=build_screen_diagnostics(raw,preparation(),structure_evidence=evidence)
        self.assertEqual(wrong['checks']['independent_structure'],'rejected_version_mismatch')

    def test_candidate_compare_retains_both_values_and_current_selection(self):
        current=table(origin='native_cell');other=deepcopy(current)
        other['tables'][0]['cells'][-1]['text']='012'
        before=deepcopy((current,other))
        report=compare_screen_candidates(current,other,preparation())
        self.assertEqual(report['selected_candidate_id'],'current')
        self.assertFalse(report['selection_applied'])
        self.assertTrue(report['conflicts'])
        self.assertEqual((current,other),before)
        other['project_image_version']='other-image'
        with self.assertRaises(ValueError):compare_screen_candidates(current,other)

    def test_lower_risk_alternative_is_recommended_without_reversing_current_value_or_adopting_it(self):
        current=table(origin='native_cell');other=deepcopy(current)
        current['tables'][0]['cells'][-1]['text']='<?>'
        report=compare_screen_candidates(current,other,preparation())
        self.assertEqual(report['recommended_candidate_id'],'alternative')
        self.assertEqual(report['selected_candidate_id'],'current')
        value=next(conflict for conflict in report['conflicts'] if conflict['code']=='candidate_value_conflict')
        self.assertEqual(value['value'],'<?>');self.assertEqual(value['alternative'],'12')
        self.assertFalse(report['selection_applied'])

    def test_invalid_independent_structure_is_an_unavailable_check_not_a_crash_or_corrected_cell(self):
        raw=table();before=deepcopy(raw)
        proposed={'image_version':'image-1','tables':[{'skeleton':{'rows':2,'columns':2,'cells':[None]}}]}
        report=build_screen_diagnostics(raw,preparation(),structure_evidence=proposed)
        self.assertIn('invalid_independent_structure',{item['code'] for item in report['items']})
        self.assertEqual(report['checks']['independent_structure'],'unavailable_no_unique_table_identity')
        self.assertEqual(raw,before)

    def test_observed_ui_overlap_is_localized_but_never_removed(self):
        raw=table(origin='native_cell');before=deepcopy(raw);info=preparation()
        info['excluded_regions']=[{'kind':'ruler','box':[100,200,200,250],'source':'ordered_ui_evidence'}]
        report=build_screen_diagnostics(raw,info)
        issue=next(item for item in report['items'] if item['code']=='cell_overlaps_observed_ui')
        self.assertEqual(issue['location']['level'],'cell')
        self.assertEqual(raw,before)


if __name__=='__main__':unittest.main()
