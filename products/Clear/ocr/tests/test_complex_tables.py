from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import random
import time
import unittest

from test_structure_workflow import prediction, table
import test_structure_workflow as workflow
from ocr_workbench.complex_table_contract import validate_grid, validate_annotation
from ocr_workbench.financial_checks import parse_amount, check_tables
from ocr_workbench.geometry_contract import tokens_from_blocks, as_json
from ocr_workbench.geometry_providers import adapt_prediction
from ocr_workbench.table_matching import assign_tokens, policy_for_algorithm
from ocr_workbench.structure_arbitration import VERSION, validate_response
from ocr_workbench.multimodal_store import enqueue_review, prepare_review, complete_review
from ocr_workbench.store import Conflict


class SpatialTests(unittest.TestCase):
    def candidates(self, values):
        p,b=prediction(values)
        width,height=len(values[0])*100,len(values)*40
        tokens,_=tokens_from_blocks(b,source_result='ocr',image_version='v',width=width,height=height)
        return tokens,adapt_prediction(p,width,height)[0]['cells']

    def test_index_equivalent_and_permutation_invariant(self):
        tokens,cells=self.candidates([['A','A','B'],['C','D','D']])
        p=policy_for_algorithm('local-v3')
        baseline=assign_tokens(tokens,cells,p)
        p['spatial_index']=True
        for seed in range(10):
            a,b=list(tokens),list(cells);rng=random.Random(seed);rng.shuffle(a);rng.shuffle(b)
            got=assign_tokens(a,b,p)
            self.assertEqual([as_json(r) for r in got[0]],[as_json(r) for r in baseline[0]])
            self.assertEqual(got[2],None)

    def test_overlapping_cells_remain_ambiguous(self):
        from dataclasses import replace
        tokens,cells=self.candidates([['A','B']])
        cells.append(replace(cells[0],id='overlapping-cell'))
        p={**policy_for_algorithm('local-v3'),'spatial_index':True}
        records,_,error=assign_tokens(tokens,cells,p)
        self.assertIsNone(error)
        self.assertIsNone(records[0].adopted_cell_id)

    def test_large_index_and_expired_budget(self):
        tokens,cells=self.candidates([[str(r*20+c) for c in range(20)] for r in range(50)])
        p=policy_for_algorithm('local-v3')
        self.assertEqual(assign_tokens(tokens,cells,p)[2],'candidate_budget_exceeded')
        p['spatial_index']=True
        records,_,error=assign_tokens(tokens,cells,p,deadline=time.perf_counter()+2)
        self.assertIsNone(error)
        self.assertEqual(sum(r.adopted_cell_id is not None for r in records),1000)
        self.assertEqual(assign_tokens(tokens,cells,p,deadline=time.perf_counter()-1),([],{},'timeout'))


class AnnotationTests(unittest.TestCase):
    def fixture(self):
        from ocr_workbench.coordinates import box_polygon
        t=table([['金额','1.00']]);t.update(image_version='v',image_sha256='h',polygon=box_polygon([0,0,200,40]),page_to_crop=[[1,0,0],[0,1,0],[0,0,1]])
        for c in t['cells']:
            c.update(id=str(c['column']),text_state='sourced',text_polygon=None,cell_polygon=box_polygon([c['column']*100,0,(c['column']+1)*100,40]),header_ids=[])
        return t

    def test_annotation_coordinates_headers_and_grid(self):
        self.assertTrue(validate_annotation(self.fixture(),200,40)['valid'])
        for mutate in [lambda t:t['cells'][0].update(column_span=2),lambda t:t['cells'][0].update(id='1'),
                       lambda t:t['cells'][0].update(header_ids=['foreign']),lambda t:t.update(page_to_crop=[[0,0,0]]*3),
                       lambda t:t['cells'][0].update(text_state='verified_blank')]:
            t=self.fixture();mutate(t)
            with self.assertRaises(ValueError):validate_annotation(t,200,40)

    def test_fill_rectangles_do_not_remove_thin_rules(self):
        from ocr_workbench.pdf_tables import without_shading
        rect={'object_type':'rect','fill':True,'stroke':False,'width':100,'height':.5}
        self.assertTrue(without_shading(rect))
        self.assertFalse(without_shading({**rect,'height':30}))
        self.assertTrue(without_shading({**rect,'height':30,'stroke':True}))

    def test_foreign_image_version_rejected_and_blank_not_invented(self):
        from ocr_workbench.structure_diagnostics import prepare_candidates
        pred,blocks=prediction([['A','']])
        with self.assertRaises(ValueError):
            prepare_candidates({**pred,'image_version':'foreign'},blocks,source_result='r',image_version='v',width=200,height=40)
        prepared=prepare_candidates(pred,blocks,source_result='r',image_version='v',width=200,height=40)
        source=prepared['tables'][0]['skeleton']['cells'][1]['structure_source']
        self.assertEqual(source['empty_evidence']['state'],'unknown')
        self.assertEqual(source['token_ids'],[])


class FinanceTests(unittest.TestCase):
    def test_exact_decimal_signs_percent_and_ids(self):
        self.assertEqual(parse_amount('(1,234.50)')['value'],Decimal('-1234.50'))
        self.assertEqual(parse_amount('12.50%')['value'],Decimal('.125'))
        self.assertEqual(parse_amount('000123')['kind'],'identifier')
        for value in ['NaN','1e3','12,34','(-2)','1.2.3']:
            self.assertEqual(parse_amount(value)['kind'],'unknown')
        self.assertEqual(parse_amount('—')['kind'],'dash')
        self.assertEqual(parse_amount('')['kind'],'blank')

    def test_clear_total_and_no_mutation(self):
        t=table([['项目','金额'],['收入','0.10'],['其他','0.20'],['合计','0.31']]);t['caption']='单位：万元'
        before=deepcopy(t)
        report=check_tables([t])
        self.assertEqual(t,before)
        item=next(i for i in report['issues'] if i['kind']=='total_difference')
        self.assertEqual(item['difference'],'0.01')
        self.assertEqual(item['status'],'rounding_possible')
        t['cells'][-1]['text']='10.00'
        self.assertEqual(check_tables([t])['issues'][-1]['status'],'suspect')

    def test_missing_units_dashes_nested_totals_and_currency_abstain(self):
        for values in ([['项目','金额'],['甲','1.00'],['乙','2.00'],['合计','3.00']],
                       [['项目','金额'],['甲','—'],['乙','2.00'],['合计','3.00']],
                       [['项目','金额'],['小计','1.00'],['乙','2.00'],['合计','3.00']],
                       [['项目','金额'],['甲','USD 1.00'],['乙','HKD 2.00'],['合计','USD 3.00']]):
            report=check_tables([table(values)])
            self.assertTrue(any(i['status']=='uncertain' for i in report['issues']))
            self.assertFalse(any(i['kind']=='total_difference' for i in report['issues']))


class ArbitrationTests(unittest.TestCase):
    setUp=workflow.StructureStoreTests.setUp
    tearDown=workflow.StructureStoreTests.tearDown
    proposal=workflow.StructureStoreTests.proposal

    def enqueue(self,request='structure-one'):
        self.proposal()
        p=self.store.result(self.result_id)
        config={'backend':'external','protocol':'openai','base_url':'http://127.0.0.1:9/v1','model':'fixture',
                'revision':1,'config_sha256':'fixture','prompt_version':'fixture','limits':{'request_timeout_seconds':1}}
        return enqueue_review(self.store,self.result_id,{'request_id':request,'model_id':'external:fixture','revision':p['revision'],
            'version_id':self.version['id'],'scope':'table','review_kind':'structure','table':0},config)

    def snapshot(self):
        task=self.enqueue()
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='running' WHERE id=?",(task,))
        return task,prepare_review(self.store,task)

    def response(self,snapshot):
        candidate=snapshot['structure']['candidates'][0]
        return {'version':VERSION,'decision':'select','candidate_id':candidate['id'],
                'token_ids':candidate['token_ids'],'reason':'原图行列与候选一致'}

    def test_complete_recommends_without_adopting_then_existing_undo(self):
        from ocr_workbench.structure_store import structure_view,decide_structure
        task,s=self.snapshot();before=self.store.result(self.result_id)['edited']
        response=self.response(s)
        self.assertTrue(complete_review(self.store,task,{'structure_response':response}))
        self.assertEqual(self.store.result(self.result_id)['edited'],before)
        view=structure_view(self.store,self.result_id)
        self.assertEqual(view['arbitrations'][0]['response'],response)
        p=next(p for p in view['proposals'] if p['id']==response['candidate_id'])
        saved=decide_structure(self.store,self.result_id,p['id'],workflow.StructureStoreTests.body(self,p))
        self.assertEqual(saved['edited']['tables'][0]['rows'],3)
        back=self.store.history(self.result_id,-1,saved['revision'])
        self.assertEqual(back['edited'],before)

    def test_invalid_unknown_duplicate_missing_and_freeform_rejected(self):
        _,s=self.snapshot();good=self.response(s)
        bad=[]
        for field,value in [('candidate_id','unknown'),('token_ids',good['token_ids']*2),('token_ids',[]),('span',[0,0,99,99])]:
            bad.append({**good,field:value})
        bad.append({**good,'decision':'abstain'})
        for response in bad:
            with self.assertRaises(ValueError):validate_response(response,s['structure'])
        broken=deepcopy(s['structure']);broken['candidates'][0]['table']['cells'][0]['row_span']=999
        with self.assertRaises(ValueError):validate_response(good,broken)

    def test_stale_candidate_and_cancel_prevent_completion(self):
        task,s=self.snapshot()
        with self.store.transaction() as db:db.execute("UPDATE structure_proposals SET state='rejected' WHERE id=?",(s['structure']['candidates'][0]['id'],))
        self.assertFalse(complete_review(self.store,task,{'structure_response':self.response(s)}))
        self.assertEqual(self.store.one('tasks',task)['status'],'cancelled')

    def test_call_budget_includes_failed_attempts(self):
        self.enqueue('one');self.enqueue('two')
        with self.assertRaisesRegex(ValueError,'额度'):self.enqueue('three')

    def test_abstain_and_no_new_text(self):
        _,s=self.snapshot()
        response={'version':VERSION,'decision':'abstain','candidate_id':None,'token_ids':[],'reason':'图像不足'}
        self.assertEqual(validate_response(response,s['structure']),response)

    def test_dispatch_cannot_repeat_after_uncertain_failure(self):
        from ocr_workbench.structure_arbitration import begin_dispatch
        task,_=self.snapshot()
        begin_dispatch(self.store,task)
        with self.assertRaises(Conflict):begin_dispatch(self.store,task)

    def test_both_network_protocols_send_exact_image_and_candidates(self):
        import base64,hashlib
        from test_external_review import ProviderFixture,KEY
        from ocr_workbench.external_review import ExternalReviewSession,LIMITS
        from ocr_workbench.multimodal_runtime import ReviewProtocolError
        provider=ProviderFixture();self.addCleanup(provider.close)
        _,snapshot=self.snapshot()
        for protocol in ('openai','anthropic'):
            config={**snapshot['config'],'protocol':protocol,'base_url':provider.url,'limits':{**LIMITS,'request_timeout_seconds':2}}
            output=Path(self.temp.name)/protocol
            with ExternalReviewSession(Path(self.temp.name),config,output,key=KEY) as session:
                response=session.review(self.store.file(snapshot['image_relative_path']),snapshot)
                self.assertEqual(response['structure_response']['candidate_id'],snapshot['structure']['candidates'][0]['id'])
                payload=provider.requests[-1]['payload'];content=payload['messages'][-1]['content']
                img=next(c for c in content if c['type'] in ('image','image_url'))
                raw=base64.b64decode(img['source']['data'] if protocol=='anthropic' else img['image_url']['url'].split(',')[1])
                self.assertEqual(hashlib.sha256(raw).hexdigest(),response['evidence']['sent_image_sha256'])
                self.assertNotIn(KEY,(output/'input-snapshot.json').read_text('utf-8'))
                self.assertNotIn(KEY,(output/'response.json').read_text('utf-8'))
                for mode in ('unknown-target','duplicate-target','illegal-span','non-json','truncated','status:429'):
                    provider.mode=mode;count=len(provider.requests)
                    with self.assertRaises((ValueError,ReviewProtocolError)):
                        session.review(self.store.file(snapshot['image_relative_path']),snapshot)
                    self.assertEqual(len(provider.requests),count+1)
                provider.mode='normal'


if __name__=='__main__':unittest.main()
