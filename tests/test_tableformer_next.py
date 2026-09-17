from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.geometry_labels import canonical_grid, derive_grid_sample, validate_annotations
from ocr_workbench.geometry_candidate_worker import region_inputs
from ocr_workbench.geometry_providers import adapt_prediction
from ocr_workbench.table_matching import local_mapping, policy_for_algorithm
from ocr_workbench.tables import parse_tables
from test_table_matching import html, prediction, mapped
import test_geometry as legacy_geometry


class BoundaryContractTests(unittest.TestCase):
    def canonical(self):
        return {'rows': [{'pdf_row_bbox': [0, 0, 100, 10]}, {'pdf_row_bbox': [0, 30, 100, 40]}],
                'columns': [{'pdf_column_bbox': [0, 0, 20, 40]}, {'pdf_column_bbox': [60, 0, 100, 40]}],
                'cells': [{'row_nums': [0], 'column_nums': [0]}, {'row_nums': [0], 'column_nums': [1]},
                          {'row_nums': [1], 'column_nums': [0, 1]}]}

    def test_grid_preserves_gutters_and_spans_without_mutating_pdf_extents(self):
        canonical = self.canonical(); original = deepcopy(canonical)
        grid = canonical_grid(canonical)
        self.assertEqual(grid[0, 0, 1, 1], [0, 0, 40, 20])
        self.assertEqual(grid[1, 0, 1, 2], [0, 20, 100, 40])
        self.assertEqual(canonical, original)
        for change in ('gap', 'duplicate', 'nan', 'order'):
            c = self.canonical()
            if change == 'gap': c['cells'].pop()
            elif change == 'duplicate': c['cells'].append(c['cells'][0])
            elif change == 'nan': c['rows'][0]['pdf_row_bbox'][0] = float('nan')
            else: c['columns'].reverse()
            with self.assertRaises(ValueError): canonical_grid(c)

    def test_mixed_or_undeclared_label_semantics_cannot_be_scored(self):
        annotations = {'samples': [{'width': 10, 'height': 10, 'targets': [
            {'row': 0, 'column': 0, 'box': [0, 0, 10, 10], 'boundary_convention': 'pdf_cell_extent'}]}]}
        with self.assertRaises(ValueError): validate_annotations(annotations, 'full_grid')
        annotations['boundary_convention'] = 'full_grid'
        with self.assertRaises(ValueError): validate_annotations(annotations, 'full_grid')


class NeighborAnchorTests(unittest.TestCase):
    def test_bracketed_repeated_row_recovers_without_moving_model_boxes(self):
        a = html([['Name', 'Amount'], ['A', '10'], ['0', '0'], ['B', '20']])
        p = prediction(a)
        before = mapped(a, p)
        after = mapped(a, p, policy=policy_for_algorithm('local-v3'))
        self.assertTrue(all(v['level'] != 'cell' for v in before[4:6]))
        self.assertTrue(all(v['level'] == 'cell' for v in after))
        self.assertEqual(after[4]['polygon'], box_polygon([0, 80, 100, 120]))
        lineage = after[4]['correspondence']['anchors'][0]['lineage']
        self.assertGreaterEqual(len(lineage['roots']), 2)
        self.assertNotIn('0:2:0', lineage['roots'])
        p['ocr_blocks'].reverse(); p['tables'][0]['raw']['det']['boxes'].reverse()
        reversed_result = mapped(a, p, policy=policy_for_algorithm('local-v3'))
        self.assertEqual([m['polygon'] for m in after], [m['polygon'] for m in reversed_result])

    def test_missing_row_and_all_repeated_values_cannot_create_circular_anchors(self):
        a = html([['Name', 'Amount'], ['A', '10'], ['0', '0'], ['B', '20']])
        p = prediction(html([['Name', 'Amount'], ['A', '10'], ['B', '20']]))
        m = mapped(a, p, policy=policy_for_algorithm('local-v3'))
        self.assertTrue(all(v['level'] != 'cell' for v in m[4:6]))
        repeated = html([['0', '0'], ['0', '0']])
        self.assertTrue(all(v['level'] != 'cell' for v in mapped(repeated, prediction(repeated), policy=policy_for_algorithm('local-v3'))))

    def test_empty_and_crossing_tokens_still_need_real_geometry_and_exclusivity(self):
        a = html([['Name', 'Amount'], ['A', '10'], ['', ''], ['B', '20']])
        p = prediction(a)
        m = mapped(a, p, policy=policy_for_algorithm('local-v3'))
        self.assertTrue(all(v['level'] == 'cell' for v in m[4:6]))
        self.assertEqual(m[4]['content_polygons'], [])
        p['ocr_blocks'] = [{'id': 'cross', 'text': 'Name Amount A 10 B 20', 'polygon': box_polygon([0, 0, 200, 160])}]
        self.assertTrue(all(v['level'] != 'cell' for v in mapped(a, p, policy=policy_for_algorithm('local-v3'))))

    def test_thresholds_remain_identical_to_control(self):
        a, b = policy_for_algorithm('local-v2'), policy_for_algorithm('local-v3')
        for key in ('token_minimum_containment', 'token_minimum_margin', 'token_maximum_other_overlap',
                    'table_minimum_unique_anchors', 'table_minimum_margin', 'maximum_group_cells', 'table_budget_ms'):
            self.assertEqual(a[key], b[key])


class RawProviderTests(unittest.TestCase):
    def candidate(self):
        return {'component': 'tableformer-raw', 'coordinate_contract': 'crop-pixels-to-image-affine-v1',
            'candidate_tables': [{'region_id': 'right-table', 'table_box': [100, 40, 300, 120],
                'crop_to_image': [1, 0, 100, 0, 1, 40, 0, 0, 1], 'prediction': {
                    'source_semantics': 'raw_structure', 'model_sha256': 'pinned', 'tf_table_cells': [
                        {'cell_id': 19, 'row_id': 3, 'column_id': 4, 'rowspan_val': 2, 'colspan_val': 1, 'bbox': [0, 0, 80, 60]}]}}]}

    def test_crop_transform_preserves_original_ids_spans_and_box(self):
        p = self.candidate(); table = adapt_prediction(p, 400, 200)[0]; cell = table['cells'][0]
        self.assertEqual(cell.original_cell_id, '19')
        self.assertEqual((cell.row_start, cell.row_end, cell.column_start), (3, 5, 4))
        self.assertEqual(cell.original_polygon, box_polygon([0, 0, 80, 60]))
        self.assertEqual(cell.cell_polygon, box_polygon([100, 40, 180, 100]))
        self.assertEqual(table['region_id'], 'right-table')
        p['candidate_tables'][0]['crop_to_image'][2] = 0
        with self.assertRaises(ValueError): adapt_prediction(p, 400, 200)

    def test_text_extents_and_duplicate_original_ids_are_rejected(self):
        p = self.candidate(); raw = p['candidate_tables'][0]['prediction']
        raw['source_semantics'] = 'matched_text_extent'
        with self.assertRaises(ValueError): adapt_prediction(p, 400, 200)
        raw['source_semantics'] = 'raw_structure'; raw['tf_table_cells'] *= 2
        with self.assertRaises(ValueError): adapt_prediction(p, 400, 200)

    def test_overlapping_regions_do_not_duplicate_shared_ocr(self):
        image = Image.new('RGB', (400, 200))
        request = {'image_version': 'v', 'regions': [{'box': [100, 40, 300, 120]}],
            'ocr_blocks': [{'text': 'Value', 'polygon': box_polygon([110, 50, 150, 60])}]}
        before = deepcopy(request)
        self.assertEqual(list(region_inputs(image, request))[0][3][0]['polygon'], box_polygon([10, 10, 50, 20]))
        self.assertEqual(request, before)
        request['regions'] *= 2
        self.assertTrue(all(not blocks for _, _, _, blocks in region_inputs(image, request)))


class ProviderStorageTests(unittest.TestCase):
    setUp = legacy_geometry.GeometryStorageTests.setUp
    tearDown = legacy_geometry.GeometryStorageTests.tearDown
    def test_provider_and_algorithm_selections_have_distinct_candidate_and_mapping_caches(self):
        from ocr_workbench.geometry import enqueue_geometry
        with patch('ocr_workbench.geometry_provider_config.provider_identity', side_effect=lambda p: {'provider': p}):
            requests = [enqueue_geometry(self.store, self.result, 0, provider=p, algorithm=a)
                        for p, a in [('paddle', 'local-v2'), ('tableformer-raw', 'local-v2'), ('tableformer-raw', 'local-v3')]]
        snapshots = [json.loads(self.store.rows('SELECT snapshot FROM geometry_requests WHERE task_id=?', (r['task_id'],))[0]['snapshot']) for r in requests]
        self.assertNotEqual(snapshots[0]['candidate_cache_key'], snapshots[1]['candidate_cache_key'])
        self.assertEqual(snapshots[1]['candidate_cache_key'], snapshots[2]['candidate_cache_key'])
        self.assertNotEqual(snapshots[1]['mapping_cache_key'], snapshots[2]['mapping_cache_key'])
        with self.assertRaises(ValueError): enqueue_geometry(self.store, self.result, 0, provider='tableformer-raw', algorithm='legacy')

    def complete_raw(self):
        from ocr_workbench.geometry import enqueue_geometry, complete_geometry
        from ocr_workbench.geometry_contract import fingerprint
        from ocr_workbench.geometry_provider_config import file_sha
        with patch('ocr_workbench.geometry_provider_config.provider_identity', return_value={'provider': 'tableformer-raw'}):
            enqueue_geometry(self.store, self.result, 0, provider='tableformer-raw', algorithm='local-v3')
        task = self.store.claim()
        upstream = self.store.root / 'upstream.json'; upstream.write_text('{}', 'utf-8')
        path = self.store.root / 'geometry.json'
        version = self.store.one('versions', self.image['active_version'])
        result = {'status': 'success', 'component': 'tableformer-raw', 'model_revisions': {},
            'image_sha256': version['sha256'], 'image_version': version['id'], 'ocr_blocks': [],
            'shared_ocr_sha256': fingerprint([]), 'candidate_tables': [],
            'coordinate_contract': 'crop-pixels-to-image-affine-v1',
            'upstream_artifact': {'path': 'upstream.json', 'sha256': file_sha(upstream)}}
        path.write_text(json.dumps(result), 'utf-8')
        with patch('ocr_workbench.geometry_provider_config.verify_provider', return_value={}):
            complete_geometry(self.store, task, result, path)
        return task, result, path

    def test_latest_selected_provider_is_visible_and_cached_replay_never_loads_gpu(self):
        from ocr_workbench.geometry import geometry_view, enqueue_geometry
        from ocr_workbench.task_queue import TaskQueue
        self.complete_raw()
        rows = geometry_view(self.store, self.result)['evidence']
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r['source'] == 'tableformer-raw-local-v3' for r in rows))
        self.assertEqual(geometry_view(self.store, self.result, provider='paddle')['evidence'], [])
        changed = deepcopy(self.edit); changed['tables'][0]['cells'][3]['text'] = '200.00'
        self.store.save(self.result, changed, 0)
        with patch('ocr_workbench.geometry_provider_config.provider_identity', return_value={'provider': 'tableformer-raw'}):
            request = enqueue_geometry(self.store, self.result, 1, provider='tableformer-raw', algorithm='local-v3')
        def no_gpu(*args): raise AssertionError('Cached candidate must not load GPU')
        with patch('ocr_workbench.geometry_provider_config.verify_provider', return_value={}):
            self.assertTrue(TaskQueue(self.store, self.root, adapter_factory=no_gpu).step())
        self.assertEqual(self.store.one('tasks', request['task_id'])['status'], 'succeeded')
        self.assertTrue(all(r['details']['candidate_cache_hit'] for r in geometry_view(self.store, self.result)['evidence']))

    def test_changed_upstream_artifact_is_rejected_before_cache_commit(self):
        from ocr_workbench.geometry import enqueue_geometry
        from ocr_workbench.task_queue import TaskQueue
        self.complete_raw()
        (self.store.root / 'upstream.json').write_text('{"changed":true}', 'utf-8')
        with patch('ocr_workbench.geometry_provider_config.provider_identity', return_value={'provider': 'tableformer-raw'}):
            request = enqueue_geometry(self.store, self.result, 0, provider='tableformer-raw', algorithm='local-v3', force=True)
        with patch('ocr_workbench.geometry_provider_config.verify_provider', return_value={}):
            TaskQueue(self.store, self.root).step()
        task = self.store.one('tasks', request['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertIn('哈希不一致', task['error'])


if __name__ == '__main__':
    unittest.main()
