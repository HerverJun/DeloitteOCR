"""Document request, completion fairness and visible-search regressions; no GPU."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from ocr_workbench.imaging import add_image, save_version
from ocr_workbench.page_processing import complete_region_task, finalize_waiting_pages
from ocr_workbench.service import create_app
from ocr_workbench.store import Conflict, now, uid
from ocr_workbench.tables import parse_tables


class AuditDocumentRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bundle = self.root / 'bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config/engines.json').write_text(json.dumps({
            engine: {'name': engine, 'models': []} for engine in ('ppocr', 'glm')
        }), encoding='utf-8')
        self.app = create_app(bundle, self.root / 'workspace', 'audit-regression', start_queue=False)
        self.store = self.app.state.store
        self.manager = self.app.state.documents
        self.queue = self.app.state.queue
        self.project = self.store.project('独立文档回归')

    def route(self, path):
        return next(route.endpoint for route in self.app.routes if route.path == path)

    def photo(self, project=None):
        source = self.root / 'source.png'
        Image.new('RGB', (80, 60), 'white').save(source)
        return add_image(self.store, (project or self.project)['id'], source.name, source)

    def result(self, image, text, tables=None):
        task = self.store.enqueue(image['project_id'], [image['active_version']], ['ppocr'])[0]
        self.assertEqual(self.store.claim()['id'], task)
        self.store.complete(task, {'text': text, 'tables': tables or [], 'blocks': [], 'engine': 'ppocr'})
        return self.store.one('tasks', task)['result_id']

    def unrendered_page(self):
        document, page = uid(), uid()
        with self.store.transaction() as db:
            db.execute('''INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated)
                VALUES(?,?,?,'pdf',?,'synthetic',1,?,?)''',
                (document, self.project['id'], 'test.pdf', 'not-read.pdf', now(), now()))
            db.execute('INSERT INTO pages(id,document_id,page_number,render_dpi,created,updated) VALUES(?,?,1,300,?,?)',
                       (page, document, now(), now()))
        return page

    def waiting_page(self, project=None):
        image = self.photo(project)
        stage = self.manager.process(image['id'], 'ocr')
        self.assertTrue(self.manager.step())
        self.assertEqual(self.store.one('document_stages', stage)['status'], 'waiting_gpu')
        task = self.store.rows('SELECT task_id FROM page_ocr_inputs WHERE stage_id=?', (stage,))[0]['task_id']
        return stage, task

    def two_page_document(self):
        first, second = self.unrendered_page(), uid()
        document = self.store.one('pages', first)['document_id']
        with self.store.transaction() as db:
            db.execute('UPDATE documents SET page_count=2 WHERE id=?', (document,))
            db.execute('INSERT INTO pages(id,document_id,page_number,render_dpi,created,updated) VALUES(?,?,2,300,?,?)',
                       (second, document, now(), now()))
        return document, first, second

    def complete_region(self, task):
        self.assertTrue(complete_region_task(self.store, task, {
            'text': '已完成页 00123', 'tables': [], 'blocks': [], 'engine': 'ppocr',
        }))

    def test_render_then_process_conflicts_until_render_finishes(self):
        image = self.photo()
        render = self.route('/api/pages/{key}/render')
        process = self.route('/api/pages/{key}/process')
        stage = render(image['id'], {})['stage_id']
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                with self.store.transaction() as db:
                    db.execute('UPDATE document_stages SET status=? WHERE id=?', (status, stage))
                with self.assertRaisesRegex(Conflict, '其他处理请求'):
                    process(image['id'], {'mode': 'native'})
                self.assertEqual(render(image['id'], {})['stage_id'], stage)
        self.assertTrue(self.store.finish_document_stage(stage, {'image_id': image['id']}))
        following = process(image['id'], {'mode': 'native'})['stage_id']
        self.assertNotEqual(following, stage)
        self.assertTrue(self.manager.step())
        self.assertEqual(self.store.one('document_stages', following)['status'], 'succeeded')
        self.assertEqual(len(self.store.rows('SELECT id FROM results')), 1)

    def test_same_active_request_reuses_id_but_mode_engine_and_version_conflict(self):
        image = self.photo()
        params = {'mode': 'native', 'engine': 'ppocr'}
        stage = self.store.enqueue_document_stage(image['id'], 'process', params)
        for status in ('queued', 'running', 'waiting_gpu'):
            with self.subTest(status=status):
                with self.store.transaction() as db:
                    db.execute('UPDATE document_stages SET status=? WHERE id=?', (status, stage))
                # JSON property order is not part of request identity.
                self.assertEqual(self.store.enqueue_document_stage(image['id'], 'process',
                                 {'engine': 'ppocr', 'mode': 'native'}, force=True), stage)
                for changed in ({'mode': 'ocr', 'engine': 'ppocr'}, {'mode': 'native', 'engine': 'glm'}):
                    with self.assertRaises(Conflict):
                        self.store.enqueue_document_stage(image['id'], 'process', changed, force=True)
        parent = self.store.one('versions', image['active_version'])
        save_version(self.store, parent, Image.new('RGB', (80, 60)), {'kind': 'contrast', 'factor': 1.2})
        with self.assertRaises(Conflict):
            self.store.enqueue_document_stage(image['id'], 'process', params)
        self.assertEqual(len(self.store.rows('SELECT id FROM document_stages')), 1)

    def test_completed_request_is_reused_unless_forced(self):
        image = self.photo()
        stage = self.manager.process(image['id'], 'native')
        self.manager.step()
        self.assertEqual(self.manager.process(image['id'], 'native'), stage)
        forced = self.manager.process(image['id'], 'native', force=True)
        self.assertNotEqual(forced, stage)
        self.assertEqual(self.manager.process(image['id'], 'native', force=True), forced)

    def test_conflicting_render_dpi_does_not_change_pending_page(self):
        page = self.unrendered_page()
        render = self.route('/api/pages/{key}/render')
        stage = render(page, {'dpi': 72})['stage_id']
        self.assertEqual(self.store.one('pages', page)['render_dpi'], 72)
        self.assertEqual(render(page, {'dpi': 72})['stage_id'], stage)
        with self.assertRaises(Conflict):
            render(page, {'dpi': 150})
        self.assertEqual(self.store.one('pages', page)['render_dpi'], 72)
        self.assertEqual(json.loads(self.store.one('document_stages', stage)['parameters']), {'dpi': 72})

    def test_request_remains_idempotent_during_lazy_image_publication(self):
        page = self.unrendered_page()
        parameters = {'mode': 'native', 'engine': 'ppocr'}
        stage = self.store.enqueue_document_stage(page, 'process', parameters)
        source = self.root / 'lazy.png'
        Image.new('RGB', (80, 60), 'white').save(source)
        image = add_image(self.store, self.project['id'], source.name, source, page_id=page)
        self.assertIsNone(self.store.one('document_stages', stage)['version_id'])
        self.assertEqual(self.store.enqueue_document_stage(page, 'process', parameters), stage)
        parent = self.store.one('versions', image['active_version'])
        save_version(self.store, parent, Image.new('RGB', (80, 60)), {'kind': 'contrast', 'factor': 1.2})
        with self.assertRaises(Conflict):
            self.store.enqueue_document_stage(page, 'process', parameters)

    def test_store_rechecks_rendered_dpi_before_creating_stage(self):
        image = self.photo()
        with self.assertRaisesRegex(ValueError, '此页已展开'):
            self.store.enqueue_document_stage(image['id'], 'render', {'dpi': 72})
        self.assertEqual(self.store.one('pages', image['id'])['render_dpi'], 300)
        self.assertEqual(self.store.rows('SELECT id FROM document_stages'), [])

    def test_conflicting_page_request_returns_http_409(self):
        from test_document_api import LocalClient

        image = self.photo()
        client = LocalClient(self.app, {'Authorization': 'Bearer audit-regression'})
        self.addCleanup(client.close)
        first = client.post(f"/api/pages/{image['id']}/render", json={})
        self.assertEqual(first.status_code, 200)
        conflict = client.post(f"/api/pages/{image['id']}/process", json={'mode': 'native'})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        repeated = client.post(f"/api/pages/{image['id']}/render", json={})
        self.assertEqual(repeated.json()['stage_id'], first.json()['stage_id'])

    def test_document_process_conflict_rolls_back_all_pages_and_retry_is_idempotent(self):
        from test_document_api import LocalClient

        document, first, second = self.two_page_document()
        render = self.route('/api/pages/{key}/render')(second, {})['stage_id']
        self.manager.wake.clear()
        client = LocalClient(self.app, {'Authorization': 'Bearer audit-regression'})
        self.addCleanup(client.close)
        response = client.post(f'/api/documents/{document}/process', json={'mode': 'native', 'force': True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.rows('SELECT id FROM document_stages WHERE page_id=?', (first,)), [])
        self.assertEqual(self.store.one('document_stages', render)['status'], 'queued')
        self.assertEqual(len(self.store.rows('SELECT id FROM document_stages')), 1)
        self.assertFalse(self.manager.wake.is_set())

        with self.store.transaction() as db:
            db.execute("UPDATE document_stages SET status='cancelled' WHERE id=?", (render,))
        retried = client.post(f'/api/documents/{document}/process', json={'mode': 'native', 'force': True})
        self.assertEqual(retried.status_code, 200, retried.text)
        stages = retried.json()['stage_ids']
        self.assertEqual(len(stages), 2)
        self.assertTrue(self.manager.wake.is_set())
        self.assertEqual([self.store.one('document_stages', stage)['page_id'] for stage in stages], [first, second])
        self.assertTrue(all(self.store.one('document_stages', stage)['status'] == 'queued' for stage in stages))
        repeated = client.post(f'/api/documents/{document}/process', json={'mode': 'native', 'force': True})
        self.assertEqual(repeated.json()['stage_ids'], stages)
        selected = client.post(f'/api/documents/{document}/process', json={'mode': 'native', 'page_numbers': [2]})
        self.assertEqual(selected.json()['stage_ids'], stages[1:])

    def test_document_process_conflict_restores_failed_stage_status(self):
        document, first, second = self.two_page_document()
        previous = self.manager.process(first, 'native')
        with self.store.transaction() as db:
            db.execute("UPDATE document_stages SET status='failed',error='original failure',phase='处理失败' WHERE id=?", (previous,))
        original = self.store.one('document_stages', previous)
        self.route('/api/pages/{key}/render')(second, {})
        self.manager.wake.clear()

        with self.assertRaises(Conflict):
            self.route('/api/documents/{key}/process')(document, {'mode': 'native'})
        self.assertEqual(self.store.one('document_stages', previous), original)
        self.assertEqual(len(self.store.rows('SELECT id FROM document_stages')), 2)
        self.assertFalse(self.manager.wake.is_set())

    def test_completed_page_bypasses_fifty_paused_pages_in_another_project(self):
        blocked = [self.waiting_page() for _ in range(50)]
        other = self.store.project('独立完成项目')
        stage, task_id = self.waiting_page(other)
        self.queue.action(self.project['id'], 'pause', [task for _, task in blocked])
        task = self.store.claim()
        self.assertEqual(task['id'], task_id)
        self.complete_region(task)
        finalize_waiting_pages(self.manager)
        self.assertEqual(self.store.one('document_stages', stage)['status'], 'succeeded')
        self.assertTrue(all(self.store.one('document_stages', item)['status'] == 'waiting_gpu' for item, _ in blocked))
        self.assertTrue(all(self.store.one('tasks', item)['status'] == 'paused' for _, item in blocked))
        self.assertEqual(len(self.store.rows('SELECT id FROM results')), 1)
        finalize_waiting_pages(self.manager)
        self.assertEqual(len(self.store.rows('SELECT id FROM results')), 1)

    def test_completion_batch_remains_bounded_and_failure_does_not_starve_ready_page(self):
        stages = [self.waiting_page() for _ in range(52)]
        self.queue.action(self.project['id'], 'cancel', [stages[0][1]])
        while task := self.store.claim():
            self.complete_region(task)
        finalize_waiting_pages(self.manager)
        statuses = self.store.rows('SELECT status,COUNT(*) n FROM document_stages GROUP BY status')
        self.assertEqual({row['status']: row['n'] for row in statuses}, {'failed': 1, 'succeeded': 49, 'waiting_gpu': 2})
        finalize_waiting_pages(self.manager)
        self.assertEqual(self.store.one('document_stages', stages[-1][0])['status'], 'succeeded')
        self.assertEqual(len(self.store.rows('SELECT id FROM results')), 51)

    def test_search_decodes_quotes_paths_newlines_and_unicode_case(self):
        image = self.photo()
        text = '合同 "甲方"；路径 C:\\财务\\报表；ÉLODIE；Straße；00123\n下一行'
        self.result(image, text)
        search = self.route('/api/documents/{key}/search')
        for query in ('"甲方"', 'C:\\财务\\报表', 'élodie', 'STRASSE', '00123\n下一行', '甲方'):
            with self.subTest(query=query):
                response = search(image['id'], query)
                self.assertEqual(response['total'], 1)
                self.assertIn(query.casefold(), response['matches'][0]['snippet'].casefold())
        self.assertEqual(search(image['id'], 'tables')['total'], 0)

    def test_search_unicode_expansion_maps_snippet_to_original_text(self):
        image = self.photo()
        self.result(image, 'ß' * 150 + ' ÉLODIE ' + '尾' * 150)
        response = self.route('/api/documents/{key}/search')(image['id'], 'élodie')
        snippet = response['matches'][0]['snippet']
        self.assertIn('ÉLODIE', snippet)
        self.assertLessEqual(len(snippet), 30 + len('ÉLODIE') + 80)

    def test_search_uses_edited_cells_captions_and_retained_source_text(self):
        image = self.photo()
        raw = '正文\n<table><tr><td>OLD-CELL</td></tr></table>\n结尾'
        result = self.result(image, raw, parse_tables(raw))
        edited = deepcopy(self.store.result(result)['edited'])
        edited['tables'][0]['cells'][0]['text'] = '"新格" C:\\财务'
        edited['tables'][0]['caption'] = 'ÉLODIE 表题'
        saved = self.store.save(result, edited, 0)
        search = self.route('/api/documents/{key}/search')
        self.assertEqual(search(image['id'], 'OLD-CELL')['total'], 0)
        self.assertEqual(search(image['id'], '"新格" C:\\财务')['matches'][0]['target'], 'table:0:0:0')
        self.assertEqual(search(image['id'], 'élodie')['matches'][0]['target'], 'caption:0')
        self.store.save(result, {'text': raw.replace('OLD-CELL', '原文 &amp; 保留'), 'tables': []}, saved['revision'])
        self.assertEqual(search(image['id'], '原文 & 保留')['total'], 1)

    def test_search_only_adopted_revision_and_pagination(self):
        image = self.photo()
        table = {'rows': 2, 'columns': 1, 'cells': [
            {'row': row, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': f'共同词 {row}'} for row in range(2)
        ]}
        result = self.result(image, '共同词 正文', [table])
        self.result(image, '非采用结果专有词')
        search = self.route('/api/documents/{key}/search')
        self.assertEqual(search(image['id'], '非采用结果专有词')['total'], 0)
        all_matches = search(image['id'], '共同词')
        second = search(image['id'], '共同词', offset=1, limit=1)
        self.assertEqual(second['total'], 3)
        self.assertEqual(second['matches'], all_matches['matches'][1:2])
        self.assertTrue(all(row['result_id'] == result for row in all_matches['matches']))
        self.store.save(result, {'text': '新正文', 'tables': []}, 0)
        self.assertEqual(search(image['id'], '共同词')['total'], 0)
        self.assertEqual(search(image['id'], '新正文')['matches'][0]['revision'], 1)


if __name__ == '__main__':
    unittest.main()
