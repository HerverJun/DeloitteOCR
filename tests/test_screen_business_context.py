"""Protect spatial business context ownership and full auxiliary evidence."""

from copy import deepcopy
import unittest

from ocr_workbench.screen_evidence import organize_screen_business_metadata, normalize_screen_context


def fixture():
    region={'id':'business','table_box':[100,200,700,400],'box':[85,135,720,460],
            'sources':['layout_table'],'score':.95,'ancillary':[]}
    prep={'width':800,'height':600,'business_regions':[region],'excluded_regions':[]}
    raw={'tables':[{'rows':2,'columns':2,'cells':[{'row':0,'column':0,'text':'业务标题'},
        {'row':0,'column':1,'text':'金额'},{'row':1,'column':0,'text':'Save'},{'row':1,'column':1,'text':'00001'}]}]}
    evidence={'layout':[{'kind':'table','box':[100,200,700,400],'confidence':.95},
        {'kind':'title','box':[110,140,650,165],'confidence':.96},
        {'kind':'note','box':[110,430,690,455],'confidence':.9}],
        'text':[{'text':'业务标题','box':[120,142,500,162],'source':'PP-OCRv6-auxiliary'},
        {'text':'单位：万元','box':[490,175,670,195],'source':'PP-OCRv6-auxiliary'},
        {'text':'业务标题','box':[115,210,300,235],'source':'PP-OCRv6-auxiliary'},
        {'text':'00001','box':[510,340,670,365],'source':'PP-OCRv6-auxiliary'},
        {'text':'Save','box':[120,340,230,365],'source':'PP-OCRv6-auxiliary'},
        {'text':'表注：Excel 中的编号均为原文','box':[115,432,670,452],'source':'PP-OCRv6-auxiliary'}]}
    return raw,prep,evidence


class BusinessContextTests(unittest.TestCase):
    def test_independent_title_unit_note_survive_while_owned_grid_text_is_not_duplicated(self):
        raw,prep,evidence=fixture();before=deepcopy((raw,prep,evidence))
        organized=organize_screen_business_metadata(raw,prep,evidence)
        self.assertEqual([part['text'] for part in organized['items']],['业务标题','单位：万元','表注：Excel 中的编号均为原文'])
        self.assertEqual([part['kind'] for part in organized['items']],['title','unit','note'])
        self.assertEqual(organized['counts']['table_content'],3)
        self.assertEqual(organized['auxiliary_evidence'],evidence)
        self.assertEqual((raw,prep,evidence),before)
        self.assertTrue(all(part['human_confirmed'] is False for part in organized['items']))
        self.assertEqual(organized['counts']['observations'],len(evidence['text']))

    def test_same_literal_at_different_locations_is_preserved_and_same_observation_is_deduplicated(self):
        raw,prep,evidence=fixture()
        evidence['layout'].append({'kind':'title','text':'业务标题','box':[120,142,500,162],'confidence':.99})
        evidence['text'].append({'text':'业务标题','box':[120,465,500,485],'kind':'note'})
        evidence['layout'].append({'kind':'note','box':[120,465,500,485],'confidence':.95})
        organized=organize_screen_business_metadata(raw,prep,evidence)
        titles=[part for part in organized['items'] if part['text']=='业务标题']
        self.assertEqual(len(titles),2)
        self.assertEqual(len(titles[0]['evidence_refs']),2)
        self.assertEqual(organized['counts']['duplicate_observation'],1)

    def test_business_label_crossing_ui_box_is_retained_with_conflict(self):
        raw,prep,evidence=fixture()
        prep['excluded_regions']=[{'kind':'toolbar','box':[0,130,800,169]}]
        organized=organize_screen_business_metadata(raw,prep,evidence)
        title=next(part for part in organized['items'] if part['kind']=='title')
        self.assertTrue(title['review_required'])
        self.assertTrue(any(part.get('reason')=='conflicting_ui_and_business_geometry' for part in organized['decisions']))

    def test_multitable_shared_title_retained_once_without_guessed_table_index(self):
        raw,prep,evidence=fixture()
        prep['business_regions'][0]['table_box']=[100,200,400,400]
        prep['business_regions'].append({'id':'second','table_box':[420,200,700,400],'box':[410,135,720,460]})
        raw['tables'].append(deepcopy(raw['tables'][0]))
        organized=organize_screen_business_metadata(raw,prep,evidence)
        title=next(part for part in organized['items'] if part['kind']=='title')
        self.assertIsNone(title['table']);self.assertIsNone(title['region_id'])
        self.assertEqual(title['region_ids'],['business','second'])
        self.assertTrue(title['review_required'])

    def test_missing_geometry_does_not_drop_labeled_unit_or_raw_evidence(self):
        raw,prep,evidence=fixture()
        evidence['text'].extend([{'kind':'unit','text':'单位：元'}, {'text':'不确定文字','box':[1,1,1,2]}])
        organized=organize_screen_business_metadata(raw,prep,evidence)
        unit=next(part for part in organized['items'] if part['text']=='单位：元')
        self.assertIsNone(unit['box']);self.assertTrue(unit['review_required'])
        self.assertEqual(len(organized['decisions']),len(evidence['text']))
        self.assertEqual(organized['auxiliary_evidence'],evidence)

    def test_confirmed_excel_ruler_with_blank_rows_is_excluded_and_business_letters_are_protected(self):
        raw,prep,_=fixture()
        # Actual known failure: 18px letters sit 108px above the business grid.
        prep['business_regions'][0]['table_box']=[100,395,700,550]
        blocks=[{'text':'report.xlsx - Excel','box':[30,20,300,40]},
                {'text':'文件 开始 插入 页面布局 公式 数据 审阅 视图','box':[30,60,700,85]}]
        blocks += [{'text':letter,'box':[110+index*70,270,125+index*70,288]} for index,letter in enumerate('BCDEFGHһJK')]
        blocks += [{'text':'A B C D 为业务分组','box':[120,340,680,365]}]
        evidence=normalize_screen_context({'status':'success','image':{'width':800,'height':600},
            'layout_blocks':[{'kind':'table','box':[100,395,700,550],'confidence':.95},
                             {'kind':'caption','box':[110,335,690,370],'confidence':.95}],
            'ocr_blocks':blocks,'ocr_source':'PP-OCRv6-auxiliary'})
        organized=organize_screen_business_metadata(raw,prep,evidence)
        self.assertFalse(any(part['text'] in 'BCDEFGHһJK' for part in organized['items']))
        self.assertIn('A B C D 为业务分组',[part['text'] for part in organized['items']])
        self.assertEqual(organized['auxiliary_evidence'],evidence)


if __name__=='__main__':unittest.main()
