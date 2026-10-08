"""SQLite budget counterexamples and actual queue-to-adapter timeout plumbing.

Historical elapsed counters are controlled fixtures, not measured OCR speed.
The adapter is CPU-only and never loads a model or starts a background service.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches,screen_seconds_spent
from ocr_workbench.screen_resolution import execution_budget,resolve_screen_issues
from ocr_workbench.store import Store,encoded,now
from ocr_workbench.task_queue import TaskQueue


ROOT=Path('F:/OCR/output/platform-specialized-agent-20261007/temp')
TABLE={'rows':2,'columns':2,'cells':[{'row':r,'column':c,'row_span':1,'column_span':1,'text':text}
    for r,line in enumerate([['编号','金额'],['0001','10.00']]) for c,text in enumerate(line)]}


def prepare(source,folder,**kwargs):
    path=Path(folder)/'primary.png'
    with Image.open(source) as image:image.save(path)
    return {'width':100,'height':70,'primary_id':'primary','quality':{'flags':[],'unreadable':False},
        'business_regions':[{'id':'r1','table_box':[0,0,100,70],'sources':['pixel_grid'],
            'grid_reliable':True,'observed_grid_rows':2,'observed_grid_columns':3}],
        'prepared_images':[{'id':'primary','kind':'original','path':str(path),'width':100,'height':70,
            'prepared_to_source':[1,0,0,0,1,0,0,0,1]}]}


class Adapter:
    def __init__(self,bundle,engine,folder,observed):
        self.bundle=Path(bundle);self.engine=engine;self.observed=observed
        self.recognition_timeout=900;self.cancelled=None
    def load(self):pass
    def unload(self):pass
    def recognize(self,path,output,*args,**kwargs):
        self.observed.append({'engine':self.engine,'timeout':self.recognition_timeout})
        return {'status':'success','text':'fixture','tables':[deepcopy(TABLE)],'blocks':[],
                'engine':self.engine,'elapsed_seconds':0}


class LineageBudgetTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT,prefix='lineage-budget-')
        self.root=Path(self.temp.name);self.store=Store(self.root/'workspace')
        self.project=self.store.project('预算边界')['id'];self.observed=[]
        self.queue=TaskQueue(self.store,self.root,adapter_factory=lambda *args:Adapter(*args,self.observed))
        self.manager=ScreenBatches(self.store,self.queue,self.root,prepare=prepare)
        self.manager.defaults=lambda:{}
        self.capability=patch('ocr_workbench.screen_resolution.precision_capability',return_value={'available':True,'basis':'CPU-controlled-fixture'})
        self.capability.start()
        self.version_index=0

    def tearDown(self):
        self.capability.stop();self.manager.stop();self.queue.unload();self.temp.cleanup()

    def image(self,duplicate=None):
        index=self.version_index;self.version_index+=1
        path=self.root/f'input-{index}.png'
        if duplicate:
            path.write_bytes(self.store.file(self.store.one('versions',duplicate)['path']).read_bytes())
        else:Image.new('RGB',(100,70),(200-index,240,240)).save(path)
        return add_image(self.store,self.project,path.name,path)['active_version']

    def batch(self,versions,**policy):
        return self.manager.submit(self.project,{'version_ids':versions,'request_id':'main-request-'+str(self.version_index),
            'policy':{'primary_engine':'hunyuan','fallback_engines':[],'max_upgrades':0,'layout_evidence':False,
                'budget_seconds':200,'request_timeout_seconds':180,'batch_budget_seconds':1000,
                'require_human_review':True,**policy}})['batch_id']

    def task(self,batch,version,attempt=None):
        condition=' AND s.attempt=?' if attempt is not None else ''
        args=(batch,version,attempt) if attempt is not None else (batch,version)
        return self.store.rows('SELECT t.*,s.metadata,s.attempt,s.spent_seconds FROM tasks t JOIN screen_tasks s ON s.task_id=t.id '
            'WHERE s.batch_id=? AND s.input_version_id=?'+condition+' ORDER BY s.attempt DESC,t.created DESC',args)[0]

    def cost(self,task,seconds,status='failed'):
        with self.store.transaction() as db:
            db.execute('UPDATE screen_tasks SET spent_seconds=? WHERE task_id=?',(seconds,task['id']))
            db.execute('UPDATE tasks SET status=?,started=NULL,finished=NULL,error=? WHERE id=?',
                       (status,'controlled historical failure' if status=='failed' else None,task['id']))

    def finish(self,batch,version,seconds=20):
        task=self.task(batch,version)
        self.cost(task,seconds,'running')
        self.store.complete(task['id'],{'status':'success','text':'fixture','tables':[deepcopy(TABLE)],
            'blocks':[],'engine':task['engine'],'project_image_version':version})
        self.manager.step()

    def body(self,batch,version,request='precision-request-1',budget=120):
        detail=self.manager.detail(self.project,batch,version)
        issue=next(item for item in detail['diagnostics']['items'] if item['code']=='missing_observed_columns')
        return {'request_id':request,'action':'precision_subbatch','budget_seconds':budget,'targets':[
            {'input_version_id':version,'result_id':detail['result']['id'],
             'result_revision':detail['result']['revision'],'issue_ids':[issue['id']]}]}

    def test_precision_cannot_reserve_budget_while_primary_inputs_are_unsettled(self):
        one,two=self.image(),self.image();batch=self.batch([one,two])
        self.manager.step();self.finish(batch,one)
        before=self.store.result(self.manager.detail(self.project,batch,one)['result_id'])
        body=self.body(batch,one)
        receipt=resolve_screen_issues(self.manager,self.project,batch,body)
        self.assertEqual(receipt['reason'],'primary_batch_unsettled')
        self.assertEqual(receipt['pending_version_ids'],[two])
        self.assertIn('主批次',receipt['message'])
        self.assertFalse(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(batch,)))
        self.assertEqual(self.store.result(before['id']),before)
        self.assertEqual(resolve_screen_issues(self.manager,self.project,batch,body,allow_new=False)['reason'],receipt['reason'])

    def test_live_child_reservation_clamps_root_retry_and_actual_adapter_request(self):
        version=self.image();batch=self.batch([version])
        self.manager.step();self.finish(batch,version,50)
        prior=self.manager.detail(self.project,batch,version)['result'];snapshot=deepcopy(prior)
        child=resolve_screen_issues(self.manager,self.project,batch,self.body(batch,version))['batch_id']
        # A new model identity requires inference instead of reusing the prior
        # successful candidate; original-pixel budgets must still carry over.
        self.manager.model_identity='controlled-new-model-identity'
        self.manager._update(batch,version,status='failed',review_state='accepted')
        self.manager.action(self.project,batch,{'action':'retry_failed','version_ids':[version],'request_id':'retry-with-child'})
        self.manager.step()
        root_retry=self.task(batch,version,1)
        metadata=json.loads(root_retry['metadata'])
        self.assertLessEqual(metadata['budget_reservation_seconds'],30)
        self.assertGreaterEqual(metadata['budget_reservation_seconds'],25)
        self.assertEqual(self.manager._batch(self.project,child)['policy']['budget_seconds'],120)
        self.queue.action(self.project,'pause',[self.task(child,version)['id']])
        self.queue.step()
        self.assertEqual(self.observed[0]['engine'],'hunyuan')
        self.assertLessEqual(self.observed[0]['timeout'],30)
        self.assertGreaterEqual(self.observed[0]['timeout'],20)
        self.assertEqual(self.store.result(prior['id']),snapshot)
        self.assertEqual(self.manager._items(batch)[0]['review_state'],'accepted')

    def test_exhausted_retry_keeps_original_attempt_results_and_confirmation(self):
        version=self.image();batch=self.batch([version],budget_seconds=100)
        self.manager.step();self.finish(batch,version,100)
        prior=self.manager.detail(self.project,batch,version)['result'];snapshot=deepcopy(prior)
        self.manager._update(batch,version,status='failed',review_state='accepted')
        count=len(self.store.rows('SELECT id FROM tasks'))
        self.manager.action(self.project,batch,{'action':'retry_failed','request_id':'retry-exhausted'})
        item=self.manager._items(batch)[0]
        self.assertEqual(item['attempt'],0)
        self.assertEqual(item['stage'],'source_budget_reached')
        self.assertIn('累计',item['error'])
        self.assertEqual(item['review_state'],'accepted')
        self.assertEqual(self.store.result(prior['id']),snapshot)
        self.manager.step()
        self.assertEqual(len(self.store.rows('SELECT id FROM tasks')),count)

    def test_running_task_releases_only_own_reservation_and_keeps_sibling_frozen(self):
        one,two=self.image(),self.image();batch=self.batch([one,two],budget_seconds=100,request_timeout_seconds=100,batch_budget_seconds=120)
        self.manager.step()
        first,second=self.task(batch,one),self.task(batch,two)
        first_allowance=json.loads(first['metadata'])['budget_reservation_seconds']
        second_allowance=json.loads(second['metadata'])['budget_reservation_seconds']
        self.assertLessEqual(first_allowance+second_allowance,120)
        self.cost(first,20,'running')
        own=self.queue._screen_budget_remaining(first,for_request=True)
        sibling=self.queue._screen_budget_remaining(second,for_request=True)
        self.assertGreater(own,60)
        self.assertLessEqual(own,first_allowance-20)
        self.assertLessEqual(sibling,second_allowance)
        self.assertLessEqual(own+sibling+20,120)

    def test_identical_pixels_reuse_without_charge_and_failed_cache_cannot_restart_full_budget(self):
        one=self.image();two=self.image(duplicate=one)
        batch=self.batch([one,two],budget_seconds=100)
        self.manager.step();leader,follower=self.task(batch,one),self.task(batch,two)
        self.assertEqual(follower['kind'],'screen_cached')
        self.assertEqual(json.loads(follower['metadata'])['budget_reservation_seconds'],0)
        self.cost(leader,100,'failed')
        self.manager._resolve_ocr_reuse(self.manager._batch(self.project,batch))
        failed=self.store.one('tasks',follower['id'])
        self.assertEqual(failed['status'],'failed')
        self.assertIn('累计',failed['error'])
        self.assertEqual(failed['kind'],'screen_cached')
        self.assertEqual(self.observed,[])

    def test_recovery_preserves_carried_cost_after_started_is_cleared(self):
        version=self.image();batch=self.batch([version],budget_seconds=100,request_timeout_seconds=100)
        self.manager.step();task=self.task(batch,version)
        self.cost(task,30,'running')
        started=(datetime.now(timezone.utc)-timedelta(seconds=16)).isoformat(timespec='seconds').replace('+00:00','Z')
        with self.store.transaction() as db:db.execute('UPDATE tasks SET started=? WHERE id=?',(started,task['id']))
        self.store.recover()
        recovered=self.task(batch,version)
        self.assertEqual(recovered['status'],'interrupted')
        self.assertGreaterEqual(screen_seconds_spent(self.store,batch,version),45)
        remaining=self.queue._screen_budget_remaining(recovered,for_request=True)
        self.assertLessEqual(remaining,55)
        self.assertGreater(remaining,40)
        reopened=ScreenBatches(Store(self.store.root),self.queue,self.root,prepare=prepare)
        try:
            item=reopened._items(batch)[0]
            self.assertLessEqual(reopened._execution_budget(reopened._batch(self.project,batch),item)['remaining_seconds'],55)
            reopened.action(self.project,batch,{'action':'resume','version_ids':[version]})
            self.assertLessEqual(self.queue._screen_budget_remaining(self.task(batch,version),for_request=True),55)
            self.queue.step()
            self.assertLessEqual(self.observed[-1]['timeout'],55)
            self.assertGreaterEqual(self.task(batch,version)['spent_seconds'],45)
            # Old recovered ledgers can already have folded the segment and
            # cleared started; their carried cost remains payable.
            with self.store.transaction() as db:db.execute('UPDATE tasks SET started=NULL,finished=NULL WHERE id=?',(task['id'],))
            self.assertGreaterEqual(screen_seconds_spent(self.store,batch,version),45)
        finally:reopened.stop()

    def test_new_attempts_and_repeated_retry_requests_never_reset_source_allowance(self):
        version=self.image();batch=self.batch([version],budget_seconds=100,request_timeout_seconds=100)
        self.manager.step();original=self.task(batch,version)
        self.cost(original,70);self.manager.step()
        body={'action':'retry_failed','request_id':'retry-attempt-one'}
        self.manager.action(self.project,batch,body);self.manager.action(self.project,batch,body)
        self.manager.step();first_retry=self.task(batch,version,1)
        self.assertLessEqual(json.loads(first_retry['metadata'])['budget_reservation_seconds'],30)
        self.cost(first_retry,15);self.manager.step()
        self.manager.action(self.project,batch,{'action':'retry_failed','request_id':'retry-attempt-two'})
        self.manager.step();second_retry=self.task(batch,version,2)
        self.assertLessEqual(json.loads(second_retry['metadata'])['budget_reservation_seconds'],15)
        self.assertGreater(screen_seconds_spent(self.store,batch,version,0),69)
        self.assertGreater(screen_seconds_spent(self.store,batch,version,1),14)
        self.assertEqual(len(self.store.rows('SELECT task_id FROM screen_tasks WHERE batch_id=?',(batch,))),3)

    def test_child_runtime_keeps_sibling_child_reservation_and_positive_own_allowance(self):
        one,two=self.image(),self.image();batch=self.batch([one,two],batch_budget_seconds=160)
        self.manager.step();self.finish(batch,one,10);self.finish(batch,two,10)
        first=resolve_screen_issues(self.manager,self.project,batch,self.body(batch,one,budget=60))['batch_id']
        second=resolve_screen_issues(self.manager,self.project,batch,self.body(batch,two,request='precision-request-2',budget=60))['batch_id']
        self.manager.step()
        task=self.task(first,one);other=self.task(second,two)
        self.cost(task,10,'running')
        remaining=self.queue._screen_budget_remaining(task,for_request=True)
        self.assertGreater(remaining,40)
        self.assertLessEqual(remaining,50)
        other_remaining=self.queue._screen_budget_remaining(other,for_request=True)
        self.assertLessEqual(remaining+other_remaining+30,160)
        budget=execution_budget(self.manager,self.project,first,one,task_id=task['id'])
        self.assertGreaterEqual(budget['inflight_reserved_seconds'],59)
        self.assertGreater(budget['wall_deadline_remaining_seconds'],remaining)

    def test_frozen_intent_keeps_reservation_during_main_retry_and_resumes_after_settlement(self):
        one,two=self.image(),self.image();batch=self.batch([one,two])
        self.manager.step();self.finish(batch,one);self.finish(batch,two)
        body=self.body(batch,one)
        with patch.object(self.manager,'submit',side_effect=RuntimeError('controlled-pre-submit-stop')):
            with self.assertRaises(RuntimeError):resolve_screen_issues(self.manager,self.project,batch,body)
        self.manager._update(batch,two,status='prepared',stage='retry')
        delayed=resolve_screen_issues(self.manager,self.project,batch,body)
        self.assertEqual(delayed['status'],'intent')
        self.assertEqual(delayed['reason'],'primary_batch_unsettled')
        stored=json.loads(self.store.rows('SELECT receipt FROM screen_actions WHERE batch_id=? AND request_id=?',
                                         (batch,body['request_id']))[0]['receipt'])
        self.assertEqual(stored['status'],'intent')
        budget=execution_budget(self.manager,self.project,batch,two)
        self.assertGreaterEqual(budget['inflight_reserved_seconds'],120)
        self.manager._update(batch,two,status='needs_review',stage='review')
        resumed=resolve_screen_issues(self.manager,self.project,batch,body)
        self.assertEqual(resumed['status'],'submitted')
        self.assertEqual(resumed['budget']['reserved_seconds'],120)
        self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches WHERE parent_batch_id=?',(batch,))),1)

    def test_whole_execution_allowance_and_request_cap_remain_distinct_after_cold_loading(self):
        version=self.image();batch=self.batch([version],budget_seconds=360,request_timeout_seconds=180)
        self.manager.step();task=self.task(batch,version)
        metadata=json.loads(task['metadata'])
        self.assertGreater(metadata['budget_reservation_seconds'],350)
        self.assertEqual(metadata['request_timeout_seconds'],180)
        self.assertGreater(self.queue._screen_budget_remaining(task),350)
        self.assertEqual(self.queue._screen_budget_remaining(task,for_request=True),180)
        with patch.object(Adapter,'load',lambda adapter:self.cost(self.task(batch,version),210,'running')):
            self.queue.step()
        self.assertLessEqual(self.observed[-1]['timeout'],150)
        self.assertGreater(self.observed[-1]['timeout'],140)


if __name__=='__main__':unittest.main()
