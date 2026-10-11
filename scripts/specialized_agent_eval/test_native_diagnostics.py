"""CPU tamper/revision/geometry gates; synthetic receipts are not model evidence."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import tempfile
import unittest
from common import FORMAT, digest, fingerprint, fpath, frozen_json
from native_bridge import normalize_native_index
from native_diagnostics import collect_diagnostics, diagnostic_source, validate_detail

SOURCE = fpath('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr')
OUTPUT = None


def fixture(module, geometry=True):
    cells = [{'row': r, 'column': c, 'row_span': 1, 'column_span': 1,
              'text': [['编号', '金额'], ['000123', '[unreadable]']][r][c],
              'box': [c * 40., r * 30., (c + 1) * 40., (r + 1) * 30.],
              'geometry_origin': 'direct_detection' if geometry else 'word_detection'}
             for r in range(2) for c in range(2)]
    tables = [{'rows': 2, 'columns': 2, 'box': [0., 0., 80., 60.], 'cells': cells}]
    original = {'engine': 'fixture-only', 'text': '', 'tables': tables, 'project_image_version': 'v1',
                'screen': {'batch_id': 'b1', 'input_sha256': 'b' * 64,
                           'selection': {'selected_id': 'c1'}, 'sources': [{'result_id': 'source-r1', 'revision': 0}]}}
    edited = {'text': '', 'tables': deepcopy(tables)}
    preparation = {'width': 80, 'height': 60, 'business_regions': []}
    native_item = {'batch_id': 'b1', 'input_version_id': 'v1', 'image_id': 'im1', 'status': 'needs_review',
                   'result_id': 'r1', 'result_revision': 0, 'review_state': 'automatic',
                   'preparation': json.dumps(preparation), 'evidence': '{}'}
    row = {'id': 's1', 'input_sha256': 'a' * 64, 'native_version_sha256': 'b' * 64,
           'input_version_id': 'v1', 'result_id': 'r1', 'result_revision': 0, 'native_status': 'needs_review',
           'scoring_status': 'success', 'review_state': 'automatic', 'human_accepted': False,
           'stored_result_task': {'id': 't1', 'image_id': 'im1'},
           'selected_candidate_id': 'c1', 'source_receipts': deepcopy(original['screen']['sources']),
           'result_original_sha256': fingerprint(original), 'result_edited_sha256': fingerprint(edited),
           'issues': [{'code': 'quality_review_required'}],
           'selected_candidate': {**deepcopy(original), 'quality': {'automatic_delivery': False}},
           'edited_view': deepcopy(edited)}
    diagnostic = module.build_screen_diagnostics({**original, **edited}, preparation, {}, remaining_seconds=10,
        capabilities={'local_reread': False, 'precision_subbatch': True, 'structure_compare': False})
    diagnostic.update(result_id='r1', result_revision=0, input_version_id='v1')
    detail = {'sample_id': 's1', 'source_image_sha256': 'a' * 64, 'input_version_id': 'v1', 'image_id': 'im1',
              'status': 'needs_review', 'result_id': 'r1', 'result_revision': 0, 'review_state': 'automatic',
              'current_result_revision': 0, 'review_stale': False, 'preparation': preparation,
              'result': {'id': 'r1', 'task_id': 't1', 'revision': 0, 'original': original, 'edited': edited},
              'diagnostics': diagnostic}
    index = {'format': 'actual-native-offline-receipt-index-v1', 'truth_opened': False,
             'inputs_sha256': 'fixture-only', 'native_table_identity_sha256': 'fixture-only',
             'native_database': 'fixture-only', 'batch_id': 'b1', 'actual_policy': {'budget_seconds': 20},
             'rows': [row]}
    return detail, row, index, native_item


def grid_fixture(module):
    """Synthetic source pixels and PP anchors, never actual OCR qualification."""
    from PIL import Image, ImageDraw
    path = OUTPUT / 'synthetic-grid-source.png'
    xs, ys = [30, 125, 220, 340], [30, 70, 115, 165]
    image = Image.new('RGB', (370, 195), 'white')
    draw = ImageDraw.Draw(image)
    for x in xs:
        draw.line((x, ys[0], x, ys[-1]), fill='black', width=2)
    for y in ys:
        draw.line((xs[0], y, xs[-1], y), fill='black', width=2)
    values = [['ID', 'Item', 'Amount'], ['000123', 'alpha', '10.00'], ['000124', 'beta', '20.00']]
    cells, readings = [], []
    for r in range(3):
        for c in range(3):
            draw.text((xs[c]+8, ys[r]+9), values[r][c], fill='black')
            cells.append({'row': r, 'column': c, 'row_span': 1, 'column_span': 1,
                          'text': '[unreadable]' if (r, c) == (1, 2) else values[r][c]})
            readings.append({'text': values[r][c], 'box': [xs[c]+8, ys[r]+8, xs[c]+70, ys[r]+25],
                             'source': 'PP-OCRv6-fixture-only', 'confidence': .99, 'kind': 'text'})
    image.save(path)
    sha = digest(path)
    detail, row, index, native_item = fixture(module)
    original = detail['result']['original']
    original['tables'] = [{'rows': 3, 'columns': 3, 'cells': cells}]
    original['screen']['input_sha256'] = sha
    edited = {'text': '', 'tables': deepcopy(original['tables'])}
    preparation = {'input_path': str(path), 'input_sha256': sha, 'width': 370, 'height': 195,
        'business_regions': [{'id': 'synthetic-grid', 'table_box': [30, 30, 340, 165],
            'grid_reliable': True, 'sources': ['pixel_grid'], 'observed_grid_rows': 3,
            'observed_grid_columns': 3, 'observed_grid': {'horizontal_lines': ys, 'vertical_lines': xs,
                'source': 'pixel_grid', 'range_semantics': 'observed_line_positions',
                'coordinate_space': 'exif_normalized_original'}}]}
    evidence = {'text': readings, 'provenance': {'image_version': 'v1', 'contains_reference_input': False}}
    native_item.update(preparation=json.dumps(preparation), evidence=json.dumps(evidence))
    row.update(native_version_sha256=sha, result_original_sha256=fingerprint(original),
               result_edited_sha256=fingerprint(edited), selected_candidate=deepcopy(original), edited_view=deepcopy(edited))
    detail.update(preparation=preparation)
    detail['result']['edited'] = edited
    detail['diagnostics'] = module.build_screen_diagnostics({**original, **edited}, preparation, evidence,
        remaining_seconds=10, capabilities={'local_reread': False, 'precision_subbatch': True, 'structure_compare': False})
    detail['diagnostics'].update(result_id='r1', result_revision=0, input_version_id='v1')
    return detail, row, index, native_item


class DiagnosticGates(unittest.TestCase):
    def validate(self, detail, row, index, item, module):
        return validate_detail(detail, row, index, item, module,
            receipt_path=OUTPUT / 'fixture-detail.json', receipt_sha256='fixture-not-a-model-receipt')

    def test_current_detail_localizes_without_changing_review_or_delivery(self):
        with diagnostic_source(SOURCE) as module:
            detail, row, index, item = fixture(module)
            validated = self.validate(detail, row, index, item, module)
        self.assertEqual(len(validated['review_regions']), 1)
        region = validated['review_regions'][0]
        self.assertEqual((region['scope'], region['table_index'], region['row'], region['column']), ('cell', 0, 1, 1))
        self.assertTrue(region['reliable'])
        record = normalize_native_index(index, {'s1': validated})['records'][0]
        self.assertTrue(record['review_image'])
        self.assertFalse(record['automatic_delivery'])
        self.assertFalse(record['human_confirmed'])
        self.assertFalse(validated['automatic_delivery_qualification'])

    def test_grid_cell_replay_checks_complete_pixels_topology_and_anchor_proof(self):
        with diagnostic_source(SOURCE) as module:
            detail, row, index, item = grid_fixture(module)
            validated = self.validate(detail, row, index, item, module)
        self.assertEqual(validated['version'], 'screen-diagnostics-v2')
        self.assertTrue(validated['grid_localization_replayed'])
        self.assertEqual(validated['observed_grid_cell_count'], 9)
        self.assertTrue(all(region['reliable'] for region in validated['review_regions']))
        self.assertFalse(validated['automatic_delivery_qualification'])
        self.assertFalse(validated['human_review_qualification'])

    def test_rectified_grid_replay_requires_inverse_and_actual_source_polygon(self):
        import cv2
        import numpy as np
        from PIL import Image
        with diagnostic_source(SOURCE) as module:
            from ocr_workbench.coordinates import bounds,box_polygon,inverse,polygon
            detail,row,index,item=grid_fixture(module)
            prep=detail['preparation'];path=fpath(prep['input_path'])
            transform=[1.,.03,20.,.01,1.,20.,0.,0.,1.]
            with Image.open(path) as opened:
                image=Image.fromarray(cv2.warpPerspective(np.asarray(opened),np.asarray(transform).reshape(3,3),
                    (440,260),flags=cv2.INTER_CUBIC,borderValue=(255,255,255)))
            image.save(path);sha=digest(path)
            evidence=json.loads(item['evidence'])
            for reading in evidence['text']:
                reading['polygon']=polygon(transform,box_polygon(reading['box']))
                reading['box']=bounds(reading['polygon'])
            prep.update(input_sha256=sha,width=440,height=260,
                business_regions=[{'id':'synthetic-grid','table_box':bounds(polygon(transform,box_polygon([30,30,340,165])))}],
                prepared_images=[{'kind':'perspective','width':370,'height':195,
                    'source_to_prepared':inverse(transform),'prepared_to_source':transform,
                    'source_polygon':polygon(transform,box_polygon([0,0,370,195]))}])
            original=detail['result']['original'];original['screen']['input_sha256']=sha
            raw={**original,**detail['result']['edited']}
            item.update(preparation=json.dumps(prep),evidence=json.dumps(evidence))
            row.update(native_version_sha256=sha,result_original_sha256=fingerprint(original),selected_candidate=deepcopy(original))
            detail['diagnostics']=module.build_screen_diagnostics(raw,prep,evidence,remaining_seconds=10)
            detail['diagnostics'].update(result_id='r1',result_revision=0,input_version_id='v1')
            validated=self.validate(detail,row,index,item,module)
            self.assertEqual(validated['observed_grid_cell_count'],9)
            diagnostic=detail['diagnostics']
            self.assertEqual(diagnostic['grid_localization']['version'],'screen-grid-localization-v2')
            cells=[part for part in diagnostic['items'] if part['location']['level']=='cell']
            self.assertTrue(cells)
            self.assertNotEqual(cells[0]['location']['polygon'],box_polygon(cells[0]['location']['box']))
            for alteration in ('inverse','source_polygon','display_polygon'):
                with self.subTest(alteration=alteration):
                    changed=deepcopy(detail)
                    proof=changed['diagnostics']['grid_localization']['tables'][0]
                    if alteration=='inverse':proof['rectification']['prepared_to_source'][2]+=1
                    elif alteration=='source_polygon':proof['cells'][0]['polygon'][0][0]+=1
                    else:
                        target=next(part for part in changed['diagnostics']['items'] if part['location']['level']=='cell')
                        target['location']['polygon'][0][0]+=1
                    with self.assertRaises(ValueError):self.validate(changed,row,index,item,module)

    def test_grid_proof_tampering_or_source_drift_cannot_certify_locations(self):
        with diagnostic_source(SOURCE) as module:
            for kind in ('source_hash', 'cell_box', 'anchors', 'partition', 'source_bytes', 'missing_proof', 'version'):
                with self.subTest(kind=kind):
                    detail, row, index, item = grid_fixture(module)
                    proof = detail['diagnostics']['grid_localization']
                    if kind == 'source_hash': proof['binding']['input_sha256'] = '0' * 64
                    elif kind == 'cell_box': proof['tables'][0]['cells'][0]['box'][0] += 1
                    elif kind == 'anchors': proof['tables'][0]['anchors'][0]['text'] = 'fabricated'
                    elif kind == 'partition': proof['tables'][0]['pixel_observation']['cells'][0]['column_span'] = 2
                    elif kind == 'source_bytes':
                        from PIL import Image
                        Image.new('RGB', (370, 195), 'black').save(detail['preparation']['input_path'])
                    elif kind == 'missing_proof': detail['diagnostics'].pop('grid_localization')
                    else: detail['diagnostics']['version'] = 'screen-diagnostics-v1'
                    with self.assertRaises(ValueError):
                        self.validate(detail, row, index, item, module)

    def test_source_or_result_identity_tampering_rejected(self):
        mutations = {'sample': lambda d: d.update(sample_id='s2'),
                     'source': lambda d: d.update(source_image_sha256='0' * 64),
                     'input': lambda d: d.update(input_version_id='v2'),
                     'image': lambda d: d.update(image_id='im2'),
                     'revision': lambda d: d.update(result_revision=1),
                     'current_revision': lambda d: d.update(current_result_revision=1),
                     'stale': lambda d: d.update(review_stale=True),
                     'result_original': lambda d: d['result']['original']['screen'].update(batch_id='other'),
                     'result_edited': lambda d: d['result']['edited']['tables'][0]['cells'][3].update(text='-20.00'),
                     'diagnostic_revision': lambda d: d['diagnostics'].update(result_revision=1),
                     'table_digest': lambda d: d['diagnostics']['binding'].update(table_snapshot_sha256='0' * 64),
                     'candidate': lambda d: d['diagnostics']['binding'].update(candidate_id='other'),
                     'geometry': lambda d: d['diagnostics']['items'][0]['location'].update(box=[1, 2, 3, 4]),
                     'issue_reason': lambda d: d['diagnostics']['items'][0].update(reason='fabricated'),
                     'omitted_issue': lambda d: d['diagnostics'].update(items=[]),
                     'preparation': lambda d: d['preparation'].update(width=200),
                     'budget': lambda d: d['diagnostics']['budget'].update(remaining_seconds=21)}
        with diagnostic_source(SOURCE) as module:
            for kind, mutate in mutations.items():
                with self.subTest(kind=kind):
                    detail, row, index, item = fixture(module)
                    mutate(detail)
                    with self.assertRaises(ValueError):
                        self.validate(detail, row, index, item, module)

    def test_db_source_candidate_and_reference_evidence_tampering_rejected(self):
        with diagnostic_source(SOURCE) as module:
            for kind in ('batch', 'sources', 'candidate', 'table', 'reference_input'):
                with self.subTest(kind=kind):
                    detail, row, index, item = fixture(module)
                    if kind == 'batch': item['batch_id'] = 'other'
                    elif kind == 'sources': row['source_receipts'] = []
                    elif kind == 'candidate': row['selected_candidate_id'] = 'other'
                    elif kind == 'table':
                        detail['result']['edited']['tables'][0]['cells'][3]['text'] = 'changed'
                        row['result_edited_sha256'] = fingerprint(detail['result']['edited'])
                    else: item['evidence'] = json.dumps({'provenance': {'contains_reference_input': True}})
                    with self.assertRaises(ValueError):
                        self.validate(detail, row, index, item, module)

    def test_self_reported_reliable_without_full_cell_geometry_stays_table_review(self):
        with diagnostic_source(SOURCE) as module:
            detail, row, index, item = fixture(module, geometry=False)
            detail['diagnostics']['items'][0]['reliable'] = True
            validated = self.validate(detail, row, index, item, module)
        self.assertEqual(validated['review_regions'][0]['scope'], 'table')
        self.assertFalse(validated['review_regions'][0]['reliable'])
        self.assertFalse(validated['automatic_delivery_qualification'])

    def test_frozen_diagnostic_module_binding_rejected(self):
        with self.assertRaises(ValueError), diagnostic_source(SOURCE, {}):
            pass

    def test_actual_platform_path_to_url_presentation_is_checked(self):
        with diagnostic_source(SOURCE) as module:
            detail, row, index, item = fixture(module)
            original = json.loads(item['preparation'])
            original['prepared_images'] = [{'id': 'p1', 'path': 'F:/fixture/p1.png', 'sha256': 'c' * 64}]
            item['preparation'] = json.dumps(original)
            detail['preparation'] = deepcopy(original)
            image = detail['preparation']['prepared_images'][0]
            image.pop('path')
            image['url'] = '/api/platform/v1/workspaces/w1/ocr/screen-batches/b1/items/v1/files/p1'
            self.assertTrue(self.validate(detail, row, index, item, module)['source_replay_verified'])
            image['url'] = image['url'].replace('/items/v1/', '/items/v2/')
            with self.assertRaises(ValueError): self.validate(detail, row, index, item, module)
            image['url'] = image['url'].replace('/items/v2/', '/items/v1/')
            image['sha256'] = '0' * 64
            with self.assertRaises(ValueError): self.validate(detail, row, index, item, module)

    def test_detail_sha_mutation_legacy_absence_and_wrong_scope(self):
        with tempfile.TemporaryDirectory(dir=OUTPUT) as temporary, diagnostic_source(SOURCE) as module:
            root = fpath(temporary)
            folder = root / 'native-receipts'; folder.mkdir()
            detail, row, index, item = fixture(module)
            files = {str(path.relative_to(SOURCE)).replace('\\', '/'): digest(path)
                     for path in (SOURCE / 'src/ocr_workbench').glob('*.py')}
            frozen_json(root / 'prepared.json', {'native_source': str(SOURCE), 'native_source_files': files})
            path = folder / 's1.json'; frozen_json(path, detail)
            index.update(native_run=str(root), native_flavor='agent', prepared_sha256=digest(root / 'prepared.json'),
                         detail_receipt_sha256={str(path): digest(path)})
            tables = {'screen_items': [item]}
            self.assertTrue(collect_diagnostics(index, tables)['s1']['source_replay_verified'])
            path.write_text(json.dumps(detail) + '\n ', encoding='utf-8')
            with self.assertRaises(ValueError): collect_diagnostics(index, tables)
            detail.pop('diagnostics'); path.write_text(json.dumps(detail), encoding='utf-8')
            index['detail_receipt_sha256'][str(path)] = digest(path)
            with self.assertRaises(ValueError): collect_diagnostics(index, tables)
            # A genuine old reader has no diagnostic-producing detail method.
            # This synthetic legacy inventory deliberately omits screen_batches.
            files.pop('src/ocr_workbench/screen_batches.py')
            (root / 'prepared.json').write_text(json.dumps({'native_source': str(SOURCE), 'native_source_files': files}), encoding='utf-8')
            index['prepared_sha256'] = digest(root / 'prepared.json')
            self.assertEqual(collect_diagnostics(index, tables), {})
            index['detail_receipt_sha256'] = {}
            with self.assertRaises(ValueError): collect_diagnostics(index, tables)
            with self.assertRaises(ValueError): normalize_native_index(index, {'unknown': {}})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    OUTPUT = fpath(args.output_root); OUTPUT.mkdir(parents=True, exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DiagnosticGates))
    frozen_json(args.receipt, {'format': FORMAT, 'status': 'passed' if result.wasSuccessful() else 'failed',
        'tests_run': result.testsRun, 'errors': len(result.errors), 'failures': len(result.failures),
        'scope': 'CPU source-bound diagnostic tamper gates only; no actual native run/model/Agent/acceptance evidence',
        'diagnostic_source': str(SOURCE),
        'source_files': {path.name: digest(path) for path in (SOURCE / 'src/ocr_workbench').glob('screen_*.py')},
        'failure_details': [{'test': str(case), 'traceback': trace} for case, trace in result.errors + result.failures]})
    raise SystemExit(0 if result.wasSuccessful() else 1)
