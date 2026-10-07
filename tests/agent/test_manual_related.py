"""Manual discovery shows task-related results before explicit project expansion."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
import test_data_tools as data_tests
from workbench_platform.application_services.manual import candidates


class RelatedResultsTests(unittest.IsolatedAsyncioTestCase):
    setUp=data_tests.DataToolTests.setUp
    tearDown=data_tests.DataToolTests.tearDown
    asset=data_tests.DataToolTests.asset
    call=data_tests.DataToolTests.call
    create=data_tests.DataToolTests.create
    import_asset=data_tests.DataToolTests.import_asset
    async def test_task_parent_selects_current_version_and_project_expansion_keeps_other_results(self):
        first_asset=self.asset('suppliers-left.csv')
        second_asset=self.asset('suppliers-right.csv')
        first=await self.create(); await self.import_asset(first,'left',first_asset)
        second=await self.create(); await self.import_asset(second,'left',second_asset)
        self.services.agent=SimpleNamespace(tasks=SimpleNamespace(get=lambda _: {
            'initial_refs':[{'kind':'table','application':'data','object_id':'old_version','parent_id':first}],
            'result_refs':[],'artifact_refs':[],'job_refs':[]}))
        scoped=candidates(self.ctx,related_only=True)
        expanded=candidates(self.ctx)
        self.assertTrue(scoped['items'])
        self.assertEqual({item['reference']['parent_id'] for item in scoped['items']},{first})
        self.assertTrue(all(item['related_to_task'] for item in scoped['items']))
        self.assertEqual({item['reference']['parent_id'] for item in expanded['items']},{first,second})
        # Discovery alone never changes the task's selected assets or starts a
        # new run; return POST still protects exact references and added scope.
        self.assertEqual(self.store.session(self.ctx.session_id)['selected_assets'],[])
        self.assertEqual(len(self.store.rows('SELECT id FROM runs')),1)
        selected=replace(self.ctx,selected_assets=(second_asset.asset_id,))
        by_source=candidates(selected,related_only=True)
        self.assertTrue(any(item['reference']['parent_id']==second for item in by_source['items']))
