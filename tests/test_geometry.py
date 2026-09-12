from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image

from ocr_workbench.store import Store, Conflict
from ocr_workbench.imaging import add_image
from ocr_workbench.geometry import conservative_mapping, enqueue_geometry, complete_geometry, geometry_view, bind_manual
from ocr_workbench.tables import parse_tables
from ocr_workbench.coordinates import box_polygon

HTML = '<table><tr><td>Account</td><td>Amount</td></tr><tr><td>001234</td><td>100.00</td></tr></table>'


def prediction(html=HTML):
    boxes = [[10, 10, 100, 40], [100, 10, 190, 40], [10, 40, 100, 70], [100, 40, 190, 70]]
    return {'model_revisions': {'detector': 'fixed-version'}, 'inference_seconds': .05, 'tables': [
        {'region_id': None, 'table_box': [0, 0, 200, 80], 'final': {'pred_html': html, 'cell_box_list': boxes},
         'raw': {'det': {'boxes': [{'coordinate': box, 'score': .98} for box in boxes]}}}]}


class GeometryMappingTests(unittest.TestCase):
    def test_malformed_model_structure_degrades_without_crashing(self):
        result = conservative_mapping({'tables':parse_tables(HTML)},prediction('<table><tr><td>Truncated'),200,80)
        self.assertTrue(all(item['level']=='image' for item in result))

    def test_direct_detection_unique_structure_and_text_give_cell_coordinates(self):
        edit = {'text': HTML, 'tables': parse_tables(HTML)}
        result = conservative_mapping(edit, prediction(), 200, 80)
        self.assertEqual([x['level'] for x in result], ['region']+['cell']*4)
        self.assertTrue(all(x['geometry_origin'] == 'direct_detection' for x in result[1:]))

    def test_unsupported_postprocessed_box_degrades_without_fake_precision(self):
        p = prediction()
        p['tables'][0]['raw']['det']['boxes'].pop()
        result = conservative_mapping({'tables': parse_tables(HTML)}, p, 200, 80)
        self.assertEqual(result[-1]['level'], 'region')
        self.assertEqual(result[-1]['reason'], 'postprocessed_box_without_unique_detection_support')

    def test_same_cell_count_different_structure_and_repeated_values_are_ambiguous(self):
        p = prediction('<table><tr><td>A</td><td>B</td><td>C</td><td>D</td></tr></table>')
        self.assertTrue(all(x['level'] != 'cell' for x in conservative_mapping({'tables': parse_tables(HTML)}, p, 200, 80)))
        repeated = '<table><tr><td>0</td><td>0</td></tr><tr><td>0</td><td>0</td></tr></table>'
        self.assertTrue(all(x['level'] != 'cell' for x in conservative_mapping({'tables': parse_tables(repeated)}, prediction(repeated), 200, 80)))

    def test_ambiguous_multiple_tables_do_not_guess_even_a_region(self):
        p = prediction()
        p['tables'] *= 2
        values = conservative_mapping({'tables': parse_tables(HTML+HTML)}, p, 200, 80)
        self.assertTrue(all(x['polygon'] is None for x in values))


class GeometryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'data')
        self.project = self.store.project('Geometry')
        path = self.root / 'image.png'
        Image.new('RGB', (200, 80), 'white').save(path)
        self.image = add_image(self.store, self.project['id'], 'image.png', path)
        self.edit = {'text': HTML, 'tables': parse_tables(HTML)}
        task = self.store.enqueue(self.project['id'], [self.image['active_version']], ['ppocr'])[0]
        self.store.claim()
        self.store.complete(task, {**self.edit, 'blocks': [], 'engine': 'ppocr'})
        self.result = self.store.one('tasks', task)['result_id']

    def tearDown(self):
        self.temporary.cleanup()

    def geometry(self):
        request = enqueue_geometry(self.store, self.result, 0)
        task = self.store.claim()
        self.assertEqual(request['task_id'], task['id'])
        self.assertTrue(complete_geometry(self.store, task, prediction(), self.store.root / 'evidence.json'))

    def test_geometry_cache_never_changes_text_revision_selection_or_original(self):
        before = self.store.result(self.result)
        selection = self.store.rows('SELECT * FROM selections')
        self.geometry()
        self.assertEqual(self.store.result(self.result), before)
        self.assertEqual(self.store.rows('SELECT * FROM selections'), selection)
        self.assertTrue(enqueue_geometry(self.store, self.result, 0)['cached'])
        self.assertEqual(len(geometry_view(self.store, self.result)['evidence']), 5)

    def test_native_structure_is_separate_preview_and_exports_native_words_once(self):
        from ocr_workbench.pdf_export import positioned_content
        text = 'Account Amount\n001234 100.00'
        values=['Account','Amount','001234','100.00']
        boxes=prediction()['tables'][0]['final']['cell_box_list']
        blocks=[];cursor=0
        for value,box in zip(values,boxes):
            start=text.index(value,cursor);cursor=start+len(value)
            blocks.append({'text':value,'source':'pdf-native','polygon':box_polygon(box),'text_range':[start,cursor],'confidence':None})
        task=self.store.enqueue(self.project['id'],[self.image['active_version']],['ppocr'])[0]
        self.store.claim()
        self.store.complete(task,{'text':text,'tables':[],'blocks':blocks,'engine':'pdf-native','origin':'document','document':{'conflicts':[]}})
        self.result=self.store.one('tasks',task)['result_id']
        original=self.store.result(self.result)
        selection=self.store.rows('SELECT * FROM selections')
        enqueue_geometry(self.store,self.result,0)
        task=self.store.claim()
        complete_geometry(self.store,task,prediction(),self.store.root/'evidence.json')
        self.assertEqual(self.store.result(self.result),original)
        self.assertEqual(self.store.rows('SELECT * FROM selections'),selection)
        preview_task=self.store.rows("SELECT * FROM tasks WHERE phase='原生表格结构预览'")[0]
        preview=self.store.result(preview_task['result_id'])
        self.assertEqual([c['text'] for c in preview['edited']['tables'][0]['cells']],values)
        units,missing,changed=positioned_content(preview,geometry_view(self.store,preview['id'])['evidence'],{'width':200,'height':80})
        self.assertEqual(missing,[])
        self.assertFalse(changed)
        self.assertEqual([u['text'] for u in units],values)
        self.assertTrue(all(u['native_preserved'] for u in units))
        self.assertTrue(enqueue_geometry(self.store,self.result,0)['cached'])
        from ocr_workbench.fusion import default_policy
        for result_id in (self.result, preview['id']):
            with self.assertRaises(ValueError):
                self.store.enqueue_fusion(self.project['id'], [result_id], default_policy(), 'reject-native-'+result_id)

    def test_text_edit_keeps_geometry_but_structure_edit_and_undo_expire_it(self):
        self.geometry()
        edit = deepcopy(self.edit)
        edit['tables'][0]['cells'][3]['text'] = '200.00'
        self.store.save(self.result, edit, 0)
        self.assertEqual(len(geometry_view(self.store, self.result)['evidence']), 5)
        changed = deepcopy(edit)
        changed['tables'][0]['rows'] = 3
        changed['tables'][0]['cells'].append({'row': 2, 'column': 0, 'row_span': 1, 'column_span': 2, 'text': 'added'})
        self.store.save(self.result, changed, 1)
        self.assertEqual(geometry_view(self.store, self.result)['evidence'], [])
        self.store.history(self.result, -1, 2)
        self.assertEqual(geometry_view(self.store, self.result)['evidence'], [])

    def test_manual_binding_is_explicit_and_revision_checked(self):
        self.geometry()
        target = {'kind': 'cell', 'table': 0, 'row': 1, 'column': 1}
        body = {'revision': 0, 'version_id': self.image['active_version'], 'target': target,
                'polygon': box_polygon([100, 40, 190, 70])}
        bind_manual(self.store, self.result, body)
        evidence = geometry_view(self.store, self.result, target)['evidence']
        self.assertEqual(evidence[0]['source'], 'manual')
        self.assertEqual(geometry_view(self.store,self.result,dict(reversed(list(target.items()))))['evidence'][0]['id'],evidence[0]['id'])
        with self.assertRaises(Conflict):
            bind_manual(self.store, self.result, {**body, 'revision': 99})

    def test_cancelled_geometry_cannot_commit_late_evidence(self):
        request = enqueue_geometry(self.store, self.result, 0)
        task = self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task['id'],))
        self.assertFalse(complete_geometry(self.store, task, prediction(), self.store.root / 'evidence.json'))
        self.assertEqual(geometry_view(self.store, self.result)['evidence'], [])


if __name__ == '__main__':
    unittest.main()
