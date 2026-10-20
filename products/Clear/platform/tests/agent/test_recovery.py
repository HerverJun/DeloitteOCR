"""Recovery checks, excluded from actual controller acceptance scores."""
import asyncio
from dataclasses import replace
import json
from unittest.mock import patch
from types import SimpleNamespace
import unittest

import test_graph as graph_tests
import test_data_tools as data_tests
from packages.contracts.agent import AgentToolResult, AgentRef
from workbench_platform.agent.registry import error_result, invoke
from workbench_platform.agent.store import Conflict
from workbench_platform.agent.tools import context
from workbench_platform.application_services.operations import data, jobs
from workbench_platform.agent.context import bounded_messages


class RecoveryTests(graph_tests.GraphTests):
    test_native_wait_does_not_call_model_until_result_and_delivery_is_readable = None
    test_bound_question_survives_close_reopen_and_changes_next_tool = None
    test_stop_fences_late_controller_response = None
    test_no_delivery_cannot_be_marked_success = None

    async def test_append_during_final_reply_cannot_reuse_stale_delivery(self):
        ready=asyncio.Event();release=asyncio.Event()
        asset=self.asset('suppliers-left.csv')
        observed=[]
        async def provider(state,schemas):
            step=state['step'];observed.append(step)
            if step==0:
                return graph_tests.response(state,'data_create_session',{'name':'append final'})
            previous=next((m for m in reversed(state['messages']) if m['role']=='tool'), None)
            if step==1:
                value=previous['result']['data']
                return graph_tests.response(state,'data_import',{'data_session_id':value['data_session_id'],'expected_version':value['version'],
                    'side':'left','asset_id':asset.asset_id,'asset_revision':asset.revision,'blob_hash':asset.blob_hash})
            if step==2:
                return graph_tests.response(state,'data_read_table',{'data_session_id':previous['result']['data']['data_session_id'],'side':'left'})
            if step==3:
                return graph_tests.response(state,'delivery_publish',{'summary':'已读回台账','result_refs':previous['result']['result_refs']})
            self.assertGreaterEqual(step,4)
            self.assertTrue(any(m.get('content')=='重新导出Excel' for m in state['messages']))
            self.assertIsNone(state['delivery'])
            return graph_tests.response(state,content='本轮还未重新导出')
        runtime,session=await self.make_runtime(provider)
        original = runtime.registry.tools['delivery_publish']
        async def delayed_delivery(args, ctx):
            value = await invoke(original.execute, args, ctx)
            ready.set()
            await release.wait()
            return value
        runtime.registry.tools['delivery_publish'] = replace(original, execute=delayed_delivery)
        try:
            run=await runtime.submit(session['id'],'查看台账','initial_read')
            # Five persisted controller/tool stages may exceed 30s when
            # native and public-stream suites exercise the same drive.
            await asyncio.wait_for(ready.wait(),90)
            await runtime.submit(session['id'],'重新导出Excel','append_final')
            release.set()
            self.assertEqual((await self.wait_status(runtime,run['id'],{'paused','failed','succeeded'}))['status'],'paused')
            self.assertEqual(observed[:5],[0,1,2,3,4])
            self.assertFalse(any(event['kind']=='delivery' for event in runtime.store.events(session['id'], limit=10000)['items']))
        finally:
            await runtime.close()

    async def test_new_turn_receives_original_owned_job_after_cancel(self):
        expected = {}
        async def provider(state, schemas):
            if state['step'] == 0:
                facts = next(m['content'] for m in state['messages'] if m['role']=='user' and expected['job_id'] in m.get('content',''))
                self.assertIn(expected['job_id'],facts)
                self.assertIn('导入原台账',facts)
                return graph_tests.response(state,'jobs_get',{'job_id':expected['job_id']})
            return graph_tests.response(state, content='原任务回执已读取，等待下一步')
        runtime,session=await self.make_runtime(provider)
        try:
            old,_=runtime.store.start_run(session['id'],'导入原台账','original_before_cancel',0)
            ctx=runtime.context(runtime.initial_state(old,'导入原台账'))
            created=await runtime.registry.dispatch({'call_id':'create','tool':'data_create_session','arguments':{'name':'原任务'}},ctx)
            asset=self.asset('suppliers-left.csv')
            imported=await runtime.registry.dispatch({'call_id':'import','tool':'data_import','arguments':{
                'data_session_id':created['data']['data_session_id'],'expected_version':1,'side':'left',
                'asset_id':asset.asset_id,'asset_revision':asset.revision,'blob_hash':asset.blob_hash}},ctx)
            self.assertEqual(imported['status'],'pending')
            expected['job_id']=imported['job_refs'][0]['object_id']
            runtime.cancel(old['id'])
            new=await runtime.submit(session['id'],'继续核实刚才的任务','new_after_cancel')
            self.assertEqual((await self.wait_status(runtime,new['id'],{'paused','failed'}))['status'],'paused')
            self.assertEqual(len(self.native.get(created['data']['data_session_id'])['jobs']),1)
            self.assertEqual(len(self.native.get(created['data']['data_session_id'])['uploads']),1)
            self.assertEqual(runtime.store.run(old['id'])['status'],'cancelled')
        finally:
            await runtime.close()

    async def test_reconcile_queries_original_operation_without_resubmission(self):
        calls, lookups, receipt = [], [], []
        ready = False
        async def provider(state, schemas):
            return graph_tests.response(state, 'data_create_session', {'name': 'one'}) if state['step'] == 0 else graph_tests.response(state, content='已建立作业，尚未交付')
        runtime, session = await self.make_runtime(provider)
        original = runtime.registry.tools['data_create_session']
        def execute(args, ctx):
            calls.append(ctx.operation_id)
            receipt.append(data.create(args, ctx))
            raise TimeoutError('Native success but response lost')
        def lookup(args, ctx):
            lookups.append(ctx.operation_id)
            return receipt[0] if ready else None
        runtime.registry.tools['data_create_session'] = replace(original, execute=execute, lookup_operation=lookup)
        try:
            run = await runtime.submit(session['id'], '创建作业', 'reconcile_start')
            await self.wait_status(runtime, run['id'], {'needs_verification'})
            while run['id'] in runtime.running:
                await asyncio.sleep(.01)
            with self.assertRaises(Conflict):
                await runtime.reconcile(run['id'])
            self.assertEqual(len(calls), 1)
            ready = True
            await runtime.reconcile(run['id'])
            self.assertEqual((await self.wait_status(runtime, run['id'], {'paused', 'failed'}))['status'], 'paused')
            self.assertGreaterEqual(len(lookups), 2)
            self.assertEqual(set(lookups), set(calls))
            self.assertEqual(len(self.native.list('wtest')), 1)
            self.assertEqual(len(runtime.store.rows('SELECT * FROM operations')), 1)
            restored = [event['payload'] for event in runtime.store.events(session['id'], limit=10000)['items']
                        if event['kind'] == 'tool_activity' and event['payload']['phase'] == 'restored']
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]['status'], 'success')
            self.assertEqual(restored[0]['operation_id'], calls[0])
        finally:
            await runtime.close()

    async def test_persisted_answer_recovers_after_restart_before_resume(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                return graph_tests.response(state, 'ask_user', {'question': '工作表？', 'options': ['本期', '上期']})
            if state['step'] == 1:
                value = next(m['result'] for m in reversed(state['messages']) if m['role']=='tool')
                return graph_tests.response(state, 'data_create_session', {'name': value['data']['reply']})
            return graph_tests.response(state, content='已按答复建立作业，尚未交付')
        runtime, session = await self.make_runtime(provider)
        run = await runtime.submit(session['id'], '比对', 'answer_recovery')
        await self.wait_status(runtime, run['id'], {'waiting_user'})
        await runtime.close()
        question = runtime.store.rows('SELECT id FROM decisions')[0]['id']
        with runtime.store.transaction() as db:
            db.execute('UPDATE decisions SET reply=?,request_id=? WHERE id=?', ('上期', 'saved_reply', question))
        from workbench_platform.agent.runtime import AgentRuntime
        reopened = AgentRuntime(runtime.store.root, self.services, model_provider=provider)
        self.services.agent = reopened
        await reopened.start()
        try:
            self.assertEqual((await self.wait_status(reopened, run['id'], {'paused', 'failed'}))['status'], 'paused')
            self.assertEqual([v['name'] for v in self.native.list('wtest')], ['上期'])
        finally:
            await reopened.close()


class RetryTests(data_tests.DataToolTests):
    test_real_clean_compare_export_independent_values_and_native_provenance = None
    test_multisheet_is_discovered_and_not_implicitly_imported = None
    test_native_upload_replay_version_and_cross_project_boundaries = None

    async def test_recursive_retry_preserves_latest_model_and_only_failed_assets(self):
        assets = [self.asset('suppliers-left.csv'), self.asset('suppliers-right.csv')]
        created = context.create_set(context.AssetSetCreate(asset_ids=[a.asset_id for a in assets]), replace(self.ctx, operation_id='oset'))['data']
        fixed = self.sets.get('wtest', created['asset_set_id'], created['revision'])
        def operation(call_id, tool, arguments, parent=None):
            op, _ = self.store.begin_operation(self.ctx.run_id, 1, {'call_id': call_id, 'tool': tool, 'arguments': arguments}, parent)
            return op
        first = operation('original', 'classification_infer', {'link_id':'linkA', 'asset_set_id':fixed.asset_set_id,
            'asset_set_revision':fixed.revision, 'model_id':'originalModel', 'device':'cpu'})
        old = operation('retry_one', 'jobs_retry_failed', {'job_id':'jobOriginal', 'model_id':'alternativeModel'}, first['id'])
        self.store.finish_operation(old['id'], AgentToolResult(status='success', summary='重试结束', data={},
            job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='jobRetry',revision=1,application='classification',parent_id='handoffA')]).model_dump())
        current = operation('retry_two', 'jobs_retry_failed', {'job_id':'jobRetry'})
        native = SimpleNamespace(_context=lambda *v:None, _adapter=lambda *v:None,
            result_snapshot=lambda *v: {'items':[{'asset_id':assets[0].asset_id,'state':'succeeded'}, {'asset_id':assets[1].asset_id,'state':'failed'}]})
        self.services.classification_handoff = native
        captured = []
        def infer(args, ctx):
            captured.append(args)
            return AgentToolResult(status='pending',summary='new attempt', data={}).model_dump()
        with patch.object(jobs,'read_job',return_value={'status':'partial','counts':{'failed':1}}), \
             patch.object(jobs,'scoped_handoff',return_value={'link_id':'linkA','asset_set_id':fixed.asset_set_id,'asset_set_revision':fixed.revision}), \
             patch.object(jobs.classification,'infer',side_effect=infer):
            result = jobs.retry(jobs.JobRetry(job_id='jobRetry'), replace(self.ctx,operation_id=current['id']))
        self.assertEqual(captured[0].model_id,'alternativeModel')
        self.assertEqual(captured[0].parent_operation_id,old['id'])
        members = self.sets.get('wtest',captured[0].asset_set_id,captured[0].asset_set_revision).items
        self.assertEqual([m.asset_id for m in members],[assets[1].asset_id])
        self.assertEqual(result['data']['excluded_successful_inputs'],1)
        self.assertEqual(self.store.rows('SELECT parent_id FROM operations WHERE id=?',(current['id'],))[0]['parent_id'],old['id'])
        with patch.object(jobs,'read_job',return_value={'status':'partial','counts':{'failed':1}}), \
             patch.object(jobs,'scoped_handoff',return_value={'link_id':'linkA','asset_set_id':fixed.asset_set_id,'asset_set_revision':fixed.revision}), \
             patch.object(jobs.classification,'infer',side_effect=AssertionError('lookup must not submit')), \
             patch.object(jobs.classification,'lookup',side_effect=infer):
            recovered=jobs.lookup_retry(jobs.JobRetry(job_id='jobRetry'),replace(self.ctx,operation_id=current['id']))
        self.assertEqual(captured[1].asset_set_id,captured[0].asset_set_id)
        self.assertEqual(captured[1].model_id,'alternativeModel')
        self.assertEqual(recovered['data']['parent_operation_id'],old['id'])


class CompressionTests(unittest.TestCase):
    def test_compression_keeps_last_complete_tool_exchange_and_goals(self):
        result=AgentToolResult(status='success',summary='已读取',data={'rows':[{'value':'x'*3000}]*30},
            result_refs=[AgentRef(kind='table',workspace_id='wtest',object_id='tableA',revision='r1')]).model_dump()
        messages=[{'role':'user','content':'保留前导零'}]
        for index in range(3):
            messages.extend([{'role':'assistant','content':'x'*3000,'calls':[{'call_id':f'call{index}','tool':'data_read_table','arguments':{}}]},
                {'role':'tool','call_id':f'call{index}','result':result}])
        reduced,compressed=bounded_messages(messages,18000,1000,0)
        self.assertTrue(compressed)
        self.assertEqual(reduced[0]['content'],'保留前导零')
        self.assertEqual([m['call_id'] for m in reduced if m['role']=='tool'],['call2'])
        self.assertEqual(reduced[-2]['calls'][0]['call_id'],'call2')
        self.assertTrue(reduced[-1]['result']['data']['context_preview_only'])
        self.assertEqual(len(messages[-1]['result']['data']['rows']),30)
        self.assertIn('tableA',reduced[1]['content'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
