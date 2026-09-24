"""Large-page catalogs are readable; only an authorized bounded subset is sent."""
import asyncio
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_agent_policy
from test_structure_workflow import table
from ocr_workbench.agent.context import AgentContext
from ocr_workbench.agent.contracts import ProviderCall, ReadPageResult
from ocr_workbench.agent.jobs import JobBridge
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.policy import PolicyDenied
from ocr_workbench.agent.registry import ToolRegistry
from ocr_workbench.agent.review_tools import register_review_tools
from ocr_workbench.agent.selection import create_selection, grant_selection
from ocr_workbench.agent.tools import register_read_tools
from ocr_workbench.agent.visual import targets_for_result, check_external_job_authorization
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.editing import table_bindings
from ocr_workbench.geometry_contract import fingerprint
from ocr_workbench.multimodal_contract import build_targets, target_catalog
from ocr_workbench.multimodal_store import prepare_review, complete_review
from ocr_workbench.store import Conflict


class LargeVisualTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def prepare(self):
        self.edit = {'text': '\n'.join(f'line {i:02d} ' + 'x' * 1492 for i in range(30)),
                     'tables': [table([[f'cell {r}/{c}' for c in range(3)] for r in range(100)])]}
        task = self.store.enqueue(self.project, [self.photo['active_version']], ['glm'])[0]
        self.store.claim()
        self.store.complete(task, {**self.edit, 'blocks': [], 'engine': 'glm'})
        self.result_id = self.store.one('tasks', task)['result_id']
        self.version = self.store.one('versions', self.photo['active_version'])
        self.config = {'backend': 'external', 'base_url': 'https://visual.invalid',
                       'revision': 'synthetic', 'model': {'id': 'synthetic'}}
        self.services = ApplicationServices(self.store, None, SimpleNamespace(guard=self.store.file_lock))
        self.services.visual_parameters = lambda model: (self.config, SimpleNamespace(wake=threading.Event()))
        self.registry = ToolRegistry(self.policy)
        self.views = register_read_tools(self.registry, self.services, lambda: {})
        self.jobs = JobBridge(self.agent, self.services)
        register_review_tools(self.registry, self.services, Operations(self.agent, self.policy), self.views, self.jobs)
        with self.store.transaction() as db:
            self.catalog = targets_for_result(db, self.result_id)
        self.args = {'result_id': self.result_id, 'revision': 0, 'version_id': self.version['id'],
                     'target_ids': [self.catalog[-1]['id']], 'visual_model_id': 'external:synthetic'}

    def grant_targets(self, target_ids=None):
        self.policy.grant(self.project, 'scoped_visual_review', {
            'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
            'result_ids': [self.result_id], 'version_ids': [self.version['id']], 'revision': 0,
            'target_ids': target_ids or self.args['target_ids'], 'visual_model_id': self.args['visual_model_id']},
            source='bound_ui_scope', expires=time.time()+60, run_id=self.run['id'])

    def grant_outbound(self):
        return self.policy.grant(self.project, 'visual_images', {
            'role': 'visual', 'endpoint': 'https://visual.invalid', 'config_revision': 'synthetic',
            'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
            'data_kinds': ['image', 'text', 'metadata']},
            source='bound_ui_scope', expires=time.time()+60, run_id=self.run['id'])

    def execute(self, args=None, call_id='review'):
        call = ProviderCall(call_id=call_id, tool='request_visual_review', arguments=args or self.args)
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [call.model_dump()])
        return asyncio.run(self.registry.execute(self.run, 1, call, {
            'run': self.run, 'generation': 1, 'step': 0, 'call_id': call_id}))

    def test_large_catalog_paginates_completely_with_one_parse_per_read(self):
        self.prepare()
        self.assertGreater(len(self.edit['text']), 40000)
        self.assertEqual(len(self.catalog), 330)
        cursor, found, chunks = None, [], []
        for _ in range(100):
            with patch('ocr_workbench.multimodal_contract.table_bindings', wraps=table_bindings) as parsed:
                result = self.views.read_page(self.run, ReadPageResult(page_id=self.photo['id'], cursor=cursor))
            self.assertEqual(parsed.call_count, 1, 'Do not parse the page once per target')
            catalog = result['data']['visual_review']
            self.assertNotIn('reason', catalog)
            self.assertEqual(catalog['target_count'], 330)
            self.assertEqual(catalog['target_offset'], len(found))
            self.assertLessEqual(len(catalog['targets']), 10)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 16384)
            found.extend(catalog['targets'])
            chunks.append(result['data']['content'])
            cursor = result['next_cursor']
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(found, self.catalog)
        self.assertEqual(len({entry['id'] for entry in found}), 330)
        self.assertTrue(''.join(chunks).startswith('text\n' + self.edit['text']))
        self.assertIn('cell 99/2', ''.join(chunks))
        expected = {'kind': 'cell', 'table': 0, 'row': 99, 'column': 2}
        self.assertEqual(found[-1], {'id': 'target-' + fingerprint(expected)[:20], 'target': expected})

    def test_catalog_matches_exact_read_scope_and_cursor_binds_scope_and_version(self):
        self.prepare()
        cell_args = ReadPageResult(page_id=self.photo['id'], table_id='table:0', target_ids=['table:0:99:2'])
        cell = self.views.read_page(self.run, cell_args)
        self.assertEqual(cell['data']['visual_review']['targets'], self.catalog[-1:])
        self.assertEqual(cell['data']['visual_review']['target_count'], 1)
        self.assertEqual(cell['data']['content'], 'table:0:99:2\ncell 99/2')
        first = self.views.read_page(self.run, ReadPageResult(page_id=self.photo['id'], table_id='table:0'))
        self.assertEqual(first['data']['visual_review']['target_count'], 300)
        with self.assertRaises(Conflict):
            self.views.read_page(self.run, ReadPageResult(page_id=self.photo['id'], cursor=first['next_cursor']))
        with self.store.transaction() as db:
            db.execute('UPDATE images SET active_version=? WHERE id=?', (self.other_photo['active_version'], self.photo['id']))
        with self.assertRaises(Conflict):
            self.views.read_page(self.run, ReadPageResult(page_id=self.photo['id'], table_id='table:0', cursor=first['next_cursor']))

    def test_one_late_cell_submits_exactly_once_and_never_adopts(self):
        self.prepare()
        self.grant_targets()
        self.grant_outbound()
        original = self.store.one('results', self.result_id)
        first = self.execute()
        replay = self.execute(call_id='replayed-intent')
        self.assertEqual(first['data']['operation_id'], replay['data']['operation_id'])
        self.assertEqual(len(self.store.rows('SELECT * FROM multimodal_requests')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
        task_id = first['job_refs'][0]['job_id']
        self.store.claim(external=True)
        snapshot = prepare_review(self.store, task_id)
        self.assertEqual([entry['id'] for entry in snapshot['targets']], self.args['target_ids'])
        self.assertEqual(snapshot['targets'][0]['before'], 'cell 99/2')
        self.assertEqual(snapshot['targets'][0]['target'], self.catalog[-1]['target'])
        check_external_job_authorization(self.store, task_id, snapshot)
        self.assertTrue(complete_review(self.store, task_id, {'items': [{
            'target_id': self.args['target_ids'][0], 'decision': 'replace', 'after': 'candidate only', 'reason': '合成候选'}]}))
        self.assertEqual(self.store.one('results', self.result_id), original)
        self.assertEqual(self.store.rows('SELECT result_id FROM selections WHERE image_id=?', (self.photo['id'],))[0]['result_id'], self.result_id)
        self.assertEqual(self.jobs.operation_result(self.project, self.run['id'], first['data']['operation_id'])['status'], 'success')

    def test_one_text_fragment_on_large_page_preserves_original_target_and_value(self):
        self.prepare()
        self.args['target_ids'] = [self.catalog[0]['id']]
        self.grant_targets()
        self.grant_outbound()
        output = self.execute()
        task_id = output['job_refs'][0]['job_id']
        self.store.claim(external=True)
        snapshot = prepare_review(self.store, task_id)
        self.assertEqual([entry['id'] for entry in snapshot['targets']], self.args['target_ids'])
        self.assertEqual(snapshot['targets'][0]['target'], {'kind': 'text', 'start': 0, 'end': 1500})
        self.assertEqual(snapshot['targets'][0]['before'], self.edit['text'][:1500])

    def test_large_page_denies_extra_scope_endpoint_config_stale_and_nonadopted(self):
        self.prepare()
        self.grant_targets()
        for change in ({'target_ids': [self.catalog[-2]['id']]},
                       {'target_ids': ['target-does-not-exist']}, {'revision': 1},
                       {'version_id': self.other_photo['active_version']}):
            with self.subTest(change=change), self.assertRaises((PolicyDenied, Conflict)):
                self.execute({**self.args, **change}, 'deny-' + str(len(json.dumps(change))))
        with self.assertRaises(PolicyDenied):
            self.execute(call_id='missing-outbound')
        self.grant_outbound()
        for key, invalid in (('base_url', 'https://other.invalid'), ('revision', 'changed')):
            old = self.config[key]
            self.config[key] = invalid
            with self.subTest(config=key), self.assertRaises(PolicyDenied):
                self.execute(call_id='wrong-' + key)
            self.config[key] = old
        with self.store.transaction() as db:
            db.execute('DELETE FROM selections WHERE image_id=?', (self.photo['id'],))
        with self.assertRaises(PolicyDenied):
            self.execute(call_id='not-adopted')
        self.assertFalse(self.store.rows('SELECT * FROM multimodal_requests'))
        self.assertFalse(self.store.rows('SELECT * FROM agent_operations'))

    def test_ui_selection_can_grant_large_page_without_authorizing_another_page(self):
        self.prepare()
        selected = create_selection(self.agent, self.project, {
            'image_ids': [self.photo['id']], 'visual_model_id': 'external:synthetic', 'allow_visual_images': True},
            available_engines=[])
        grant_selection(self.policy, self.run, selected['selection'], services=self.services)
        self.policy.authorize(self.run, 1, 'request_visual_review', self.args)
        grant = self.store.rows("SELECT scope FROM agent_grants WHERE permission='scoped_visual_review'")[0]
        scope = json.loads(grant['scope'])
        self.assertEqual(scope['target_ids'], [t['id'] for t in self.catalog])
        self.assertEqual(scope['page_ids'], [self.photo['id']])
        with self.assertRaises(PolicyDenied):
            self.policy.authorize_outbound(self.run, role='visual', endpoint=self.config['base_url'],
                config_revision=self.config['revision'], document_ids=[self.other_photo['id']],
                page_ids=[self.other_photo['id']], data_kinds=['image', 'text', 'metadata'])

    def test_selected_subset_budgets_are_enforced_without_silent_truncation(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, '预算'):
            build_targets(self.edit, {}, self.version, 'page')
        for ids in ([t['id'] for t in self.catalog[:27]], [t['id'] for t in self.catalog[30:287]],
                    [self.catalog[-1]['id'], 'unknown'], [self.catalog[-1]['id']] * 2, []):
            with self.subTest(count=len(ids)), self.assertRaises(ValueError):
                build_targets(self.edit, {}, self.version, 'page', target_ids=ids)
        oversized = deepcopy(self.edit)
        oversized['tables'][0]['cells'][0]['text'] = 'y' * 4001
        ids = [entry['id'] for entry in target_catalog(oversized)]
        with self.assertRaisesRegex(ValueError, '4000'):
            build_targets(oversized, {}, self.version, 'page', target_ids=[ids[30]])
        with patch('ocr_workbench.multimodal_contract.table_bindings', wraps=table_bindings) as parsed:
            selected = build_targets(oversized, {}, self.version, 'page', target_ids=[ids[-1]])
        self.assertEqual(parsed.call_count, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['before'], 'cell 99/2')
        self.grant_targets([entry['id'] for entry in self.catalog])
        self.grant_outbound()
        with self.assertRaises(ValueError):
            self.execute({**self.args, 'target_ids': ids[:27]}, 'over-total-budget')
        with self.assertRaises(ValueError):
            self.execute({**self.args, 'target_ids': ids[30:131]}, 'over-agent-count')
        self.assertFalse(self.store.rows('SELECT * FROM multimodal_requests'))
        self.assertFalse(self.store.rows('SELECT * FROM agent_operations'))
