from copy import deepcopy
import unittest

from ocr_workbench.fusion import default_policy, fuse, validate_sources, choose, location
from ocr_workbench.fusion_alignment import canonical_edit, source_tables, table_content, text_alignment, text_source_alignment
from ocr_workbench.tables import parse_tables


def source(engine, text, *, status="succeeded", batch="batch-1", version="version-1", tables=None, blocks=None):
    return {"engine": engine, "result_id": engine + "-result", "task_id": engine + "-task",
            "image_id": "image-1", "version_id": version, "batch": batch, "status": status,
            **({"original": {"engine": engine, "text": text, "tables": parse_tables(text) if tables is None else tables,
                            "blocks": blocks or [], "image": {"width": 600, "height": 400},
                            "project_image_version": version}} if status == "succeeded" else {})}


def table(name="金额", value="00123", label="项目甲"):
    return f"<table><tr><td>项目</td><td>{name}</td></tr><tr><td>{label}</td><td>{value}</td></tr></table>"


def enabled(kind="table"):
    policy = default_policy(kind)
    policy.update(baseline="glm", automatic_replacement=True, minimum_support=2/3,
                  allow_numeric_replacement=True)
    return policy


class FusionTests(unittest.TestCase):
    def test_rejects_unknown_batches_versions_fused_and_duplicate_votes(self):
        a, b = source("glm", "a"), source("ppocr", "b", version="other")
        with self.assertRaisesRegex(ValueError, "同一图片"):
            validate_sources([a, b])
        b["version_id"] = "version-1"
        b["original"]["origin"] = "fusion"
        with self.assertRaisesRegex(ValueError, "原始单引擎"):
            validate_sources([a, b])
        self.assertEqual(len(validate_sources([a, deepcopy(a)])), 1)
        with self.assertRaisesRegex(ValueError, "不同原始"):
            validate_sources([a, source("glm", "retry")])
        a["batch"] = ""
        with self.assertRaisesRegex(ValueError, "缺少"):
            validate_sources([a])

    def test_failures_reduce_coverage_empty_strings_remain_valid_candidates(self):
        policy = enabled("print")
        evidence = choose("wrong", {"glm": "wrong", "ppocr": ""}, ["glm", "ppocr", "hunyuan"], policy)
        self.assertEqual(evidence["coverage"], 2/3)
        self.assertEqual(evidence["support_denominator"], 3)
        self.assertIn("", [c["value"] for c in evidence["candidates"]])
        self.assertEqual(evidence["selected"], "wrong")
        with self.assertRaisesRegex(ValueError, "没有可用"):
            fuse([source("glm", "", status="failed")], policy, "empty")

    def test_reordered_tables_match_identity_and_keep_literal_complete_amounts(self):
        a, b = table(value="1,260.00"), table("编号", "000098765432109876", "项目乙")
        corrected = table(value="1,280.00")
        output = fuse([source("glm", a + b), source("paddlevl", b + corrected),
                       source("hunyuan", corrected + b)], enabled(), "reorder")
        self.assertEqual(output["tables"][0]["cells"][-1]["text"], "1,280.00")
        self.assertEqual(output["tables"][1]["cells"][-1]["text"], "000098765432109876")
        self.assertEqual(output["fusion"]["units"][-1]["human_confirmed"], False)

    def test_duplicate_headers_and_same_shape_do_not_create_identity(self):
        a = table(value="10.00")
        output = fuse([source("glm", a+a), source("paddlevl", a+a)], enabled(), "duplicate")
        self.assertTrue(all(u["category"] == "structure" for u in output["fusion"]["units"]))
        self.assertTrue(any(u["target"]["kind"] == "unmatched_table" for u in output["fusion"]["units"]))
        unrelated = table("日期", "2026-09-12", "其他项目")
        output = fuse([source("glm", a), source("paddlevl", unrelated)], enabled(), "same-shape")
        self.assertEqual(output["tables"][0]["cells"][-1]["text"], "10.00")

    def test_missing_row_merge_conflict_and_shift_require_whole_table_review(self):
        a = table()
        for other in (a.replace("</table>", "<tr><td>乙</td><td>9</td></tr></table>"),
                      "<table><tr><td colspan='2'>项目 金额</td></tr><tr><td>项目甲</td><td>5</td></tr></table>"):
            output = fuse([source("glm", a), source("paddlevl", other)], enabled(), "struct")
            self.assertEqual(output["tables"][0]["cells"][-1]["text"], "00123")
            self.assertTrue(any(u["category"] == "structure" for u in output["fusion"]["units"]))

    def test_ppocr_does_not_vote_on_structure_or_fabricate_cells(self):
        output = fuse([source("glm", table()), source("ppocr", "项目 金额 项目甲 123")], enabled(), "pp")
        self.assertEqual(output["fusion"]["expected_sources"], ["glm"])
        self.assertTrue(all(u["source_states"]["ppocr"] == "unsupported" for u in output["fusion"]["units"]))
        self.assertTrue(all(c.get("polygon") is None for c in output["tables"][0]["cells"]))

    def test_numeric_protection_and_ties_keep_baseline(self):
        p = enabled()
        p["allow_numeric_replacement"] = False
        decision = choose("00123", {"glm": "00123", "ppocr": "123", "hunyuan": "123"}, list(("glm", "ppocr", "hunyuan")), p)
        self.assertEqual(decision["selected"], "00123")
        self.assertEqual(decision["reason"], "numeric_protection")
        p["allow_numeric_replacement"] = True
        decision = choose("-12.50", {"glm": "-12.50", "hunyuan": "12.50"}, ["glm", "hunyuan"], p)
        self.assertEqual(decision["selected"], "-12.50")
        self.assertEqual(decision["reason"], "tie")

    def test_text_lines_can_combine_original_candidates_without_inventing_text(self):
        p = enabled("print")
        base = "The cot sleeps.\nThe dog ploys.\n"
        output = fuse([source("glm", base), source("ppocr", "The cat sleeps.\nThe dog plays.\n"),
                       source("hunyuan", "The cat sleeps.\nThe dog plays.\n")], p, "text")
        # Multiple changed lines are conservatively a whole unresolved span.
        self.assertEqual(output["text"], base)
        one = fuse([source("glm", "The cot sleeps.\n"), source("ppocr", "The cat sleeps.\n"),
                    source("hunyuan", "The cat sleeps.\n")], p, "line")
        self.assertEqual(one["text"], "The cat sleeps.\n")

    def test_unique_insertions_deletions_and_long_spans_are_never_silently_dropped(self):
        p = enabled("print")
        output = fuse([source("glm", "原文\n"), source("ppocr", "原文\n新增独有行\n")], p, "insert")
        self.assertEqual(output["text"], "原文\n")
        self.assertTrue(any("新增独有行" in c["value"] for u in output["fusion"]["units"] for c in u["candidates"]))
        self.assertTrue(output["fusion"]["unresolved"])
        spans = text_alignment("甲"*25000, "乙"*25000, p["limits"])
        self.assertEqual(spans[0]["operation"], "resource_limit")
        self.assertFalse(spans[0]["reliable"])

    def test_location_uses_real_region_and_rejects_invalid_or_wrong_version_coordinates(self):
        raw = table()
        s = source("glm", raw, blocks=[{"kind": "table", "text": raw, "polygon": [[1,1],[500,1],[500,200],[1,200]]}])
        t = source_tables(s)[0]
        self.assertEqual(location(s, table=t)["level"], "region")
        self.assertEqual(location(s, cell={"polygon": [[-1,0],[2,0],[2,2]]})["level"], "image")
        s["original"]["project_image_version"] = "other"
        self.assertEqual(location(s, table=t)["level"], "image")

    def test_canonical_edit_regenerates_text_offsets_and_hash_after_cell_edit(self):
        raw = "前文\n" + table() + "\n后文"
        edit = {"text": raw, "tables": parse_tables(raw)}
        old = edit["tables"][0]["source"]["sha256"]
        edit["tables"][0]["cells"][-1]["text"] = "000098765432109876"
        saved = canonical_edit(edit)
        self.assertIn("000098765432109876", saved["text"])
        self.assertNotIn("00123", saved["text"])
        self.assertNotEqual(saved["tables"][0]["source"]["sha256"], old)
        self.assertEqual(saved, canonical_edit(saved))
        self.assertTrue(saved["text"].startswith("前文"))
        self.assertTrue(saved["text"].endswith("后文"))

    def test_cancellation_interrupts_cpu_fusion(self):
        from ocr_workbench.adapter import Cancelled
        with self.assertRaises(Cancelled):
            fuse([source("glm", table())], enabled(), "cancel", cancelled=lambda: True)

    def test_sparse_baseline_preserves_raw_text_without_inventing_cells(self):
        sparse = "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>"
        output = fuse([source("glm", sparse), source("hunyuan", table())], enabled(), "sparse")
        self.assertEqual(output["text"], sparse)
        self.assertEqual(output["tables"], [])
        self.assertEqual(output["fusion"]["units"][0]["reason"], "baseline_structure_invalid")
        self.assertFalse(any(u["automatic"] for u in output["fusion"]["units"]))

    def test_missing_table_requires_explicit_placement_and_preserves_other_tables(self):
        from ocr_workbench.review_issues import apply_choice
        raw = "前文\n"+table()+"\n后文"
        edit = canonical_edit({"text": raw, "tables": parse_tables(raw)})
        candidate = parse_tables(table("日期", "2026-09-12", "项目乙"))[0]
        target = {"kind": "unmatched_table", "source": "hunyuan", "source_table": 1}
        with self.assertRaisesRegex(ValueError, "明确选择"):
            apply_choice(edit, target, candidate)
        for position in ("start", "end"):
            saved = apply_choice(edit, target, candidate, position)
            self.assertEqual(len(saved["tables"]), 2)
            self.assertEqual(saved["text"].count("2026-09-12"), 1)
            self.assertEqual(saved["text"].count("00123"), 1)
            self.assertEqual(saved, canonical_edit(saved))

    def test_incomplete_structure_uses_one_identity_matched_complete_skeleton(self):
        text=table(value='00123')
        incomplete=parse_tables(text);incomplete[0]['rows']+=1
        output=fuse([source('glm',text,tables=incomplete),source('hunyuan',text)],enabled(),'skeleton')
        self.assertEqual(output['tables'][0]['rows'],2)
        self.assertEqual(output['text'].count('00123'),1)
        self.assertEqual(output['fusion']['structure_fallbacks'],[{'table':0,'engine':'hunyuan'}])
        self.assertTrue(all(u['needs_review'] for u in output['fusion']['units']))

    def test_fragment_evidence_and_reading_order_remain_explicit(self):
        p = enabled("print")
        result = fuse([source("glm", "The cot sleeps soundly.\n"), source("ppocr", "The cat sleeps soundly.\n"), source("hunyuan", "The cat sleeps soundly.\n")], p, "fragment")
        self.assertEqual(result["text"], "The cat sleeps soundly.\n")
        self.assertEqual(result["fusion"]["units"][0]["baseline"], "o")
        spans = text_alignment("甲\n乙\n丙\n", "乙\n甲\n丙\n", p["limits"])
        self.assertTrue(any(s.get("reading_order_changed") for s in spans))
        self.assertFalse(any(s["reliable"] for s in spans))

    def test_distinct_long_body_anchors_match_a_table_but_never_imply_cell_alignment(self):
        raw='<table>'+''.join(f'<tr><td>{n}</td><td>Distinct original paragraph number {n}</td></tr>' for n in range(4))+'</table>'
        shifted='<table><tr><td colspan="2">Title only in one source</td></tr>'+raw.removeprefix('<table>')
        result=fuse([source('glm',shifted),source('hunyuan',raw)],enabled(),'body-anchors')
        self.assertEqual(len(result['tables']),1)
        self.assertTrue(any(u['target']['kind']=='table' for u in result['fusion']['units']))
        self.assertFalse(any(u['target']['kind']=='cell' for u in result['fusion']['units']))

    def test_real_text_regions_preserve_offsets_and_whole_numeric_candidates(self):
        boxes = [[[10,10],[200,10],[200,50],[10,50]], [[10,70],[200,70],[200,110],[10,110]]]
        left = source('glm', '前文\nThe cot sleeps soundly.\n00123\n后文', blocks=[
            {'text':'The cot sleeps soundly.','polygon':boxes[0]}, {'text':'00123','polygon':boxes[1]}])
        right = source('ppocr', '前文\nThe cat sleeps soundly.\n00124\n后文', blocks=[
            {'text':'The cat sleeps soundly.','polygon':boxes[0]}, {'text':'00124','polygon':boxes[1]}])
        spans = text_source_alignment(left, right, enabled('print')['limits'])
        changed = [s for s in spans if s['operation'] != 'equal']
        self.assertEqual([s['value'] for s in changed], ['a', '00124'])
        self.assertTrue(all(s['region_alignment']['kind']=='real_region' for s in changed))
        for span in spans:
            self.assertEqual(right['original']['text'][span['other_start']:span['other_end']], span['value'])
        self.assertEqual(left['original']['text'][changed[-1]['start']:changed[-1]['end']], '00123')
        third=deepcopy(right);third['engine']='hunyuan';third['original']['engine']='hunyuan'
        output=fuse([left,right,third],enabled('print'),'region-fragment-location')
        self.assertTrue(all(u['location']['level']=='region' for u in output['fusion']['units']))
        self.assertEqual(output['fusion']['units'][0]['location']['polygon'],boxes[0])

    def test_reordered_or_unmatched_regions_never_write_across_regions(self):
        boxes = [[[10,10],[200,10],[200,50],[10,50]], [[10,70],[200,70],[200,110],[10,110]]]
        base = source('glm','Northern block\nSouthern block',blocks=[
            {'text':'Northern block','polygon':boxes[0]}, {'text':'Southern block','polygon':boxes[1]}])
        reordered = source('ppocr','Southern block\nNorthern block',blocks=[
            {'text':'Southern block','polygon':boxes[1]}, {'text':'Northern block','polygon':boxes[0]}])
        spans = text_source_alignment(base,reordered,enabled('print')['limits'])
        self.assertEqual(spans[0]['operation'],'region_order_changed')
        self.assertFalse(spans[0]['reliable'])
        extra = deepcopy(reordered); extra['engine']='hunyuan';extra['original']['engine']='hunyuan'
        output=fuse([base,reordered,extra],enabled('print'),'region-reorder')
        self.assertEqual(output['text'],base['original']['text'])
        self.assertTrue(output['fusion']['units'][0]['needs_review'])
        reordered['original']['blocks'].pop()
        self.assertEqual(text_source_alignment(base,reordered,enabled('print')['limits'])[0]['operation'],'region_unmatched')

    def test_repeated_region_text_and_duplicate_boxes_are_explicitly_ambiguous(self):
        box=[[10,10],[200,10],[200,50],[10,50]]
        a=source('glm','Repeat\nRepeat',blocks=[{'text':'Repeat','polygon':box}])
        b=source('ppocr','Repeat\nChanged',blocks=[{'text':'Repeat','polygon':box}])
        self.assertEqual(text_source_alignment(a,b,enabled('print')['limits'])[0]['operation'],'region_ambiguous')
        a=source('glm','North\nSouth',blocks=[{'text':'North','polygon':box},{'text':'South','polygon':box}])
        b=source('ppocr','North\nSouth',blocks=deepcopy(a['original']['blocks']))
        self.assertEqual(text_source_alignment(a,b,enabled('print')['limits'])[0]['operation'],'region_unmatched')

    def test_whole_table_preserves_actual_location_source_without_inheriting_scores(self):
        from ocr_workbench.review_issues import apply_choice
        raw=table(); incomplete=parse_tables(raw);incomplete[0]['rows']+=1
        cells=parse_tables(raw)
        polygon=[[10,10],[200,10],[200,110],[10,110]]
        for cell in cells[0]['cells']:
            cell.update(confidence=.91,polygon=polygon)
        candidate=source('hunyuan',raw,tables=cells,blocks=[{'kind':'table','text':raw,'polygon':polygon}])
        output=fuse([source('glm',raw,tables=incomplete),candidate],enabled(),'source-location')
        issue=output['fusion']['units'][0]
        self.assertEqual(issue['location']['source_result_id'],'hunyuan-result')
        self.assertEqual(issue['location']['level'],'region')
        self.assertTrue(all(c['confidence'] is None and c['polygon'] is None for c in output['tables'][0]['cells']))
        self.assertEqual(candidate['original']['tables'][0]['cells'][0]['confidence'],.91)
        saved=apply_choice({'text':output['text'],'tables':output['tables']},issue['target'],cells[0])
        self.assertTrue(all(c['confidence'] is None and c['polygon'] is None for c in saved['tables'][0]['cells']))
        mapped=fuse([source('glm',raw),candidate],enabled(),'mapped-source-location')
        self.assertTrue(all(u['location']['level']=='cell' and u['location']['source_result_id']=='hunyuan-result'
                            for u in mapped['fusion']['units']))


if __name__ == "__main__":
    unittest.main()
