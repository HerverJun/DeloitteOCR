"""Independent visual targets must retain actual image/context/detector lineage."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from PIL import Image


DRIVER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/screen_geometry_visual_inputs.py'))
TEMP_ROOT = Path('F:/OCR/output/ocr-screen-benchmark-20261007/temp')


class GeometryVisualInputTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='independent-visual-', dir=TEMP_ROOT)
        self.root = Path(self.temp.name)
        self.experiment = self.root / 'actual-run'
        self.folder = self.experiment / 'image-01'
        (self.folder / 'geometry-paddle').mkdir(parents=True)
        (self.folder / 'context/prepared').mkdir(parents=True)
        self.image = self.folder / 'normalized.png'
        Image.new('RGB', (200, 60), 'white').save(self.image)
        self.context = self.folder / 'context/context.json'
        self.write(self.context, {'component':'actual-context-fixture'})
        self.manifest = self.folder / 'context/prepared/screen-preparation.json'
        self.write(self.manifest, {'width':200,'height':60,'business_regions':[
            {'id':'region-1','box':[0,0,200,60],'table_box':[0,0,200,60],
             'sources':['layout_table'],'score':.95}]})
        self.request = self.folder / 'geometry-paddle/actual-input-request.json'
        self.write(self.request, {'image_version':DRIVER['digest'](self.image),
            'image_sha256':DRIVER['digest'](self.image),
            'actual_context_sha256':DRIVER['digest'](self.context),
            'preparation_manifest_sha256':DRIVER['digest'](self.manifest),
            'regions':[{'id':'region-1','box':[0,0,200,60]}]})
        self.observed = self.folder / 'geometry-paddle/observed.json'
        self.adapted = self.folder / 'geometry-paddle/adapted-geometry.json'
        boxes = [[0,0,100,30],[100,0,200,30],[0,30,100,60],[100,30,200,60]]
        self.raw = {'status':'success','component':'paddle-table-v2','model_revisions':{},'tables':[
            {'region_id':'region-1','table_box':[0,0,200,60],
             'raw':{'det':{'boxes':[{'coordinate':box,'score':.95} for box in boxes]}},
             'final':{'pred_html':'<table><tr><td>金额</td><td>比例</td></tr><tr><td>-125.50</td><td>12.5%</td></tr></table>',
                      'cell_box_list':deepcopy(boxes)}}]}
        self.save_geometry()
        adapter = Path(DRIVER['ROOT']) / 'src/ocr_workbench/geometry_providers.py'
        self.write(self.experiment / 'receipt.json', {'geometry_worker':{'files':{'geometry_providers.py':DRIVER['digest'](adapter)}},
            'rows':[{'id':'actual-only','image':str(self.image),'image_sha256':DRIVER['digest'](self.image),
                     'normalized_image':str(self.image),'normalized_sha256':DRIVER['digest'](self.image),'directory':'image-01',
                     'result':'F:/unopened-hunyuan-result.json','truth':'F:/unopened-truth.json'}],
            'stages':[{'id':'actual-only','stage':'geometry-paddle','status':'succeeded'}]})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.write_text(json.dumps(value,ensure_ascii=False), 'utf-8')

    def save_geometry(self):
        self.write(self.observed, self.raw)
        adapted = DRIVER['plain'](DRIVER['paddle_tables'](self.raw,200,60))
        self.write(self.adapted, adapted)

    def test_actual_numeric_full_cells_are_independent_and_blind(self):
        prepared = DRIVER['prepare'](self.experiment,self.root/'prepared')
        self.assertFalse(prepared['truth_read'])
        self.assertFalse(prepared['historical_ocr_candidates_opened'])
        self.assertFalse(prepared['gpu_started'])
        snapshot = json.loads(Path(prepared['items'][0]['snapshot']).read_text('utf-8'))
        self.assertEqual(len(snapshot['targets']),2)
        self.assertEqual([r['before'] for r in snapshot['targets']],['',''])
        self.assertEqual([r['evidence']['range_semantics'] for r in snapshot['targets']],['full_cell','full_cell'])
        self.assertEqual([r['before'] for r in snapshot['source_targets']],['-125.50','12.5%'])
        for source in snapshot['source_targets']:
            self.assertTrue(source['independent_cell_id'].startswith('paddle:'))
            self.assertTrue(source['geometry_trace']['derivation'][0]['full_cell_verified_lineage'])
        self.assertEqual(snapshot['provenance']['actual_context']['sha256'],DRIVER['digest'](self.context))

    def test_changed_geometry_cannot_be_used_even_with_a_good_looking_box(self):
        adapted = json.loads(self.adapted.read_text('utf-8'))
        adapted[0]['cells'][2]['text']='999.99'
        self.write(self.adapted,adapted)
        with self.assertRaisesRegex(ValueError,'differs from actual'):
            DRIVER['prepare'](self.experiment,self.root/'rejected')
        self.assertFalse((self.root/'rejected').exists())

    def test_changed_context_breaks_actual_input_binding(self):
        self.write(self.context,{'component':'changed-context'})
        with self.assertRaisesRegex(ValueError,'binding changed'):
            DRIVER['prepare'](self.experiment,self.root/'rejected')

    def test_request_cannot_inject_ocr_evidence_into_an_unchanged_context(self):
        request=json.loads(self.request.read_text('utf-8'))
        request['ocr_blocks']=[{'text':'999.99'}]
        self.write(self.request,request)
        with self.assertRaisesRegex(ValueError,'differs from the actual bound context'):
            DRIVER['prepare'](self.experiment,self.root/'rejected')

    def test_short_or_decimal_identifiers_and_dates_are_not_amount_targets(self):
        self.raw['tables'][0]['final']['pred_html'] = '<table><tr><td>编号</td><td>日期</td></tr><tr><td>12</td><td>2026.10</td></tr></table>'
        self.save_geometry()
        prepared=DRIVER['prepare'](self.experiment,self.root/'identity')
        self.assertEqual(prepared['items'][0]['targets'],0)

    def test_postprocessed_unverified_numeric_boxes_skip_visual_targets(self):
        for box in self.raw['tables'][0]['final']['cell_box_list'][2:]:
            box[0] += 1  # actual output differs from the detector; no exact identity
        self.save_geometry()
        prepared = DRIVER['prepare'](self.experiment,self.root/'unverified')
        self.assertEqual(prepared['items'][0]['status'],'skipped')
        self.assertEqual(prepared['items'][0]['targets'],0)

    def test_strict_probe_budget_and_no_reusing_output(self):
        for limit in (True,0,3):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                DRIVER['prepare'](self.experiment,self.root/'invalid',limit)
        DRIVER['prepare'](self.experiment,self.root/'prepared',1)
        with self.assertRaises(FileExistsError):
            DRIVER['prepare'](self.experiment,self.root/'prepared',1)


if __name__ == '__main__':
    unittest.main()
