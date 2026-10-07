"""Context normalization and residency are verified without loading any GPU."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from ocr_workbench.screen_evidence import (normalize_screen_context, collect_screen_context, merge_local_text_evidence,
    build_screen_visual_snapshot, review_screen_visual_proposals)
from ocr_workbench.table_geometry_worker import GeometryContextSession


def context(blocks=None, layout=None):
    return {"status":"success","component":"shared-geometry-context","image":{"width":800,"height":600},
            "layout_blocks":layout or [{"label":"table","box":[100,100,600,400],"confidence":.9}],
            "ocr_blocks":blocks or [],"ocr_source":"PP-OCRv6-auxiliary"}


class ScreenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root/"input.png"
        Image.new("RGB",(800,600),"white").save(self.image)

    def test_context_loads_layout_and_text_once_for_multiple_images(self):
        layout_model = MagicMock(return_value=[{"boxes":[{"label":"table","coordinate":[100,100,600,400],"score":.9},
                                                           {"label":"doc_title","coordinate":[120,55,550,80],"score":.85},
                                                           {"label":"footnote","coordinate":[120,430,550,450],"score":.8}]}])
        prediction = SimpleNamespace(json={"rec_texts":["0001"],"rec_scores":[.99],"rec_polys":[[[120,120],[170,120],[170,140],[120,140]]]})
        pp_model = MagicMock()
        pp_model.predict.return_value = [prediction]
        creator = MagicMock(return_value=layout_model)
        with patch.dict("sys.modules",{"paddlex":SimpleNamespace(create_model=creator)}), patch("ocr_workbench.worker.load_paddle",return_value=(pp_model,.01)) as loader:
            session = GeometryContextSession(self.root)
            first = session.predict(self.image,{})
            second = session.predict(self.image,{})
        self.assertEqual(creator.call_count,1)
        self.assertEqual(loader.call_count,1)
        self.assertEqual(pp_model.predict.call_count,2)
        self.assertIs(session.ocr,pp_model)
        self.assertEqual(len(first["layout_blocks"]),3)
        self.assertEqual(first["layout_blocks"][1]["label"],"doc_title")
        self.assertEqual(second["load_seconds"],0)
        self.assertGreater(first["load_seconds"],0)
        self.assertEqual(second["image"],{"width":800,"height":600})
        self.assertEqual(second["ocr_blocks"][0]["text"],"0001")
        self.assertFalse(second["contributes_to_votes"])

    def test_frozen_context_blocks_do_not_silently_run_auxiliary_ocr(self):
        session = GeometryContextSession(self.root)
        with patch("ocr_workbench.worker.load_paddle") as loader:
            with self.assertRaises(ValueError):
                session.predict(self.image,{"regions":[{"box":[100,100,600,400]}],"allow_auxiliary_ocr":False})
            loader.assert_not_called()

    def test_titles_notes_and_real_sources_normalize_without_truth(self):
        raw = context([{ "text":"金额","box":[105,105,190,130],"confidence":.96}],
                      [{"label":"table","box":[100,100,600,400],"score":.93},
                       {"label":"doc_title","box":[110,40,560,70],"score":.88},
                       {"label":"footnote","box":[110,430,560,450],"score":.77}])
        evidence = normalize_screen_context(raw)
        self.assertEqual([item["kind"] for item in evidence["layout"]],["table","title","note"])
        self.assertEqual(evidence["layout"][0]["confidence"],.93)
        self.assertEqual(evidence["text"][0]["text"],"金额")
        self.assertEqual(evidence["text"][0]["source"],"PP-OCRv6-auxiliary")
        self.assertFalse(evidence["provenance"]["contains_reference_input"])

    def test_sequential_ruler_requires_outside_table_and_regular_spatial_order(self):
        blocks = [{"text":letter,"box":[120+n*100,75,135+n*100,90]} for n,letter in enumerate("ABCD")]
        blocks += [{"text":str(n+1),"box":[105,130+n*45,125,145+n*45]} for n in range(4)]
        blocks += [{"text":"Save","box":[150,330,240,350]}]
        evidence = normalize_screen_context(context(blocks))
        self.assertEqual([item["kind"] for item in evidence["text"][:4]],["ruler"]*4)
        self.assertTrue(all(item["kind"]=="text" for item in evidence["text"][4:]))
        self.assertEqual(evidence["text"][-1]["text"],"Save")
        self.assertEqual(sum(item["kind"]=="ruler" for item in evidence["layout"]),1)
        blocks[2]["text"] = "Sales"
        evidence = normalize_screen_context(context(blocks))
        self.assertFalse(any(item["kind"]=="ruler" for item in evidence["layout"]))

    def test_crop_context_inverse_mapping_and_invalid_boxes_are_explicit(self):
        raw = context([{"text":"17","box":[5,5,25,20]},{"text":"bad","box":[900,0,920,20]}],
                      [{"label":"table","box":[0,0,100,80]}])
        evidence = normalize_screen_context(raw,width=800,height=600,candidate={"id":"crop","prepared_to_source":[1,0,100,0,1,200,0,0,1]})
        self.assertEqual(evidence["text"][0]["box"],[105.,205.,125.,220.])
        self.assertEqual(len(evidence["text"]),1)
        self.assertEqual(evidence["warnings"][0]["code"],"invalid_or_outside_context_box")

    def test_application_chrome_needs_multiple_anchors_and_preserves_business_keywords(self):
        blocks=[{'text':'report.xlsx - Excel','box':[30,20,260,40]},
                {'text':'文件 开始 插入 页面布局 公式 数据 审阅 视图','box':[30,60,650,85]},
                {'text':'fx =SUM(C2:C11)','box':[30,120,400,140]},
                {'text':'文件开始插入页面布局公式数据','box':[120,200,420,225]},
                {'text':'Save','box':[120,240,220,265]},
                {'text':'状态：就绪 合计300.00','box':[30,500,450,525]}]
        evidence=normalize_screen_context(context(blocks,[{'label':'table','box':[100,180,600,400],'confidence':.95}]))
        self.assertTrue(all(item['kind']=='toolbar' for item in evidence['text'][:3]))
        self.assertEqual(evidence['text'][3]['kind'],'text')
        self.assertEqual(evidence['text'][4]['text'],'Save')
        self.assertEqual(evidence['text'][5]['kind'],'taskbar')
        self.assertTrue(all(item['kind']=='text' for item in normalize_screen_context(context([blocks[4]]))['text']))

    def test_regular_external_ruler_allows_one_ocr_gap_without_erasing_business_numbers(self):
        blocks=[{'text':letter,'box':[100+n*80,75,115+n*80,90]} for n,letter in enumerate('AB一DEF')]
        blocks += [{'text':str(n+1),'box':[120,130+n*45,140,145+n*45]} for n in range(4)]
        evidence=normalize_screen_context(context(blocks))
        self.assertEqual(sum(item['kind']=='ruler' for item in evidence['text'][:6]),5)
        self.assertTrue(all(item['kind']=='text' for item in evidence['text'][6:]))

    def test_trusted_business_caption_containing_application_name_is_preserved(self):
        blocks=[{'text':'file.xlsx - Excel','box':[30,20,250,40]},
                {'text':'文件开始插入页面布局公式数据审阅视图','box':[30,60,650,80]},
                {'text':'业务底稿.xlsx - Excel','box':[120,120,500,145]}]
        layout=[{'label':'table','box':[100,200,600,450],'confidence':.95},
                {'label':'table_title','box':[110,115,550,150],'confidence':.95}]
        evidence=normalize_screen_context(context(blocks,layout))
        self.assertEqual(evidence['text'][-1]['kind'],'text')
        self.assertEqual(evidence['text'][-1]['text'],'业务底稿.xlsx - Excel')

    def test_local_tiles_preserve_conflicts_and_distinct_repeated_values(self):
        a = {"candidate":{"id":"a","kind":"tile","prepared_to_source":[1,0,0,0,1,0,0,0,1]},
             "context":context([{"text":"0.10","box":[110,100,150,125]},{"text":"0.10","box":[210,100,250,125]}])}
        b = {"candidate":{"id":"b","kind":"tile","prepared_to_source":[1,0,100,0,1,0,0,0,1]},
             "context":context([{"text":"0.10","box":[10,100,50,125]},{"text":"0.70","box":[110,100,150,125]}])}
        merged = merge_local_text_evidence([a,b],width=800,height=600)
        self.assertEqual(len(merged["blocks"]),3)
        self.assertEqual(len(merged["conflicts"]),1)
        self.assertEqual(merged["blocks"][0]["tile_sources"],["a","b"])
        self.assertFalse(merged["can_replace_whole_table"])
        limited = merge_local_text_evidence([a,b],width=800,height=600,max_tokens=1)
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["warnings"][-1]["code"],"local_token_budget")

    def test_collect_uses_caller_owned_context_adapter_and_normalized_pixels(self):
        adapter = SimpleNamespace(engine="geometry",geometry_provider="context",recognize=MagicMock(return_value=context()))
        result = collect_screen_context(self.image,self.root/"context",adapter,prepare=False)
        self.assertIsNone(result["preparation"])
        adapter.recognize.assert_called_once()
        self.assertTrue((self.root/"context/screen-context-evidence.json").is_file())
        self.assertEqual(adapter.recognize.call_args.kwargs["geometry_request"]["include_all_layout"],True)

    def test_visual_reader_gets_no_suggested_answer_and_only_reliable_cells(self):
        cell = {"row":0,"column":0,"text":"-10.50","box":[100,100,200,140],"geometry_origin":"direct_detection"}
        raw = {"tables":[{"cells":[cell,{"row":0,"column":1,"text":"0001","box":[200,100,300,140]}]}]}
        issues = [{"table":0,"row":0,"column":0,"code":"numeric_conflict"},{"table":0,"row":0,"column":1,"code":"id_conflict"}]
        snapshot = build_screen_visual_snapshot(raw,{"width":800,"height":600},issues,self.image)
        self.assertEqual(len(snapshot["targets"]),1)
        self.assertEqual(snapshot["targets"][0]["before"],"")
        self.assertNotIn("-10.50",snapshot["targets"][0]["context"])
        self.assertEqual(snapshot["source_targets"][0]["before"],"-10.50")
        self.assertEqual(snapshot["skipped"][0]["reason"],"no_reliable_local_cell_geometry")
        reviewer = SimpleNamespace(review=MagicMock(return_value={"items":[{"target_id":"screen:0:0:0","decision":"replace","after":"-10.50","reason":"读取原图"}]}))
        reviewed = review_screen_visual_proposals(reviewer,self.image,snapshot)
        self.assertEqual(reviewed["proposals"][0]["decision"],"keep")
        self.assertFalse(reviewed["proposals"][0]["automatic_apply"])
        self.assertEqual(reviewed["human_review"],"not_performed")


if __name__ == "__main__":
    unittest.main()
