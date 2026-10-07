"""Quality tests protect numeric truth, geometry and honest review states."""

from copy import deepcopy
import unittest

from ocr_workbench.screen_quality import assess_screen_result, select_screen_candidates, map_screen_result_to_source


def result(rows, boxes=True):
    cells = []
    for r,row in enumerate(rows):
        for c,value in enumerate(row):
            cells.append({"row":r,"column":c,"row_span":1,"column_span":1,"text":value,
                          "confidence":None,"box":[c*100,r*30,(c+1)*100,(r+1)*30] if boxes else None})
    return {"text":"", "tables":[{"rows":len(rows),"columns":len(rows[0]),"cells":cells,
                                      "box":[0,0,len(rows[0])*100,len(rows)*30] if boxes else None}]}


class ScreenQualityTests(unittest.TestCase):
    def codes(self, assessment):
        return {item["code"] for item in assessment["issues"]}

    def test_valid_grid_ids_blanks_and_decimal_values_preserved(self):
        raw = result([["编号","金额"],["00001","-12.30"],["123456789012345678","0.01"],["","(1.00)"]])
        before = deepcopy(raw)
        assessment = assess_screen_result(raw)
        self.assertTrue(assessment["automatic_delivery"])
        self.assertEqual(assessment["human_review"],"not_performed")
        self.assertEqual(assessment["checks"]["text_coverage"],"unavailable")
        self.assertEqual(raw,before)

    def test_legitimate_merge_not_missing_slots_and_overlap_invalid(self):
        raw = result([["Header","Header"],["A","5"]])
        raw["tables"][0]["cells"][0]["column_span"] = 2
        raw["tables"][0]["cells"].pop(1)
        assessment = assess_screen_result(raw)
        self.assertNotIn("missing_grid_slots",self.codes(assessment))
        self.assertTrue(assessment["automatic_delivery"])
        raw["tables"][0]["cells"].append({"row":0,"column":1,"row_span":1,"column_span":1,"text":"bad"})
        assessment = assess_screen_result(raw)
        self.assertIn("overlapping_cells",self.codes(assessment))
        self.assertFalse(assessment["automatic_delivery"])

    def test_missing_row_table_unassigned_text_and_output_cutoff(self):
        raw = result([["A","B"],["1","2"]])
        image = {"business_regions":[{"id":"r1","box":[0,0,200,120],"table_box":[0,0,200,120],"grid_reliable":True,"observed_grid_rows":4},
                                    {"id":"r2","box":[300,0,500,120]}]}
        # Current table geometry covers the observed region but returns fewer rows.
        raw["tables"][0]["box"] = [0,0,200,120]
        evidence = [{"text":"missing","box":[10,75,90,95],"confidence":.99}]
        raw["finish_reason"] = "length"
        assessment = assess_screen_result(raw,image,evidence)
        self.assertTrue({"missing_table","missing_observed_rows","unassigned_visible_text","output_cutoff"}.issubset(self.codes(assessment)))
        self.assertEqual(assessment["upgrade_kind"],"structure")

    def test_decimal_sum_is_only_doubt_not_correction(self):
        raw = result([["项目","金额"],["A","0.10"],["B","0.20"],["合计","0.30"]])
        assessment = assess_screen_result(raw)
        self.assertNotIn("arithmetic_mismatch",self.codes(assessment))
        raw["tables"][0]["cells"][-1]["text"] = "0.31"
        before = deepcopy(raw)
        assessment = assess_screen_result(raw)
        self.assertIn("arithmetic_mismatch",self.codes(assessment))
        self.assertFalse(assessment["automatic_delivery"])
        self.assertEqual(raw,before)
        mismatch = next(i for i in assessment["issues"] if i["code"]=="arithmetic_mismatch")
        self.assertEqual(mismatch["calculated_total"],"0.30")

    def test_repeated_rows_are_suspicion_not_automatic_deletion(self):
        raw = result([["类别","金额"],["A","1.00"],["A","1.00"]])
        assessment = assess_screen_result(raw)
        self.assertIn("duplicate_row_suspicion",self.codes(assessment))
        self.assertTrue(assessment["automatic_delivery"])
        self.assertEqual(raw["tables"][0]["rows"],3)

    def test_ui_text_is_excluded_by_geometry_not_keyword(self):
        raw = result([["Save","1000"],["toolbar","245.99"]])
        evidence = [{"kind":"toolbar","text":"Save 1000","box":[0,80,200,100]},
                    {"text":"245.99","box":[105,32,185,55],"confidence":.99}]
        assessment = assess_screen_result(raw,{"excluded_regions":[{"box":[0,80,200,100]}]},evidence)
        self.assertTrue(assessment["automatic_delivery"])
        self.assertEqual(raw["tables"][0]["cells"][0]["text"],"Save")

    def test_numeric_conflict_does_not_follow_two_votes_against_one(self):
        first = result([["编号","金额"],["0001","-125.50"]])
        second = result([["编号","金额"],["001","125.50"]])
        candidates = [{"id":"a","result":first},{"id":"b","result":second},{"id":"c","result":deepcopy(second)}]
        selection = select_screen_candidates(candidates,allow_text_fixes=True)
        self.assertEqual(selection["selected_id"],"a")
        self.assertEqual(selection["selected_result"]["tables"][0]["cells"][-1]["text"],"-125.50")
        self.assertFalse(selection["automatic_delivery"])
        self.assertTrue(selection["review_required"])
        self.assertTrue(all(c["severity"]=="high" for c in selection["conflicts"]))
        self.assertEqual(selection["modifications"],[])

    def test_unlocalized_and_structure_conflicts_do_not_silently_pass(self):
        first = result([["A","10"],["B","20"]],boxes=False)
        second = result([["X","100"],["Y","200"]],boxes=False)
        selected = select_screen_candidates([{"id":"a","raw":first},{"id":"b","raw":second}])
        self.assertFalse(selected["automatic_delivery"])
        self.assertIn("unmapped_candidate_difference",{c["code"] for c in selected["conflicts"]})
        self.assertEqual([a["id"] for a in selected["assessments"]],["a","b"])
        second = result([["A","10"],["B","20"],["C","30"]])
        selected = select_screen_candidates([{"id":"a","raw":first},{"id":"b","raw":second}])
        self.assertFalse(selected["automatic_delivery"])
        self.assertIn("candidate_structure_conflict",{c["code"] for c in selected["conflicts"]})

    def test_local_text_fix_requires_unique_geometry_and_keeps_provenance(self):
        raw = result([["姓名","部门"],["Alce","Sales"]])
        cell = raw["tables"][0]["cells"][2]
        cell.update(confidence=.3,geometry_origin="direct_detection")
        evidence = [{"text":"Alice","box":[5,32,85,54],"confidence":.99,"source":"independent-crop-ocr"}]
        untouched = select_screen_candidates([{"id":"a","result":raw}],text_evidence=evidence)
        self.assertEqual(untouched["modifications"],[])
        fixed = select_screen_candidates([{"id":"a","result":raw}],text_evidence=evidence,allow_text_fixes=True)
        self.assertEqual(fixed["selected_result"]["tables"][0]["cells"][2]["text"],"Alice")
        self.assertEqual(len(fixed["modifications"]),1)
        self.assertEqual(fixed["modifications"][0]["before"],"Alce")
        self.assertEqual(fixed["modifications"][0]["human_review"],"not_performed")
        # Even strong evidence never automatically rewrites a leading-zero ID.
        raw = result([["编号","部门"],["001","Sales"]])
        raw["tables"][0]["cells"][2].update(confidence=.1,geometry_origin="direct_detection")
        evidence[0]["text"] = "0001"
        fixed = select_screen_candidates([{"id":"a","result":raw}],text_evidence=evidence,allow_text_fixes=True)
        self.assertEqual(fixed["modifications"],[])
        self.assertTrue(fixed["review_required"])

    def test_neighbor_cell_evidence_and_absent_geometry_never_auto_fix(self):
        raw = result([["Name","Dept"],["Alce","Sales"]])
        raw["tables"][0]["cells"][2].update(confidence=.1,geometry_origin="direct_detection")
        evidence = [{"text":"Alice Sales","box":[5,32,185,54],"confidence":.99,"source":"ocr"}]
        selected = select_screen_candidates([{"id":"a","result":raw}],text_evidence=evidence,allow_text_fixes=True)
        self.assertEqual(selected["modifications"],[])
        self.assertFalse(selected["automatic_delivery"])
        for cell in raw["tables"][0]["cells"]:
            cell["box"] = None
        selected = select_screen_candidates([{"id":"a","result":raw}],text_evidence=evidence,allow_text_fixes=True)
        self.assertEqual(selected["modifications"],[])

    def test_unreadable_source_abstains_even_if_model_returns_valid_table(self):
        raw = result([["A","B"],["123","456"]])
        assessment = assess_screen_result(raw,{"quality":{"unreadable":True,"flags":[]}})
        self.assertFalse(assessment["automatic_delivery"])
        self.assertEqual(assessment["upgrade_kind"],"visual")
        self.assertIn("unreadable_source",self.codes(assessment))

    def test_actual_result_geometry_maps_back_without_inventing_empty_boxes(self):
        raw = result([["A","B"]])
        raw["tables"][0]["cells"][1]["box"] = None
        mapped = map_screen_result_to_source(raw,{"id":"crop","kind":"business_crop","prepared_to_source":[1,0,80,0,1,120,0,0,1]})
        self.assertEqual(mapped["tables"][0]["cells"][0]["box"],[80.,120.,180.,150.])
        self.assertIsNone(mapped["tables"][0]["cells"][1]["box"])
        self.assertEqual(raw["tables"][0]["cells"][0]["box"],[0,0,100,30])


if __name__ == "__main__":
    unittest.main()
