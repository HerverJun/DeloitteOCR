import json
import tempfile
import unittest

from ocr_workbench.agent.budgets import BudgetExceeded, increase
from ocr_workbench.agent.store import AgentStore
from ocr_workbench.store import Store, Conflict
import test_agent_operations


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = AgentStore(self.store)
        self.project = self.store.project("预算")['id']
        self.session = self.agent.create_session(self.project, {"client_request_id": "s", "title": "预算测试"})
        self.run = self.agent.create_run(self.project, self.session['id'], {"client_request_id": "r", "content": "test"},
                                         context={}, config={}, limits={"model_requests_per_run": 1, "tokens_per_run": 100})

    def begin(self, step=0, estimate=80):
        return self.agent.begin_model_request(self.project, self.run['id'], 1, step, "hash", estimated_tokens=estimate)

    def current(self):
        return self.agent.run(self.project, self.run['id'])

    def test_precharge_replay_at_limit_and_actual_reconciliation(self):
        record, fresh = self.begin()
        self.assertTrue(fresh)
        self.assertEqual(self.current()['usage']['tokens_charged'], 80)
        self.assertFalse(self.begin()[1])
        response = {"usage": {"input_tokens": 10, "output_tokens": 2}}
        self.agent.finish_model_request(self.project, self.run['id'], 1, record['request_id'], response)
        self.agent.finish_model_request(self.project, self.run['id'], 1, record['request_id'], response)
        usage = self.current()['usage']
        self.assertEqual((usage['tokens_charged'], usage['tokens_actual'], usage['tokens_estimated'], usage['tokens_unconfirmed']), (12, 12, 80, 0))
        self.assertFalse(self.begin()[1])
        with self.assertRaises(BudgetExceeded):
            self.begin(1)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests")), 1)

    def test_unknown_and_missing_usage_remain_charged(self):
        record, _ = self.begin()
        self.agent.finish_model_request(self.project, self.run['id'], 1, record['request_id'], {"usage": {}})
        self.assertEqual(self.current()['usage']['tokens_unconfirmed'], 80)
        increase(self.agent, self.project, self.run['id'], {"client_request_id": "b", "generation": 1, "limits": {"model_requests_per_run": 2}})
        with self.assertRaises(BudgetExceeded):
            self.begin(1, 30)
        self.begin(1, 20)
        self.agent.interrupt_on_startup()
        run = self.current()
        self.assertEqual(run['usage']['tokens_charged'], 100)
        row, fresh = self.agent.begin_model_request(self.project, run['id'], run['generation'], 1, "hash", estimated_tokens=20)
        self.assertFalse(fresh)
        self.assertEqual(row['state'], 'unknown')

    def test_increase_is_bound_idempotent_and_strict(self):
        body = {"client_request_id": "budget-1", "generation": 1, "limits": {"tokens_per_run": 200}}
        self.assertEqual(increase(self.agent, self.project, self.run['id'], body)['limits']['tokens_per_run'], 200)
        increase(self.agent, self.project, self.run['id'], body)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_events WHERE type='budget_updated'")), 1)
        with self.assertRaises(Conflict):
            increase(self.agent, self.project, self.run['id'], {**body, "limits": {"tokens_per_run": 201}})
        for limits in ({"tokens_per_run": 1}, {"arbitrary": 10}, {"tokens_per_run": True}, {"tokens_per_run": 3.5}):
            with self.assertRaises(ValueError):
                increase(self.agent, self.project, self.run['id'], {**body, "client_request_id": "bad", "limits": limits})


class OperationBudgetTests(unittest.TestCase):
    setUp = test_agent_operations.OperationTests.setUp
    arguments = test_agent_operations.OperationTests.arguments
    authorized = test_agent_operations.OperationTests.authorized
    effect = test_agent_operations.OperationTests.effect

    def test_effect_budget_rollback_and_replay(self):
        operations, context = self.authorized()
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET limits=? WHERE id=?", (json.dumps({"pages_per_run": 1, "engine_jobs_per_run": 1}), self.run['id']))
        with self.assertRaises(BudgetExceeded):
            operations.submit(context, 'run_ocr', self.arguments(), self.effect, cost={"pages": 1, "jobs": 2})
        self.assertEqual(self.store.rows("SELECT * FROM tasks"), [])
        self.assertEqual(self.agent.run(self.project, self.run['id'])['usage'].get('pages', 0), 0)
        first = operations.submit(context, 'run_ocr', self.arguments(), self.effect, cost={"pages": 1, "jobs": 1})
        self.assertEqual(first, operations.submit(context, 'run_ocr', self.arguments(), self.effect, cost={"pages": 1, "jobs": 1}))
        self.assertEqual(self.agent.run(self.project, self.run['id'])['usage']['pages'], 1)
