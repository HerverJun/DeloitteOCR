"""Detail actions reflect durable execution costs and live lineage reservations.

The real Store, ScreenBatches and resolver run with CPU fixture observations.
Elapsed counters are controlled costs, not model speed or accuracy evidence.
"""
from copy import deepcopy
from pathlib import Path
import runpy
import unittest

from ocr_workbench.screen_resolution import resolve_screen_issues


class ScreenBudgetDiagnosticTests(unittest.TestCase):
    def setUp(self):
        module=runpy.run_path(str(Path(__file__).with_name('test_screen_lineage_budget.py')))
        self.fixture=module['LineageBudgetTests']('runTest');self.fixture.setUp()
        self.manager=self.fixture.manager;self.store=self.fixture.store;self.project=self.fixture.project

    def tearDown(self):self.fixture.tearDown()

    def diagnostic(self,batch,version):
        detail=self.manager.detail(self.project,batch,version)
        issue=next(item for item in detail['diagnostics']['items'] if item['code']=='missing_observed_columns')
        action=next(item for item in issue['applicable_actions'] if item['action']=='precision_subbatch')
        return detail,action

    def children(self,batch_budget):
        one,two=self.fixture.image(),self.fixture.image()
        batch=self.fixture.batch([one,two],batch_budget_seconds=batch_budget)
        self.manager.step();self.fixture.finish(batch,one,10);self.fixture.finish(batch,two,10)
        first=resolve_screen_issues(self.manager,self.project,batch,
            self.fixture.body(batch,one,request='diagnostic-child-one',budget=60))
        second=resolve_screen_issues(self.manager,self.project,batch,
            self.fixture.body(batch,two,request='diagnostic-child-two',budget=60))
        self.assertEqual((first['status'],second['status']),('submitted','submitted'))
        self.assertNotEqual(first['batch_id'],second['batch_id'])
        return batch,one,two,first,second

    def test_retry_carried_cost_disables_precision_instead_of_showing_a_fresh_allowance(self):
        version=self.fixture.image()
        batch=self.fixture.batch([version],budget_seconds=100,request_timeout_seconds=100)
        self.manager.step();self.fixture.finish(batch,version,60)
        prior=deepcopy(self.manager.detail(self.project,batch,version)['result'])
        self.manager.model_identity='controlled-new-model-identity-for-retry'
        self.manager._update(batch,version,status='failed')
        self.manager.action(self.project,batch,{'action':'retry_failed','request_id':'diagnostic-carried-retry'})
        self.manager.step();self.fixture.finish(batch,version,35)
        detail,action=self.diagnostic(batch,version)
        budget=detail['diagnostics']['execution_budget']
        self.assertEqual(detail['attempt'],1)
        self.assertEqual(budget['root_batch_id'],batch)
        self.assertGreaterEqual(budget['local_input_actual_seconds'],95)
        self.assertGreaterEqual(budget['source_actual_seconds'],95)
        self.assertGreaterEqual(budget['remaining_seconds'],0)
        self.assertLessEqual(budget['remaining_seconds'],5)
        self.assertEqual(detail['diagnostics']['budget']['remaining_seconds'],budget['remaining_seconds'])
        self.assertTrue(action['capability_available'])
        self.assertFalse(action['enabled'])
        self.assertEqual(action['reason'],'remaining_budget_insufficient')
        self.assertEqual(self.store.result(prior['id']),prior)

    def test_detail_subtracts_precision_child_and_sibling_reservations(self):
        batch,one,_,first,second=self.children(160)
        prior=deepcopy(self.manager.detail(self.project,batch,one)['result'])
        before=self.manager.delivery_snapshot(self.project,batch)
        detail,action=self.diagnostic(batch,one)
        budget=detail['diagnostics']['execution_budget']
        reserved=first['budget']['reserved_seconds']+second['budget']['reserved_seconds']
        self.assertEqual(reserved,120)
        self.assertAlmostEqual(budget['inflight_reserved_seconds'],reserved,delta=.01)
        self.assertGreaterEqual(budget['actual_seconds'],20)
        self.assertGreaterEqual(budget['remaining_seconds'],15)
        self.assertLessEqual(budget['remaining_seconds'],20)
        self.assertLessEqual(budget['actual_seconds']+reserved+budget['remaining_seconds'],160.001)
        self.assertGreaterEqual(budget['source_actual_seconds'],10)
        self.assertTrue(action['enabled'])
        self.assertEqual(action['budget_seconds'],budget['remaining_seconds'])
        self.assertEqual(self.store.result(prior['id']),prior)
        self.assertEqual(self.manager.delivery_snapshot(self.project,batch),before)

    def test_insufficient_root_budget_from_siblings_disables_precision_but_retains_diagnosis(self):
        batch,one,_,first,second=self.children(145)
        detail,action=self.diagnostic(batch,one)
        budget=detail['diagnostics']['execution_budget']
        self.assertEqual(first['budget']['reserved_seconds']+second['budget']['reserved_seconds'],120)
        self.assertGreaterEqual(budget['remaining_seconds'],0)
        self.assertLessEqual(budget['remaining_seconds'],5)
        self.assertTrue(action['capability_available'])
        self.assertFalse(action['enabled'])
        self.assertEqual(action['reason'],'remaining_budget_insufficient')
        self.assertEqual(action['budget_seconds'],budget['remaining_seconds'])
        self.assertGreater(len(detail['diagnostics']['items']),0)
        self.assertFalse(detail['human_confirmed'])


if __name__=='__main__':unittest.main()
