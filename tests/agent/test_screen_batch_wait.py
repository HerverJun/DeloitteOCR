"""Durable screen directives; deterministic controller/native fixtures only.

These tests check execution semantics, never OCR or real-controller accuracy.
"""
import asyncio
from dataclasses import replace
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.scope import scope_summary,screen_fastpath
from workbench_platform.agent.tools.screen_batches import compact
from workbench_platform.agent.context import _history_summary
from workbench_platform.agent.store import digest
from workbench_platform.agent.providers import normalize_response,ProviderFault
from workbench_platform.agent.graph import terminal_screen_delivery
from workbench_platform.application_services.job_lifecycle import ADAPTERS, Lifecycle
from workbench_platform.store import WorkspaceStore


RUN = Path('F:/OCR/output/ocr-screen-benchmark-20261007/platform-validation/agent-tests')


def response(state, tool=None, arguments=None):
    return {'id': state['run_id'] + ':model:' + str(state['step']), 'role': 'assistant', 'content': '',
            'calls': [{'call_id': 'call_' + str(state['step']), 'tool': tool, 'arguments': arguments or {}}] if tool else [],
            'protocol': 'openai_chat_completions', 'private': {}, 'blocks': None,
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


class Assets:
    def get(self, workspace, identifier):
        return SimpleNamespace(asset_id=identifier, revision=1, blob_hash=digest(identifier))


class Sets:
    def create(self, workspace, items, asset_set_id):
        return SimpleNamespace(asset_set_id=asset_set_id, revision=1, items=items,
                               manifest_hash=digest([item.model_dump() for item in items]))


class ScreenWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RUN.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=RUN)
        self.root = Path(self.tmp.name)
        self.workspace = WorkspaceStore(self.root / 'workspace.sqlite3')
        self.workspace.create_workspace('screen wait fixture', workspace_id='wtest')
        self.services = SimpleNamespace(workspace_store=self.workspace, asset_library=(Assets(), Sets(), None))
        self.state = {'pending': True, 'status': 'running', 'posts': 0, 'actions': []}
        self.observed = []
        def settle(ref, ctx):
            if self.state['pending']:
                return None
            return AgentToolResult(status='success', summary='整批已结束', data={'batch_id': ref['object_id']},
                coverage={'total':240,'completed':240,'pending':0}).model_dump()
        self.lifecycle = patch.dict(ADAPTERS, {('ocr','screen_batch'): Lifecycle(
            read=lambda ref,ctx:self.state.copy(), settle=settle, cancel=lambda ref,ctx:{'status':'cancelled'},
            progress=lambda ref,ctx:{'completed':0 if self.state['pending'] else 240,'total':240}, prepare=None)})
        self.lifecycle.start()

    def tearDown(self):
        self.lifecycle.stop()
        self.tmp.cleanup()

    def test_thousand_input_exception_summary_is_bounded_and_marks_omissions(self):
        value={'batch_id':'batchA','counts':{'total':1000,'needs_review':1000,'failed':0,'pending':0},
            'items':[{'input_version_id':f'version{index}','status':'needs_review',
                'issues':[{'code':'difference','message':'疑点原文'*500,'reason':'重复原文'*500} for _ in range(8)]}
                for index in range(30)]}
        summary=compact(value)
        self.assertEqual(summary['exception_count'],1000)
        self.assertTrue(summary['exceptions_preview_only'])
        self.assertEqual(len(summary['exceptions']),5)
        self.assertLess(len(json.dumps(summary,ensure_ascii=False).encode('utf-8')),7000)

    def test_compressed_discovery_does_not_displace_real_batch_and_artifact_references(self):
        messages=[{'role':'assistant','calls':[{'call_id':'discover','tool':'tools_list','arguments':{'family':'artifacts'}},
            {'call_id':'batch','tool':'ocr_screen_batch_run','arguments':{'asset_set_id':'setA','asset_set_revision':1}}]},
            {'role':'tool','call_id':'discover','result':AgentToolResult(status='success',summary='目录',
                data={'tools':[{'name':'example','description':'目录文案'*500} for _ in range(30)]}).model_dump()},
            {'role':'tool','call_id':'batch','result':AgentToolResult(status='success',summary='批次已完成',
                job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchA',revision=1,application='ocr',
                    coverage={'kind':'screen_batch'})],artifact_refs=[AgentRef(kind='artifact',workspace_id='wtest',
                    object_id='bundleA',revision=1,application='ocr',parent_id='batchA')]).model_dump()}]
        original=json.dumps(messages,ensure_ascii=False)
        summary=_history_summary(messages)['content']
        self.assertIn('batchA',summary)
        self.assertIn('bundleA',summary)
        self.assertIn('omitted_discovery_calls',summary)
        self.assertIn('session_read_history',summary)
        self.assertNotIn('目录文案',summary)
        self.assertEqual(json.dumps(messages,ensure_ascii=False),original)

    def test_repeated_screen_risks_keep_one_full_copy_and_all_operation_counts_and_references(self):
        messages=[]
        for index,tool in enumerate(('ocr_screen_batch_run','ocr_screen_batch_control')):
            messages.extend([{'role':'assistant','calls':[{'call_id':str(index),'tool':tool,'arguments':{'batch_id':'batchA'}}]},
                {'role':'tool','call_id':str(index),'result':AgentToolResult(status='success',summary='回执',
                    data={'batch_id':'batchA','counts':{'total':8,'pending':6-index},'policy':{'primary_engine':'hunyuan'},
                        'exceptions':[{'input_version_id':'imageA','issues':[{'code':'uncertain','message':'需要保留的疑点'}]}]},
                    job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchA',revision=1)]).model_dump()}])
        original=json.dumps(messages,ensure_ascii=False)
        records=json.loads(_history_summary(messages)['content'].split('\n',1)[1])
        self.assertEqual(records[0]['data']['exceptions'][0]['issues'][0]['message'],'需要保留的疑点')
        self.assertEqual(records[1]['data']['repeated_exception_refs'][0]['input_version_id'],'imageA')
        self.assertEqual([record['data']['counts']['pending'] for record in records],[6,5])
        self.assertTrue(all(record['job_refs'][0]['object_id']=='batchA' for record in records))
        self.assertEqual(json.dumps(messages,ensure_ascii=False),original)

    def test_sending_fact_summary_omits_empty_defaults_but_keeps_zero_offsets_errors_and_evidence(self):
        receipt=AgentToolResult(status='error',summary='真实失败',data={'pending':0},coverage={'total':8,'pending':0},
            warnings=['实际疑点'],error={'code':'native_failed','message':'未完成'},next_offset=0,
            job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchA',revision=1)]).model_dump()
        messages=[{'role':'assistant','calls':[{'call_id':'A','tool':'ocr_screen_batch_status','arguments':{'batch_id':'batchA'}}]},
            {'role':'tool','call_id':'A','result':receipt}]
        original=json.dumps(messages,ensure_ascii=False)
        fact=json.loads(_history_summary(messages)['content'].split('\n',1)[1])[0]
        self.assertNotIn('artifact_refs',fact)
        self.assertNotIn('result_refs',fact)
        for key in ('job_refs','coverage','warnings','error','next_offset'):
            self.assertEqual(fact[key],receipt[key])
        self.assertEqual(fact['arguments'],{'batch_id':'batchA'})
        self.assertEqual(json.dumps(messages,ensure_ascii=False),original)

    def test_sending_fact_summary_references_exact_duplicate_delivery_text_without_rewriting_command(self):
        text='真实部分交付范围及未完成说明'*30
        messages=[{'role':'assistant','calls':[{'call_id':'A','tool':'delivery_publish','arguments':{'summary':text}}]},
            {'role':'tool','call_id':'A','result':AgentToolResult(status='success',summary=text).model_dump()}]
        original=json.dumps(messages,ensure_ascii=False)
        fact=json.loads(_history_summary(messages)['content'].split('\n',1)[1])[0]
        self.assertEqual(fact['summary_ref'],'arguments.summary')
        self.assertEqual(fact['arguments']['summary'],text)
        self.assertEqual(json.dumps(messages,ensure_ascii=False),original)

    def test_terminal_screen_delivery_rejects_pending_stale_export_scope_and_ordinary_jobs(self):
        receipt=AgentToolResult(status='success',summary='处理已结束',coverage={'total':8,'completed':0,'needs_review':8,'pending':0},
            job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchA',revision=1,application='ocr',coverage={'kind':'screen_batch'})]).model_dump()
        state={'messages':[{'role':'tool','call_id':'A','result':receipt}],'background_jobs':[]}
        delivery=AgentToolResult(status='success',summary='候选文件已交付',data={'verified':True,
            'screen_batch_coverage':[{'batch_id':'batchA','total':8,'file_count':8,'pending':0}]}).model_dump()
        self.assertTrue(terminal_screen_delivery(state,delivery))
        wrong=json.loads(json.dumps(delivery));wrong['data']['screen_batch_coverage'][0]['file_count']=2
        self.assertFalse(terminal_screen_delivery(state,wrong))
        wrong=json.loads(json.dumps(delivery));wrong['data']['screen_batch_coverage'][0]['pending']=6
        self.assertFalse(terminal_screen_delivery(state,wrong))
        wrong=json.loads(json.dumps(delivery));wrong['data']['screen_batch_coverage'][0]['total']=7
        self.assertFalse(terminal_screen_delivery(state,wrong))
        wrong=json.loads(json.dumps(state));wrong['background_jobs']=[{'operation_id':'original'}]
        self.assertFalse(terminal_screen_delivery(wrong,delivery))
        wrong=json.loads(json.dumps(state));wrong['messages'][0]['result']['coverage']['pending']=1
        self.assertFalse(terminal_screen_delivery(wrong,delivery))
        wrong=json.loads(json.dumps(state));wrong['messages'][0]['result']['job_refs'][0]['application']='data'
        self.assertFalse(terminal_screen_delivery(wrong,delivery))

    async def test_verified_final_screen_zip_ends_without_a_redundant_controller_round(self):
        bundle={'bundle_id':'bundleA','handoff_id':'batchA','files':[{'name':'screen.zip'}],
            'items':[{'input_version_id':'imageA','status':'needs_review','file_name':'imageA.xlsx'}]}
        self.services.artifact_center=SimpleNamespace(
            file=lambda *args:SimpleNamespace(artifact_id='artifactA',revision=1,blob_hash='a'*64),
            store=SimpleNamespace(stage_download=lambda *args,**kwargs:nullcontext()))
        async def provider(state,schemas):
            self.observed.append(state['step'])
            if state['step']==0:return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
            if state['step']==1:return response(state,'ocr_screen_batch_export',{'batch_id':'batchA','include_pending_review':True})
            if state['step']==2:return response(state,'delivery_publish',{'summary':'整批候选文件已核实，未经人工确认',
                'artifact_refs':state['messages'][-1]['result']['artifact_refs']})
            self.fail('Verified final screen conversion must not request another model round')
        def settle(ref,ctx):
            if self.state['pending']:return None
            return AgentToolResult(status='success',summary='已结束，待复核1',coverage={'total':1,'completed':0,'needs_review':1,'pending':0}).model_dump()
        with patch.dict(ADAPTERS,{('ocr','screen_batch'):Lifecycle(read=lambda ref,ctx:{},settle=settle,cancel=lambda ref,ctx:{},progress=lambda ref,ctx:{})}):
            runtime=await self.runtime(provider)
            def export(args,ctx):
                return AgentToolResult(status='success',summary='已生成真实来源候选ZIP',data={'bundle':bundle},
                    artifact_refs=[AgentRef(kind='artifact',workspace_id='wtest',object_id='bundleA',revision=1,application='ocr',parent_id='batchA')]).model_dump()
            runtime.registry.tools['ocr_screen_batch_export']=replace(runtime.registry.tools['ocr_screen_batch_export'],execute=export,availability=None,resource_keys=None)
            session=runtime.store.create_session('wtest','final screen',[],'session_request')
            with patch('workbench_platform.application_services.references.verify',return_value=bundle):
                try:
                    run=await runtime.submit(session['id'],'整批转Excel，包含未经人工确认文件','run_request')
                    await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                    self.state['pending']=False
                    await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='partial',runtime)
                    self.assertEqual(self.observed,[0,1,2])
                    self.assertEqual(self.state['posts'],1)
                finally:await runtime.close()

    async def wait_for(self, predicate, runtime, seconds=90):
        async with asyncio.timeout(seconds):
            while not predicate():
                if runtime.store.rows("SELECT id FROM runs WHERE status IN ('failed','needs_verification')"):
                    self.fail(str(runtime.store.rows('SELECT * FROM runs')))
                await asyncio.sleep(.03)

    async def runtime(self, provider, root=None):
        runtime = AgentRuntime(root or self.root / 'agent', self.services, model_provider=provider)
        self.services.agent = runtime
        def submit(args,ctx):
            self.state['posts'] += 1
            return AgentToolResult(status='pending', summary='已提交240张，仍在后台处理',
                data={'batch_id':'batchA'}, job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchA',
                    application='ocr',native_id='batchA',parent_id='handoffA',revision=1,
                    coverage={'kind':'screen_batch'})]).model_dump()
        def action(args,ctx):
            self.state['actions'].append(args.action)
            self.state['status'] = 'paused' if args.action == 'pause' else 'running'
            if args.action == 'cancel':
                self.state.update(pending=False,status='cancelled')
            return AgentToolResult(status='success',summary='已控制原批次',data={'batch_id':args.batch_id,'status':self.state['status']}).model_dump()
        for name,execute in [('ocr_screen_batch_run',submit),('ocr_screen_batch_control',action)]:
            runtime.registry.tools[name] = replace(runtime.registry.tools[name], execute=execute, availability=None,
                                                   resource_keys=None, lookup_operation=None)
        await runtime.start()
        return runtime

    def pending_operation(self,runtime,run):
        return runtime.store.rows("SELECT * FROM operations WHERE run_id=? AND tool='ocr_screen_batch_run'",(run['id'],))[0]

    async def test_midrun_pause_survives_restart_without_resubmit_or_model_polling(self):
        async def provider(state,schemas):
            self.observed.append(state['step'])
            step=state['step']
            if step==0:
                self.assertNotIn('ocr_screen_batch_control',state['visible'])
                return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
            if step==1:
                self.assertIn('ocr_screen_batch_control',state['visible'])
                self.assertIn('ocr_screen_batch_control',state['protected_tools'])
                self.assertEqual([m['content'] for m in state['messages'] if m.get('id')=='pause_request'],['暂停原批次'])
                return response(state,'ocr_screen_batch_control',{'batch_id':'batchA','action':'pause'})
            if step==2:
                return response(state,'conversation_answer',{'content':'原批次已暂停，现有结果保留'})
            if step==3:
                self.assertEqual([m['content'] for m in state['messages'] if m.get('id')=='resume_request'],['继续原批次'])
                return response(state,'ocr_screen_batch_control',{'batch_id':'batchA','action':'resume'})
            if step==4:
                return response(state,'conversation_answer',{'content':'原批次已继续'})
            self.assertEqual(step,5)
            original=next(m for m in state['messages'] if m.get('call_id')=='call_0' and m['role']=='tool')
            self.assertEqual(original['result']['coverage']['pending'],0)
            return response(state,'conversation_answer',{'content':'整批已结束，可下载逐图文件'})
        runtime=await self.runtime(provider)
        session=runtime.store.create_session('wtest','240 images',[],'session_request')
        run=await runtime.submit(session['id'],'整批转Excel','run_request')
        reopened=None
        try:
            await self.wait_for(lambda: runtime.store.run(run['id'])['status']=='waiting_job',runtime)
            await asyncio.sleep(.7)
            self.assertEqual(self.observed,[0])
            await runtime.submit(session['id'],'暂停原批次','pause_request')
            await self.wait_for(lambda: self.state['actions']==['pause'] and runtime.store.run(run['id'])['status']=='waiting_job',runtime)
            self.assertEqual(self.observed,[0,1,2])
            self.assertEqual(self.pending_operation(runtime,run)['state'],'pending')
            snapshot=await runtime.graph.aget_state(runtime.options(run['id']))
            self.assertEqual(len(snapshot.values['background_jobs']),1)
            await runtime.close()
            reopened=await self.runtime(provider,runtime.store.root)
            await asyncio.sleep(.7)
            self.assertEqual(self.observed,[0,1,2])
            await reopened.submit(session['id'],'继续原批次','resume_request')
            await self.wait_for(lambda: self.state['actions']==['pause','resume'] and reopened.store.run(run['id'])['status']=='waiting_job',reopened)
            await asyncio.sleep(.7)
            self.assertEqual(self.observed,[0,1,2,3,4])
            self.state.update(pending=False,status='completed')
            await self.wait_for(lambda: reopened.store.run(run['id'])['status']=='answered',reopened)
            self.assertEqual(self.observed,[0,1,2,3,4,5])
            self.assertEqual(self.state['posts'],1)
            self.assertEqual(self.pending_operation(reopened,run)['state'],'finished')
            self.assertEqual(len(reopened.store.rows('SELECT id FROM inbox WHERE run_id=?',(run['id'],))),2)
        finally:
            if reopened:await reopened.close()
            else:await runtime.close()

    async def test_original_screen_wait_keeps_controller_idle_and_fences_cancel(self):
        async def provider(state,schemas):
            self.observed.append(state['step'])
            return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
        runtime=await self.runtime(provider)
        session=runtime.store.create_session('wtest','screen',[],'session_request')
        try:
            run=await runtime.submit(session['id'],'整批转Excel','run_request')
            await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='waiting_job',runtime)
            await asyncio.sleep(.8)
            self.assertEqual(self.observed,[0])
            runtime.cancel(run['id'])
            self.state.update(pending=False,status='completed')
            await asyncio.sleep(.7)
            self.assertEqual(runtime.store.run(run['id'])['status'],'cancelled')
            self.assertEqual(self.observed,[0])
            self.assertEqual(self.state['posts'],1)
        finally:await runtime.close()

    async def test_other_job_families_still_wait_for_native_settlement_before_reading_inbox(self):
        async def provider(state,schemas):
            self.observed.append(state['step'])
            if state['step']==0:
                return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
            self.assertEqual(state.get('background_jobs',[]),[])
            self.assertTrue(any(m.get('id')=='later_goal' for m in state['messages']))
            return response(state,'conversation_answer',{'content':'已在原任务结束后读取追加目标'})
        runtime=await self.runtime(provider)
        original=runtime.registry.tools['ocr_screen_batch_run'].execute
        def other_job(args,ctx):
            value=original(args,ctx)
            value['job_refs'][0].update(application='data',coverage={'kind':'generic_fixture'})
            return value
        runtime.registry.tools['ocr_screen_batch_run']=replace(runtime.registry.tools['ocr_screen_batch_run'],execute=other_job)
        session=runtime.store.create_session('wtest','other native job',[],'session_request')
        with patch.dict(ADAPTERS,{('data','generic_fixture'):ADAPTERS[('ocr','screen_batch')]}):
            try:
                run=await runtime.submit(session['id'],'原任务','run_request')
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                await runtime.submit(session['id'],'追加目标','later_goal')
                await asyncio.sleep(.8)
                self.assertEqual(self.observed,[0])
                self.state['pending']=False
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='answered',runtime)
                self.assertEqual(self.observed,[0,1])
            finally:await runtime.close()

    async def test_original_and_upgrade_settle_independently_across_restart_without_extra_model_rounds(self):
        pending={'batchA':True,'batchB':True}
        upgraded=[]
        def settle(ref,ctx):
            if pending[ref['object_id']]:return None
            return AgentToolResult(status='success',summary='真实批次已结束',data={'batch_id':ref['object_id']},
                coverage={'total':1,'completed':1,'pending':0}).model_dump()
        def upgrade(args,ctx):
            upgraded.append(args.batch_id)
            return AgentToolResult(status='pending',summary='所选图片提高精度仍在后台',data={'batch_id':'batchB'},
                job_refs=[AgentRef(kind='job',workspace_id='wtest',object_id='batchB',native_id='batchB',
                    parent_id='handoffB',revision=1,application='ocr',coverage={'kind':'screen_batch'})]).model_dump()
        async def provider(state,schemas):
            self.observed.append(state['step'])
            if state['step']==0:return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
            if state['step']==1:return response(state,'ocr_screen_batch_control',{'batch_id':'batchA','action':'upgrade','version_ids':['imageA']})
            if state['step']==2:return response(state,'conversation_answer',{'content':'原批次与提高精度任务均继续保留'})
            self.assertEqual(state['background_jobs'],[])
            self.assertTrue(state['messages'][-1]['content'].startswith('[服务器后台屏拍批次已结束'))
            original=[m['result'] for m in state['messages'] if m['role']=='tool' and m['result'].get('data',{}).get('batch_id')=='batchA']
            self.assertTrue(any(item.get('coverage',{}).get('pending')==0 for item in original))
            return response(state,'conversation_answer',{'content':'两项真实批次均已结束'})
        with patch.dict(ADAPTERS,{('ocr','screen_batch'):Lifecycle(read=lambda ref,ctx:{'status':'running'},
            settle=settle,cancel=lambda ref,ctx:{},progress=lambda ref,ctx:{})}):
            runtime=await self.runtime(provider)
            runtime.registry.tools['ocr_screen_batch_control']=replace(runtime.registry.tools['ocr_screen_batch_control'],execute=upgrade)
            session=runtime.store.create_session('wtest','two batches',[],'session_request')
            root=runtime.store.root
            try:
                run=await runtime.submit(session['id'],'整批转Excel','run_request')
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                await runtime.submit(session['id'],'所选图片提高精度，原批次继续','upgrade_request')
                await self.wait_for(lambda:self.observed==[0,1] and runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                await runtime.submit(session['id'],'保留两项后台作业，回复当前指令即可','keep_request')
                await self.wait_for(lambda:self.observed==[0,1,2] and runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                pending['batchA']=False
                async with asyncio.timeout(90):
                    while True:
                        snapshot=await runtime.graph.aget_state(runtime.options(run['id']))
                        if len(snapshot.values.get('background_jobs',[]))==1 and runtime.store.run(run['id'])['status']=='waiting_job':break
                        await asyncio.sleep(.03)
                self.assertEqual(snapshot.values['background_jobs'][0]['result']['job_refs'][0]['object_id'],'batchB')
                await asyncio.sleep(.7)
                self.assertEqual(self.observed,[0,1,2])
                await runtime.close()
                runtime=await self.runtime(provider,root)
                runtime.registry.tools['ocr_screen_batch_control']=replace(runtime.registry.tools['ocr_screen_batch_control'],execute=upgrade)
                pending['batchB']=False
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='answered',runtime)
                self.assertEqual(self.observed,[0,1,2,3])
                self.assertEqual(self.state['posts'],1)
                self.assertEqual(upgraded,['batchA'])
                states=runtime.store.rows("SELECT state FROM operations WHERE run_id=? AND tool IN ('ocr_screen_batch_run','ocr_screen_batch_control')",(run['id'],))
                self.assertTrue(all(item['state']=='finished' for item in states))
            finally:await runtime.close()

    async def test_partial_export_returns_verified_delivery_and_original_batch_keeps_waiting(self):
        bundle={'bundle_id':'bundleA','files':[{'name':'screen.zip'}]}
        self.services.artifact_center=SimpleNamespace(
            file=lambda *args:SimpleNamespace(artifact_id='artifactA',revision=1,blob_hash='a'*64),
            store=SimpleNamespace(stage_download=lambda *args,**kwargs:nullcontext()))
        async def provider(state,schemas):
            self.observed.append(state['step'])
            step=state['step']
            if step==0:return response(state,'ocr_screen_batch_run',{'link_id':'linkA','asset_set_id':'setA','asset_set_revision':1})
            if step==1:return response(state,'ocr_screen_batch_export',{'batch_id':'batchA'})
            if step in {2,4}:
                previous=state['messages'][-1]['result']
                return response(state,'delivery_publish',{'summary':'已交付部分文件，剩余图片继续处理' if step==2 else '整批文件已核实',
                    'artifact_refs':previous['artifact_refs'],'allow_partial_screen_batch':step==2})
            if step==3:
                notification=state['messages'][-1]
                self.assertTrue(notification['content'].startswith('[服务器后台屏拍批次已结束'))
                self.assertIn('batchA',notification['content'])
                self.assertEqual(state['background_jobs'],[])
                self.assertNotIn('ocr_screen_batch_control',state['protected_tools'])
                return response(state,'ocr_screen_batch_export',{'batch_id':'batchA'})
            return response(state)
        runtime=await self.runtime(provider)
        def export(args,ctx):
            return AgentToolResult(status='success',summary='已生成ZIP',data={'bundle':bundle},
                artifact_refs=[AgentRef(kind='artifact',workspace_id='wtest',object_id='bundleA',revision=1,application='ocr',parent_id='batchA')]).model_dump()
        runtime.registry.tools['ocr_screen_batch_export']=replace(runtime.registry.tools['ocr_screen_batch_export'],
            execute=export,availability=None,resource_keys=None)
        session=runtime.store.create_session('wtest','partial export',[],'session_request')
        with patch('workbench_platform.application_services.references.verify',return_value=bundle):
            try:
                run=await runtime.submit(session['id'],'整批转Excel','run_request')
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                await runtime.submit(session['id'],'先导出已完成的文件，其余继续','export_now')
                await self.wait_for(lambda:len(self.observed)>=3 and runtime.store.run(run['id'])['status']=='waiting_job',runtime)
                receipt=json.loads(runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='delivery_publish'",(run['id'],))[0]['result'])
                self.assertEqual(receipt['data']['completeness'],'partial')
                self.assertEqual(receipt['data']['continuing_job_refs'][0]['object_id'],'batchA')
                self.assertEqual(self.pending_operation(runtime,run)['state'],'pending')
                await asyncio.sleep(.7)
                self.assertEqual(self.observed,[0,1,2])
                self.state['pending']=False
                await self.wait_for(lambda:runtime.store.run(run['id'])['status']=='succeeded',runtime)
                self.assertEqual(self.observed,[0,1,2,3,4,5])
                self.assertEqual(self.state['posts'],1)
            finally:await runtime.close()

    async def test_1000_selected_assets_are_fixed_and_bounded_in_initial_prompt(self):
        async def provider(state,schemas):return response(state)
        runtime=await self.runtime(provider)
        try:
            selected=['image'+str(i) for i in range(1000)]
            session=runtime.store.create_session('wtest','1000 images',selected,'session_request')
            run,_=runtime.store.start_run(session['id'],'转Excel','run_request',0)
            state=runtime.initial_state(run,'转Excel')
            scope=json.loads(next(m['content'] for m in state['messages'] if m['id'].endswith(':scope')).split('\n',1)[1])
            self.assertEqual(scope['selected_asset_count'],1000)
            self.assertEqual(len(scope['selected_assets']),10)
            self.assertEqual(scope['selected_asset_set']['item_count'],1000)
            self.assertLess(len(json.dumps(scope)),3000)
            again=scope_summary(runtime.context(state))
            self.assertEqual(again['selected_asset_set']['asset_set_id'],scope['selected_asset_set']['asset_set_id'])
            # Fixed current scope works without an LLM emitting 1000 IDs.
            result=await runtime.registry.dispatch({'call_id':'fixed_scope','tool':'assets_create_set','arguments':{'selected_scope':True}},runtime.context(state))
            self.assertEqual(result['status'],'success')
            self.assertEqual(result['data']['item_count'],1000)
            self.assertLess(len(json.dumps(result)),3000)
        finally:await runtime.close()


class ScreenFastpathTests(unittest.TestCase):
    def test_invalid_controller_shape_identifies_empty_response_without_content_or_credentials(self):
        raw={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':None,
            'reasoning_content':'private-token https://private.example'}}],
            'usage':{'prompt_tokens':123,'completion_tokens':0},'private_url':'https://private.example'}
        with self.assertRaises(ProviderFault) as caught:normalize_response('openai_chat_completions',raw,'messageA')
        diagnostic=caught.exception.response_diagnostics
        self.assertEqual(diagnostic['parse_category'],'empty_response')
        self.assertEqual(diagnostic['finish_reason'],'stop')
        self.assertEqual(diagnostic['usage']['prompt_tokens'],123)
        self.assertNotIn('private-token',json.dumps(diagnostic))
        self.assertNotIn('private.example',json.dumps(diagnostic))

    def setUp(self):
        self.asset=SimpleNamespace(asset_id='imageA',revision=1,blob_hash='a'*64,
                                   media_type='image/png',original_name='image.png')
        self.fixed=SimpleNamespace(asset_set_id='setA',revision=1,items=[SimpleNamespace(
            asset_id='imageA',asset_revision=1,blob_hash='a'*64)])
        self.links=[SimpleNamespace(app_id='ocr',role='editor',link_id='linkA',module_project_id='projectA')]
        self.state={'projects':[{'id':'projectA'}]}
        self.services=SimpleNamespace(screen_batches=object(),
            asset_library=(SimpleNamespace(get=lambda *a:self.asset),None,None),
            workspace_store=SimpleNamespace(list_links=lambda *a:self.links),
            job_service=SimpleNamespace(_context=lambda *a:SimpleNamespace(module_project_id='projectA'),
                _adapter=lambda *a:SimpleNamespace(_request=lambda *a:self.state,
                    capabilities=lambda:SimpleNamespace(recognition=True))))
        self.ctx=SimpleNamespace(services=self.services,workspace_id='workspaceA')
        self.availability=patch('workbench_platform.application_services.native.availability',return_value={'state':'ready'})
        self.probe=self.availability.start()
        self.addCleanup(self.availability.stop)

    def test_live_unique_project_and_current_image_set_have_verified_fastpath(self):
        hint=screen_fastpath(self.ctx,self.fixed)
        self.assertTrue(hint['verified'])
        self.assertEqual(hint['link_id'],'linkA')
        self.assertEqual(hint['asset_set_id'],'setA')
        self.assertFalse(hint['requires_prepare'])

    def test_inactive_ambiguous_readonly_or_missing_project_require_preparation(self):
        self.probe.return_value={'state':'preparable'}
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.probe.return_value={'state':'ready'}
        self.links*=2
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.links=self.links[:1];self.links[0].role='viewer'
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.links[0].role='editor';self.state['projects']=[]
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))

    def test_changed_revision_nonimage_empty_scope_and_unavailable_service_are_not_hints(self):
        self.asset.revision=2
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.asset.revision=1;self.asset.media_type='application/pdf';self.asset.original_name='file.pdf'
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.asset.media_type='image/png';self.asset.original_name='image.png';self.fixed.items=[]
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))
        self.fixed.items=[SimpleNamespace(asset_id='imageA',asset_revision=1,blob_hash='a'*64)]
        self.probe.side_effect=ValueError('service disconnected')
        self.assertIsNone(screen_fastpath(self.ctx,self.fixed))

