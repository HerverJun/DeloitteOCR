import asyncio
import json
import time
import unittest
import threading
from types import SimpleNamespace

import test_agent_policy
from test_structure_workflow import table
from ocr_workbench.agent.registry import ToolRegistry
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.tools import register_read_tools
from ocr_workbench.agent.review_tools import register_review_tools
from ocr_workbench.agent.contracts import ProviderCall
from ocr_workbench.application_services import ApplicationServices


class ReviewToolTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def prepare(self):
        task = self.store.enqueue(self.project, [self.photo['active_version']], ['glm'])[0]
        self.store.claim()
        value = table([['项目', '金额（元）'], ['甲', '0.10'], ['乙', '0.20'], ['合计', '0.31']])
        value['caption'] = '单位：元'
        self.store.complete(task, {'text': '', 'tables': [value], 'blocks': [], 'engine': 'glm'})
        result_id = self.store.one('tasks', task)['result_id']
        registry = ToolRegistry(self.policy)
        services = ApplicationServices(self.store, None, SimpleNamespace(guard=self.store.file_lock))
        views = register_read_tools(registry, services, lambda: {})
        register_review_tools(registry, services, Operations(self.agent, self.policy), views)
        args = {'result_id': result_id, 'revision': 0, 'table_id': 'table:0', 'checks': ['totals', 'rounding', 'structure']}
        return registry, args

    def execute(self, registry, args, call_id='inspect'):
        call = ProviderCall(call_id=call_id, tool='inspect_table', arguments=args)
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [call.model_dump()])
        return asyncio.run(registry.execute(self.run, 1, call, {'run': self.run, 'generation': 1, 'step': 0, 'call_id': call_id}))

    def test_decimal_checks_preserve_original_and_require_write_grant(self):
        registry, args = self.prepare()
        original = self.store.one('results', args['result_id'])
        result = self.execute(registry, args)
        issue = next(i for i in result['data']['issues'] if i['kind'] == 'total_difference')
        self.assertEqual(issue['difference'], '0.01')
        self.assertFalse(result['data']['automatic_correction'])
        self.assertEqual(self.store.one('results', args['result_id']), original)
        self.assertEqual(self.store.rows('SELECT * FROM agent_operations'), [])
        with self.assertRaises(ValueError):
            self.execute(registry, {**args, 'action': 'generate_candidates'}, 'denied')

    def test_candidate_generation_is_scoped_idempotent_and_never_adopts(self):
        registry, args = self.prepare()
        args['action'] = 'generate_candidates'
        self.policy.grant(self.project, 'scoped_check_job', {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
            'version_ids': [self.photo['active_version']], 'result_ids': [args['result_id']], 'revision': 0, 'table_id': 'table:0', 'checks': args['checks']},
            source='bound_ui_scope', expires=time.time() + 60, run_id=self.run['id'])
        original = self.store.one('results', args['result_id'])
        first = self.execute(registry, args)
        second = self.execute(registry, args, 'second')
        self.assertEqual(first['data']['operation_id'], second['data']['operation_id'])
        self.assertEqual(self.store.one('results', args['result_id']), original)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations')[0]['state'], 'finished')

    def test_visual_subset_is_atomic_uses_native_ids_and_never_adopts(self):
        from ocr_workbench.agent.visual import targets_for_result, check_external_job_authorization
        from ocr_workbench.agent.jobs import JobBridge
        from ocr_workbench.multimodal_store import prepare_review, complete_review
        registry, inspect_args = self.prepare()
        result_id = inspect_args['result_id']
        with self.store.transaction() as db:
            targets = targets_for_result(db, result_id)
        args = {'result_id': result_id, 'revision': 0, 'version_id': self.photo['active_version'],
                'target_ids': [targets[0]['id']], 'visual_model_id': 'external:synthetic'}
        self.assertTrue(args['target_ids'][0].startswith('target-'))
        config = {'backend': 'external', 'base_url': 'https://visual.invalid', 'revision': 'synthetic', 'model': {'id': 'synthetic'}}
        services = ApplicationServices(self.store, None, SimpleNamespace(guard=self.store.file_lock))
        services.visual_parameters = lambda model: (config, SimpleNamespace(wake=threading.Event()))
        registry = ToolRegistry(self.policy)
        views = register_read_tools(registry, services, lambda: {})
        jobs = JobBridge(self.agent, services)
        register_review_tools(registry, services, Operations(self.agent, self.policy), views, jobs)
        scope = {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']], 'version_ids': [self.photo['active_version']],
                 'result_ids': [result_id], 'target_ids': args['target_ids'], 'visual_model_id': args['visual_model_id'], 'revision': 0}
        self.policy.grant(self.project, 'scoped_visual_review', scope, source='bound_ui_scope', expires=time.time() + 60, run_id=self.run['id'])
        call = ProviderCall(call_id='visual', tool='request_visual_review', arguments=args)
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [call.model_dump()])
        context = {'run': self.run, 'generation': 1, 'step': 0, 'call_id': 'visual'}
        with self.assertRaises(ValueError):
            asyncio.run(registry.execute(self.run, 1, call, context))
        self.assertEqual(self.store.rows('SELECT * FROM multimodal_requests'), [])
        grant = self.policy.grant(self.project, 'visual_images', {'role': 'visual', 'endpoint': config['base_url'], 'config_revision': config['revision'],
            'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']], 'data_kinds': ['image', 'text', 'metadata']},
            source='bound_ui_scope', expires=time.time() + 60, run_id=self.run['id'])
        output = asyncio.run(registry.execute(self.run, 1, call, context))
        self.assertTrue(output['data']['waiting_jobs'])
        repeated = asyncio.run(registry.execute(self.run, 1, call, context))
        self.assertEqual(output['data']['operation_id'], repeated['data']['operation_id'])
        task = output['job_refs'][0]['job_id']
        self.store.claim(external=True)
        snapshot = prepare_review(self.store, task)
        self.assertEqual([t['id'] for t in snapshot['targets']], args['target_ids'])
        check_external_job_authorization(self.store, task, snapshot)
        with self.store.transaction() as db:
            db.execute('UPDATE agent_grants SET revoked=1 WHERE id=?', (grant,))
        with self.assertRaises(ValueError):
            check_external_job_authorization(self.store, task, snapshot)
        original = self.store.one('results', result_id)
        complete_review(self.store, task, {'summary': '已核对', 'items': [{'target_id': t['id'], 'decision': 'keep', 'after': t['before'], 'reason': '保留原文'} for t in snapshot['targets']]})
        completed = jobs.operation_result(self.project, self.run['id'], output['data']['operation_id'])
        self.assertEqual(completed['status'], 'success')
        self.assertFalse(completed['data']['reviews'][0]['automatic_adoption'])
        reference = completed['evidence_refs'][0]
        self.assertEqual(reference['result_id'], result_id)
        self.assertEqual(reference['revision'], 0)
        self.assertEqual(completed['data']['reviews'][0]['evidence_ref_id'], reference['ref_id'])
        self.assertEqual(reference['target_id'], 'visual-job:' + task)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_evidence WHERE id=?', (reference['ref_id'],))), 1)
        self.assertEqual(self.store.one('results', result_id), original)
