from copy import deepcopy
import unittest

from ocr_workbench.coordinates import box_polygon
from ocr_workbench.geometry_contract import full_cell_evidence, matching_text, polygon_iou, tokens_from_blocks
from ocr_workbench.geometry_providers import adapt_prediction
from ocr_workbench.table_matching import default_policy, local_mapping
from ocr_workbench.tables import parse_tables


def html(rows):
    return '<table>'+''.join('<tr>'+''.join('<td>'+s+'</td>' for s in row)+'</tr>' for row in rows)+'</table>'


def prediction(markup,*,header_offset=0):
    table=parse_tables(markup)[0];boxes=[];blocks=[]
    for i,c in enumerate(table['cells']):
        x,y=c['column']*100,(c['row']+header_offset)*40
        box=[x,y,x+c['column_span']*100,y+c['row_span']*40];boxes.append(box)
        if c['text']:
            blocks.append({'id':f'word:{i}','text':c['text'],'polygon':box_polygon([x+5,y+10,box[2]-5,y+30]),'granularity':'word'})
    return {'status':'success','image_version':'image:1','ocr_source':'frozen:1','ocr_blocks':blocks,'model_revisions':{'det':'v1'},
        'tables':[{'table_box':[0,0,400,400],'raw':{'det':{'boxes':[{'coordinate':b,'score':.99} for b in boxes]}},
        'final':{'pred_html':markup,'cell_box_list':boxes}}]}


def mapped(markup,p,**kwargs):
    return [m for m in local_mapping({'tables':parse_tables(markup)},p,400,400,**kwargs) if m['target']['kind']=='cell']


class LocalMatchingTests(unittest.TestCase):
    def test_extra_header_and_missing_row_do_not_shift_independent_body(self):
        adopted=html([['Item','Value'],['Alpha','10'],['Beta','20'],['Gamma','30']])
        predicted=html([['Extra','Header'],['Item','Value'],['Alpha','10'],['Gamma','30']])
        m=mapped(adopted,prediction(predicted));by={(c['target']['row'],c['target']['column']):c for c in m}
        self.assertEqual(by[1,1]['polygon'],box_polygon([100,80,200,120]))
        self.assertEqual(by[3,1]['polygon'],box_polygon([100,120,200,160]))
        self.assertNotEqual(by[2,1]['level'],'cell')

    def test_bad_merged_header_preserves_body(self):
        a='<table><tr><td colspan="2">Heading</td></tr><tr><td>Alpha</td><td>10</td></tr><tr><td>Beta</td><td>20</td></tr></table>'
        p=prediction(html([['Heading','Extra'],['Alpha','10'],['Beta','20']]))
        m=mapped(a,p)
        self.assertEqual([v['level'] for v in m[1:]],['cell']*4)

    def test_repeated_amounts_need_two_axis_anchors(self):
        a=html([['Item','Value'],['Alpha','0'],['Beta','0']])
        m=mapped(a,prediction(a));self.assertTrue(all(v['level']=='cell' for v in m))
        zero=html([['0','0'],['0','0']])
        self.assertTrue(all(v['level']!='cell' for v in mapped(zero,prediction(zero))))

    def test_empty_cell_uses_real_candidate_and_two_anchors(self):
        a=html([['Item','Value'],['Alpha',''],['Beta','2']])
        m=mapped(a,prediction(a));self.assertEqual(m[3]['level'],'cell')
        self.assertEqual(m[3]['content_polygons'],[])
        blank=html([['',''],['','']]);self.assertTrue(all(v['level']!='cell' for v in mapped(blank,prediction(blank))))

    def test_one_to_many_requires_contiguous_real_member_geometry(self):
        a='<table><tr><td>X</td><td>Y</td></tr><tr><td colspan="2">AB</td></tr><tr><td>R</td><td>S</td></tr></table>'
        p=prediction(html([['X','Y'],['A','B'],['R','S']]))
        m=mapped(a,p)[2];self.assertEqual(m['level'],'cell');self.assertEqual(m['geometry_origin'],'derived_from_cells')
        self.assertEqual(len(m['cell_polygons']),2);self.assertEqual(m['correspondence']['relation'],'one_to_many')
        p['tables'][0]['final']['cell_box_list'][3][0]+=3
        p['tables'][0]['raw']['det']['boxes'][3]['coordinate'][0]+=0 # shared test fixture list
        self.assertNotEqual(mapped(a,p)[2]['level'],'cell')

    def test_many_to_one_never_copies_parent_box_to_children(self):
        a=html([['X','Y'],['A','B'],['R','S']])
        merged='<table><tr><td>X</td><td>Y</td></tr><tr><td colspan="2">AB</td></tr><tr><td>R</td><td>S</td></tr></table>'
        p=prediction(merged);p['ocr_blocks']=[b for b in p['ocr_blocks'] if b['text']!='AB']+[
            {'id':'sub-A','text':'A','polygon':box_polygon([5,50,60,70])},
            {'id':'sub-B','text':'B','polygon':box_polygon([110,50,150,70])}]
        m=mapped(a,p)
        self.assertEqual([v['range_semantics'] for v in m[2:4]],['text_extent','text_extent'])
        self.assertFalse(any(full_cell_evidence(v) for v in m[2:4]))

    def test_line_crossing_two_cells_is_not_split(self):
        a=html([['X','Y'],['A','B']]);p=prediction(a)
        p['ocr_blocks']=p['ocr_blocks'][:2]+[{'id':'line','text':'A B','polygon':box_polygon([5,45,195,75]),'granularity':'line'}]
        self.assertTrue(all(v['level']!='cell' for v in mapped(a,p)[2:]))

    def test_short_word_in_large_cell_uses_token_containment(self):
        a=html([['A','B'],['C','D']]);p=prediction(a)
        p['ocr_blocks'][0]['polygon']=box_polygon([5,10,10,15])
        m=mapped(a,p)[0];self.assertEqual(m['level'],'cell')
        self.assertNotEqual(m['content_polygons'][0],m['cell_polygons'][0])

    def test_normalization_does_not_change_numeric_identity(self):
        for a,b in [('001','1'),('-10','10'),('1.00','100'),('10%','10'),('O','0'),('I','1'),('$1','1')]:
            self.assertNotEqual(matching_text(a),matching_text(b))

    def test_similar_tables_are_ambiguous_and_order_independent(self):
        a=html([['A','B'],['C','D']]);p=prediction(a);p['tables']*=2
        self.assertTrue(all(v['level']!='cell' for v in mapped(a,p)))
        p=prediction(a);before=mapped(a,p);p['ocr_blocks'].reverse();p['tables'][0]['raw']['det']['boxes'].reverse()
        self.assertEqual([(m['target'],m['polygon'],m['level']) for m in before],[(m['target'],m['polygon'],m['level']) for m in mapped(a,p)])

    def test_version_mismatch_and_budget_failure_keep_all_targets(self):
        a=html([['A','B'],['C','D']]);p=prediction(a)
        m=mapped(a,p,image_version='image:2');self.assertEqual(len(m),4)
        self.assertTrue(all(v['reason']=='coordinate_version_mismatch' and v['polygon'] is None for v in m))
        policy=default_policy();policy['maximum_token_cell_pairs']=1
        self.assertTrue(all(v['level']!='cell' for v in mapped(a,p,policy=policy)))

    def test_text_extent_cannot_claim_full_cell_export_capability(self):
        a=html([['A','B'],['C','D']]);p=prediction(a);p['tables'][0]['raw']['det']['boxes']=[]
        m=mapped(a,p)[0];self.assertEqual(m['range_semantics'],'text_extent');self.assertFalse(full_cell_evidence(m))
        m.update(level='cell');m['capabilities']['full_cell']=True
        self.assertFalse(full_cell_evidence(m))

    def test_polygon_iou_does_not_use_bounding_rectangles(self):
        square=box_polygon([0,0,10,10]);diamond=[[5,0],[10,5],[5,10],[0,5]]
        self.assertAlmostEqual(polygon_iou(square,diamond),.5)

    def test_rapid_span_mismatch_is_explicit_not_compressed(self):
        a=html([['A','B'],['C','D']]);p=prediction(a)
        rapid={'html':a,'cell_bboxes':[[v for pt in box_polygon(b) for v in pt] for b in p['tables'][0]['final']['cell_box_list']],
            'logic_points':[[0,0,0,0],[0,0,1,1],[1,1,0,0],[1,1,2,2]]}
        table=adapt_prediction(rapid,400,400)[0]
        self.assertIn('span_conflict',table['reason_codes']);self.assertEqual(len(table['cells']),3)

    def test_broken_other_table_does_not_discard_valid_region(self):
        a=html([['A','B'],['C','D']]);p=prediction(a)
        p['tables'].append({'table_box':None,'final':{'pred_html':'broken','cell_box_list':[]}})
        self.assertTrue(all(c['level']=='cell' for c in mapped(a,p)))


if __name__=='__main__':unittest.main()
