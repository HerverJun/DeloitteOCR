"""Agent cold backup integrity and relocation, including startup-only state."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from dataclasses import replace
from scripts.backup_workspace import create, restore, verify
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.tools import results
from workbench_platform.application_services.references import asset_allowed
import test_graph as graph_tests


class AgentBackupTests(graph_tests.GraphTests):
    test_native_wait_does_not_call_model_until_result_and_delivery_is_readable = None
    test_bound_question_survives_close_reopen_and_changes_next_tool = None
    test_stop_fences_late_controller_response = None
    test_no_delivery_cannot_be_marked_success = None

    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 60

    async def test_both_stores_derived_sources_and_checkpoint_survive_relocation(self):
        root=Path(self.temporary.name)
        async def provider(state, schemas):
            if state['step'] == 0:
                return graph_tests.response(state,'ask_user',{'question':'保留哪张表？','options':['本期','上期']})
            result = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            self.assertEqual(result['data']['reply'], '上期')
            return graph_tests.response(state, 'conversation_answer', {'content': '已恢复原问题并采用上期。'})
        runtime,session=await self.make_runtime(provider)
        run=await runtime.submit(session['id'],'恢复检查','backup_run')
        await self.wait_status(runtime,run['id'],{'waiting_user'})
        decision = runtime.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))[0]
        await runtime.close()
        # Application stores are normally in root/agent. Use this runtime's
        # exact stores without changing their contents or original paths.
        import shutil
        shutil.copy2(runtime.store.path,root/'agent/operations.sqlite3')
        shutil.copytree(runtime.store.root/'harness',root/'agent/harness')
        store=AgentStore(root/'agent')
        (root/'asset-blobs').mkdir()
        self.assets.blob_root=root/'asset-blobs'
        asset=self.asset('suppliers-left.csv')
        uploads=root/'asset-uploads';uploads.mkdir();self.assets.roots['uploads']=uploads
        ctx=replace(self.ctx,store=store,operation_id='cold_bridge',selected_assets=(asset.asset_id,))
        value=results.install(b'id,amount\n000001,100.00\n','bridge.csv','text/csv',[],[asset.asset_id],{},ctx)
        derived=value['data']['asset_id']
        # Exercise relocation of the previous absolute-path representation.
        row=store.rows('SELECT * FROM derived_inputs WHERE asset_id=?',(derived,))[0]
        with store.transaction() as db:
            db.execute('UPDATE derived_inputs SET provenance_path=? WHERE asset_id=?',(str(store.root/row['provenance_path']),derived))
        archive=root.parent/(root.name+'-backup'); recovered=root.parent/(root.name+'-restored')
        try:
            manifest=create(root,archive)
            self.assertIn('agent/operations.sqlite3', manifest['files'])
            self.assertTrue(any(name.startswith('agent/harness/sessions/') and name.endswith('.jsonl') for name in manifest['files']))
            verify(archive)
            receipt=restore(archive,recovered)
            self.assertEqual(receipt['agent_paths_relocated'],1)
            from workbench_platform.assets.registry import AssetRegistry
            registry=AssetRegistry(recovered/'assets.sqlite3',recovered/'asset-blobs',{'uploads':recovered/'asset-uploads'})
            restored=AgentStore(recovered/'agent')
            services=replace_namespace(self.services,asset_library=(registry,self.sets,None))
            checked=replace(ctx,services=services,store=restored)
            self.assertTrue(asset_allowed(checked,derived))
            async_runtime=AgentRuntime(restored.root,services,model_provider=provider)
            services.agent=async_runtime
            await async_runtime.start()
            try:
                snapshot=await async_runtime.graph.aget_state(async_runtime.options(run['id']))
                self.assertTrue(snapshot.values)
                self.assertEqual(async_runtime.store.session(session['id'])['workspace_id'],'wtest')
                self.assertEqual(async_runtime.store.run(run['id'])['status'], 'waiting_user')
                self.assertEqual(async_runtime.store.rows('SELECT question FROM decisions WHERE id=?', (decision['id'],))[0]['question'], '保留哪张表？')
                await async_runtime.reply(decision['id'], '上期', 1, 'restored_bound_reply')
                final = await self.wait_status(async_runtime, run['id'], {'answered', 'failed'})
                self.assertEqual(final['status'], 'answered', async_runtime.store.events(session['id']))
                self.assertEqual(len(async_runtime.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))), 1)
            finally:
                await async_runtime.close()
            (recovered/'agent/bridges'/Path(row['provenance_path']).name).write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError):
                asset_allowed(checked,derived)
            # A retained legacy checkpoint is not a substitute for the new
            # canonical Harness history, even when the old file is valid SQLite.
            with closing(sqlite3.connect(root / 'agent/checkpoints.sqlite3')) as old:
                old.execute('CREATE TABLE legacy_evidence(id TEXT)')
                old.commit()
            log = next((root / 'agent/harness/sessions').rglob('session.v4.jsonl'))
            self.assertTrue(log.resolve().is_relative_to((root / 'agent/harness/sessions').resolve()))
            log.unlink()
            with self.assertRaisesRegex(ValueError, 'Harness backup requires one durable session log'):
                create(root, archive.with_name(archive.name + '-missing-harness'))
        finally:
            shutil.rmtree(archive,ignore_errors=True);shutil.rmtree(recovered,ignore_errors=True)

    async def test_settings_and_unstarted_runs_backup_allowed_but_legacy_execution_requires_evidence(self):
        root=Path(self.temporary.name)
        store=AgentStore(root/'agent')
        archive=root.parent/(root.name+'-settings-backup')
        startup_archive = archive.with_name(archive.name + '-startup')
        # setUp creates a run; remove only the isolated test records to model
        # initial settings without any graph execution.
        with store.transaction() as db:
            db.execute('DELETE FROM runs')
        import shutil
        try:
            create(root,archive)
            verify(archive)
            run, _ = store.start_run(self.ctx.session_id,'cannot omit execution history','new_run',0)
            create(root, startup_archive)
            verify(startup_archive)
            store.begin_model(run['id'], run['generation'], 0, 'original-request-fingerprint')
            with self.assertRaisesRegex(ValueError,'Legacy Agent backup requires'):
                create(root,archive.with_name(archive.name+'-invalid'))
        finally:
            shutil.rmtree(archive,ignore_errors=True)
            shutil.rmtree(startup_archive,ignore_errors=True)


def replace_namespace(value,**changes):
    from types import SimpleNamespace
    return SimpleNamespace(**{**vars(value),**changes})

if __name__=='__main__':
    unittest.main(verbosity=2)
