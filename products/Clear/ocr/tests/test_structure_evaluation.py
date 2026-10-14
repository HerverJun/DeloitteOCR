import importlib.util
from pathlib import Path
import sys
import unittest
from copy import deepcopy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from structure_eval import official_grits, score_page, paired_interval, validate_annotations, error_set


def table():
    return {'rows':2,'columns':2,'polygon':[[0,0],[100,0],[100,80],[0,80]],'cells':[
        {'row':r,'column':c,'row_span':1,'column_span':1,'text':[['Code','Amount'],['00123','-0.10']][r][c],
         'full_grid_box':[c*50,r*40,(c+1)*50,(r+1)*40]} for r in range(2) for c in range(2)]}


class EvaluationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('fitz'),'isolated GriTS scoring runtime required')
    def test_official_metrics_crosscheck_identical_text_and_span_perturbations(self):
        reference=table()
        identical=official_grits(reference,reference)
        self.assertEqual(identical,{'topology':1.,'content':1.,'location':1.})
        changed=deepcopy(reference);changed['cells'][2]['text']='123'
        scored=official_grits(reference,changed)
        self.assertEqual(scored['topology'],1.)
        self.assertLess(scored['content'],1.)
        changed=deepcopy(reference);changed['cells'][0]['column_span']=2;changed['cells'].pop(1)
        self.assertLess(official_grits(reference,changed)['topology'],1.)

    def test_failed_and_missing_pages_keep_all_target_cells(self):
        page={'id':'p1','document_id':'doc','tables':[table(),table()]}
        result=score_page(page,{'status':'failed','tables':[],'rejection_reasons':['timeout']})
        self.assertEqual(result['reference_cells'],8)
        self.assertEqual(result['reference_tables'],2)
        self.assertEqual(result['usable_tables'],0)
        self.assertEqual(len(result['ledger']),8)
        self.assertTrue(all(c['primary_failure']=='engineering' for c in result['ledger']))

    def test_extra_output_on_no_table_page_is_not_deliverable(self):
        result=score_page({'id':'p','document_id':'d','tables':[]},{'status':'success','tables':[table()]})
        self.assertEqual(result['extra_tables'],1)
        self.assertFalse(result['document_page_deliverable'])

    def test_unreviewed_or_text_extent_annotations_fail_closed(self):
        page={'id':'p','tables':[table()]}
        with self.assertRaises(ValueError):validate_annotations({'pages':[page]})
        page['annotation_review']={'reviewer_ids':['human-a','human-b'],'image_verified':True,'unresolved_ambiguities':0}
        page['tables'][0]['geometry_semantics']='text_extent'
        with self.assertRaises(ValueError):validate_annotations({'pages':[page]})
        page['tables'][0]['geometry_semantics']='full_grid'
        self.assertTrue(validate_annotations({'pages':[page]}))

    def test_paired_intervals_resample_groups_not_cells(self):
        baseline=[{'id':str(i),'group_id':'a' if i<50 else 'b','reference_tables':1,'usable_tables':0} for i in range(51)]
        candidate=[{**p,'usable_tables':1} for p in baseline]
        report=paired_interval(baseline,candidate)
        self.assertEqual(report['groups'],2)
        self.assertEqual(report['ci95'],[1.,1.])

    def test_suggestion_improvement_rejects_new_numeric_damage(self):
        reference=table();before=deepcopy(reference);before['cells'].pop()
        corrected=deepcopy(reference)
        self.assertLess(error_set(reference,corrected),error_set(reference,before))
        corrected['cells'][2]['text']='123'
        self.assertFalse(error_set(reference,corrected) < error_set(reference,before))
