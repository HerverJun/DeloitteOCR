"""Real SQLite receipts and native submissions; no OCR model/service is run."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.screen_resolution import resolve_screen_issues,precision_capability
from ocr_workbench.store import Store,Conflict,encoded


class Queue:
    def __init__(self):self.wake=threading.Event()


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='F:/OCR/output/platform-specialized-agent-20261007/temp',prefix='resolution-')
        self.root=Path(self.temp.name);self.store=Store(self.root/'workspace')
        self.project=self.store.project('问题处理测试')['id']
        self.manager=ScreenBatches(self.store,Queue(),self.root)
        self.capability=patch('ocr_workbench.screen_resolution.precision_capability',return_value={'available':True,'engine':'glm','basis':'controlled-test-only'})
        self.capability.start();self.addCleanup(self.capability.stop)
        self.versions=[]
        for n in range(2):
            path=self.root/f'input-{n}.png';Image.new('RGB',(100,70),(245-n,245,245)).save(path)
            self.versions.append(add_image(self.store,self.project,path.name,path)['active_version'])
        self.batch=self.manager.submit(self.project,{'version_ids':self.versions,'request_id':'source-batch-001',
            'policy':{'primary_engine':'hunyuan','fallback_engines':[],'max_upgrades':0,'require_human_review':True,
                      'budget_seconds':360,'batch_budget_seconds':1000}})['batch_id']
        for item in self.manager._items(self.batch):
            preparation={'width':100,'height':70,'quality':{'flags':[]},'preparation_seconds':20,
                'business_regions':[{'id':'r1','table_box':[0,0,100,70],'sources':['pixel_grid'],'grid_reliable':True,
                                     'observed_grid_columns':3,'observed_grid_rows':2}]}
            table={'rows':2,'columns':2,'cells':[{'row':r,'column':c,'row_span':1,'column_span':1,'text':value}
                for r,line in enumerate([['编号','金额'],['0001','10.00']]) for c,value in enumerate(line)]}
            raw={'status':'success','text':'候选','tables':[table],'engine':'hunyuan','project_image_version':item['input_version_id'],
                 'screen':{'input_sha256':item['source_hash']}}
            result=self.manager._final_result(self.manager._batch(self.project,self.batch),item,raw)
            self.manager._update(self.batch,item['input_version_id'],status='needs_review',result_id=result,result_revision=0,
                preparation=encoded(preparation),candidates=encoded([{'id':'h-'+item['input_version_id'],'engine':'hunyuan','raw':raw}]))

    def tearDown(self):
        self.manager.stop();self.temp.cleanup()

    def body(self,index=0,request='resolve-request-001',budget=120):
        detail=self.manager.detail(self.project,self.batch,self.versions[index])
        issue=next(issue for issue in detail['diagnostics']['items'] if issue['code']=='missing_observed_columns')
        return {'request_id':request,'action':'precision_subbatch','budget_seconds':budget,'targets':[
            {'input_version_id':self.versions[index],'result_id':detail['result']['id'],
             'result_revision':detail['result']['revision'],'issue_ids':[issue['id']]}]}

    def resolve(self,body,allow_new=True):
        return resolve_screen_issues(self.manager,self.project,self.batch,body,allow_new=allow_new)

    def test_submit_uses_existing_native_route_preserves_parent_selection_and_requires_review(self):
        body=self.body();before=self.store.result(body['targets'][0]['result_id'])
        receipt=self.resolve(body)
        self.assertEqual(receipt['status'],'submitted')
        child=self.manager._batch(self.project,receipt['batch_id'])
        self.assertEqual(child['parent_batch_id'],self.batch)
        self.assertEqual(child['policy']['primary_engine'],'glm')
        self.assertEqual(child['policy']['fallback_engines'],[])
        self.assertEqual(child['policy']['max_upgrades'],0)
        self.assertTrue(child['policy']['require_human_review'])
        self.assertFalse(child['policy']['allow_text_fixes']);self.assertFalse(child['policy']['visual_review'])
        self.assertEqual(self.store.result(before['id']),before)
        self.assertEqual(self.store.rows('SELECT result_id FROM selections WHERE image_id=?',(self.manager._items(self.batch)[0]['image_id'],))[0]['result_id'],before['id'])
        self.assertFalse(receipt['selection_applied'])
        self.assertEqual(receipt['human_review'],'not_performed')
        self.assertEqual(self.resolve(body,False)['batch_id'],receipt['batch_id'])

    def test_new_request_same_pixels_does_not_create_another_child_and_partial_overlap_is_explicit(self):
        one=self.resolve(self.body())
        repeat=self.resolve(self.body(request='resolve-repeat-002'))
        self.assertEqual(repeat['status'],'reused');self.assertEqual(repeat['batch_id'],one['batch_id'])
        mixed=self.body(request='resolve-mixed-003');mixed['targets'].extend(self.body(1)['targets'])
        blocked=self.resolve(mixed)
        self.assertEqual(blocked['status'],'blocked');self.assertEqual(blocked['unprocessed_version_ids'],[self.versions[1]])
        second=self.resolve(self.body(1,request='resolve-second-004'))
        self.assertEqual(second['status'],'submitted')
        self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,))),2)

    def test_requested_budget_is_preserved_separately_from_root_limit_and_child_reservation(self):
        body=self.body(budget=500)
        receipt=self.resolve(body)
        child=self.manager._batch(self.project,receipt['batch_id'])
        self.assertEqual(receipt['requested_budget_seconds'],500)
        self.assertEqual(receipt['budget']['limit_seconds'],1000)
        self.assertEqual(child['policy']['batch_budget_seconds'],180)
        self.assertEqual(receipt['budget']['reserved_seconds'],180)
        recovered=self.resolve(body,False)
        self.assertEqual(recovered['requested_budget_seconds'],500)
        repeated=self.resolve(self.body(request='resolve-new-budget-008',budget=70))
        self.assertEqual(repeated['status'],'reused')
        self.assertEqual(repeated['batch_id'],receipt['batch_id'])
        self.assertEqual(repeated['requested_budget_seconds'],70)
        self.assertEqual(repeated['budget']['reserved_seconds'],180)

    def test_unknown_readback_never_submits_and_conflicting_request_payload_is_rejected(self):
        body=self.body();count=len(self.store.rows('SELECT id FROM screen_batches'))
        self.assertIsNone(self.resolve(body,False));self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches')),count)
        self.resolve(body);changed=deepcopy(body);changed['budget_seconds']=121
        with self.assertRaises(Conflict):self.resolve(changed,False)

    def test_current_revision_issue_identity_and_input_bytes_are_checked_before_submission(self):
        body=self.body();changed=deepcopy(body);changed['targets'][0]['result_revision']=99
        with self.assertRaises(Conflict):self.resolve(changed)
        changed=deepcopy(body);changed['targets'][0]['issue_ids']=['not-an-issue']
        with self.assertRaises(Conflict):self.resolve(changed)
        version=self.store.one('versions',self.versions[0]);self.store.file(version['path']).write_bytes(b'changed')
        with self.assertRaises(Conflict):self.resolve(body)
        self.assertFalse(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,)))

    def test_intent_survives_submit_crash_and_readback_does_not_repeat_unknown_submission(self):
        body=self.body()
        with patch.object(self.manager,'submit',side_effect=RuntimeError('controlled-before-submit')):
            with self.assertRaises(RuntimeError):self.resolve(body)
        read=self.resolve(body,False);self.assertEqual(read['status'],'intent')
        self.assertFalse(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,)))
        resumed=self.resolve(body);self.assertEqual(resumed['status'],'submitted')
        self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,))),1)

    def test_submit_response_loss_is_recovered_from_native_request_identity(self):
        original=self.manager.submit;body=self.body()
        def submit_then_lose(*args,**kwargs):
            original(*args,**kwargs);raise RuntimeError('controlled-response-loss')
        with patch.object(self.manager,'submit',side_effect=submit_then_lose):
            with self.assertRaises(RuntimeError):self.resolve(body)
        receipt=self.resolve(body,False)
        self.assertEqual(receipt['status'],'reused');self.assertIsNotNone(receipt['batch_id'])
        self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,))),1)

    def test_sibling_reservations_and_ancestor_preparation_reduce_remaining_budget(self):
        with self.store.transaction() as db:
            policy=json.loads(db.execute('SELECT policy FROM screen_batches WHERE id=?',(self.batch,)).fetchone()['policy'])
            policy['batch_budget_seconds']=150
            db.execute('UPDATE screen_batches SET policy=? WHERE id=?',(encoded(policy),self.batch))
        first=self.resolve(self.body(budget=100))
        self.assertEqual(first['budget']['reserved_seconds'],100)
        blocked=self.resolve(self.body(1,request='resolve-second-005',budget=100))
        self.assertEqual(blocked['status'],'submitted')
        self.assertEqual(blocked['budget']['reserved_seconds'],10)
        self.assertEqual(blocked['budget']['consumed_or_reserved_seconds'],140)

    def test_existing_glm_or_incomplete_source_returns_review_reason_not_another_model_run(self):
        item=self.manager._items(self.batch)[0];candidates=json.loads(item['candidates']);candidates[0]['engine']='glm'
        self.manager._update(self.batch,item['input_version_id'],candidates=encoded(candidates))
        receipt=self.resolve(self.body());self.assertEqual(receipt['status'],'blocked')
        self.assertEqual(receipt['existing_candidates'][0]['reason'],'existing_glm_candidate_requires_comparison')
        self.assertFalse(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,)))

    def test_missing_capability_and_exhausted_lineage_are_explicit(self):
        with patch('ocr_workbench.screen_resolution.precision_capability',return_value={'available':False,'reason':'glm_runtime_missing'}):
            blocked=self.resolve(self.body());self.assertEqual(blocked['reason'],'glm_runtime_missing')
        with self.store.transaction() as db:
            policy=json.loads(db.execute('SELECT policy FROM screen_batches WHERE id=?',(self.batch,)).fetchone()['policy'])
            policy['batch_budget_seconds']=10
            db.execute('UPDATE screen_batches SET policy=? WHERE id=?',(encoded(policy),self.batch))
        blocked=self.resolve(self.body(request='resolve-budget-006'));self.assertEqual(blocked['reason'],'lineage_budget_exhausted')
        self.assertFalse(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(self.batch,)))

    def test_recovery_verifies_native_child_parameters_not_just_derived_request_name(self):
        original=self.manager.submit;body=self.body()
        def submit_then_lose(*args,**kwargs):
            value=original(*args,**kwargs)
            with self.store.transaction() as db:
                db.execute('UPDATE screen_batches SET parent_batch_id=NULL WHERE id=?',(value['batch_id'],))
            raise RuntimeError('controlled-response-loss')
        with patch.object(self.manager,'submit',side_effect=submit_then_lose):
            with self.assertRaises(RuntimeError):self.resolve(body)
        with self.assertRaises(Conflict):self.resolve(body,False)

    def test_capability_check_distinguishes_configuration_from_present_assets_without_loading(self):
        self.assertEqual(precision_capability(self.manager)['reason'],'engine_inventory_unavailable')
        (self.root/'config').mkdir()
        (self.root/'config/engines.json').write_text(json.dumps({'glm':{'capabilities':{'tables':True},'models':['GLM-OCR']}}),'utf-8')
        self.assertEqual(precision_capability(self.manager)['reason'],'glm_runtime_missing')
        (self.root/'runtimes/glm').mkdir(parents=True)
        (self.root/'runtimes/glm/python.exe').write_bytes(b'controlled-test-not-executed')
        self.assertEqual(precision_capability(self.manager)['reason'],'glm_model_assets_missing')
        (self.root/'models/GLM-OCR').mkdir(parents=True)
        (self.root/'models/GLM-OCR/model.safetensors').write_bytes(b'controlled-test-not-a-model')
        capability=precision_capability(self.manager)
        self.assertTrue(capability['available']);self.assertFalse(capability['inference_verified_by_this_check'])


if __name__=='__main__':unittest.main()
