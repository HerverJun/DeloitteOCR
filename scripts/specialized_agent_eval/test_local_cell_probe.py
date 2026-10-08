"""CPU contract tests; synthetic cells never count as native model evidence."""
from __future__ import annotations
import argparse
from copy import deepcopy
import io
import json
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from common import digest, fingerprint, fpath, frozen_json
from native_diagnostics import diagnostic_source
from local_cell_probe import (crop_cell, focus_categories, no_reference, prepare,
    source_rgb, validate_grid, validate_prepared)

SOURCE = fpath('F:/OCR/output/platform-specialized-agent-20261007/worktrees/ocr')
ROOT = fpath('F:/OCR/output/platform-specialized-agent-20261007')
OUTPUT = None


def fixture(directory, module):
    from PIL import Image, ImageDraw
    directory.mkdir(parents=True, exist_ok=False)
    path = directory/'fixture-only.png'
    xs, ys = [30,125,220,340], [30,70,115,165]
    image = Image.new('RGB',(370,195),'white')
    draw = ImageDraw.Draw(image)
    for x in xs:
        draw.line((x,ys[0],x,ys[-1]), fill='black', width=2)
    for y in ys:
        draw.line((xs[0],y,xs[-1],y), fill='black', width=2)
    values = [['ID','Item','Amount'],['000123','alpha','10.00'],['000124','beta','20.00']]
    cells, readings = [], []
    for r in range(3):
        for c in range(3):
            draw.text((xs[c]+8,ys[r]+9),values[r][c],fill='black')
            cells.append({'row':r,'column':c,'row_span':1,'column_span':1,
                'text':'[unreadable]' if (r,c)==(1,2) else values[r][c]})
            readings.append({'text':values[r][c],'box':[xs[c]+8,ys[r]+8,xs[c]+70,ys[r]+25],
                'source':'PP-OCRv6-fixture-only','confidence':.99,'kind':'text'})
    image.save(path)
    prep={'input_path':str(path),'input_sha256':digest(path),'width':370,'height':195,
        'business_regions':[{'id':'fixture-grid','table_box':[30,30,340,165],'grid_reliable':True,
            'sources':['pixel_grid'],'observed_grid_rows':3,'observed_grid_columns':3,
            'observed_grid':{'horizontal_lines':ys,'vertical_lines':xs,'source':'pixel_grid',
                'range_semantics':'observed_line_positions','coordinate_space':'exif_normalized_original'}}]}
    aux={'text':readings,'provenance':{'image_version':'fixture-v1','contains_reference_input':False}}
    raw={'engine':'fixture-only','project_image_version':'fixture-v1','text':'','tables':[{'rows':3,'columns':3,'cells':cells}],
        'screen':{'input_sha256':digest(path),'prepared':prep,'business_metadata_evidence':{'auxiliary_evidence':aux}}}
    diagnostics=module.build_screen_diagnostics(raw,prep,aux)
    return raw,prep,aux,diagnostics


def write_case(directory, raw, prep, aux, diagnostics):
    id='development-fixture-only'
    candidate={'original':raw,'edited':{}}
    record={'id':id,'image_sha256':prep['input_sha256'],'candidate':candidate}
    observation=directory/'observations.json'
    frozen_json(observation,{'records':[record],'evidence_kind':'synthetic-test-only'})
    names=('__init__','screen_diagnostics','screen_quality','screen_evidence','screen_imaging','image_utils','coordinates','tables')
    sources=[{'source':str(SOURCE/f'src/ocr_workbench/{name}.py'),
        'frozen':str(SOURCE/f'src/ocr_workbench/{name}.py'),'sha256':digest(SOURCE/f'src/ocr_workbench/{name}.py')} for name in names]
    receipts={}
    for route in ('hunyuan','glm'):
        receipt={'format':'development-source-bound-grid-diagnostic-cpu-replay-v1','route':route,'id':id,
            'source_observations':str(observation),'source_observations_sha256':digest(observation),
            'source_record_sha256':fingerprint(record),'candidate_sha256':fingerprint(candidate),
            'preparation_sha256':fingerprint(prep),'auxiliary_sha256':fingerprint(aux),
            'input_path':prep['input_path'],'input_sha256':prep['input_sha256'],
            'source_files_sha256':{row['source']:row['sha256'] for row in sources},
            'diagnostics':diagnostics,'truth_read':False,'gpu_or_model_executed':False}
        receipts[route]=directory/f'{route}-receipt.json'
        frozen_json(receipts[route],receipt)
    pair=directory/'pair.json'
    frozen_json(pair,{'id':id,'input_sha256':prep['input_sha256'],'truth_read':False,'selection_applied':False,
        'values_modified':False,'hunyuan_diagnostic_receipt':str(receipts['hunyuan']),
        'glm_diagnostic_receipt':str(receipts['glm']),'hunyuan_candidate_sha256':fingerprint(candidate),
        'glm_candidate_sha256':fingerprint(candidate),'comparison':{'diagnostics':{'items':diagnostics['items']}}})
    summary=directory/'summary.json'
    frozen_json(summary,{'sources':sources,'source_bindings_unchanged':True,'truth_read':False,
        'validation_or_holdout_read':False,'comparisons':[{'id':id,'receipt_path':str(pair)}],
        'route_summaries':[{'route':'hunyuan','source_cell_count':9},{'route':'glm','source_cell_count':9}]})
    inputs=directory/'inputs.json'
    frozen_json(inputs,{'truth_available_to_inference':False,'samples':[{'id':id,'image':prep['input_path'],
        'image_sha256':prep['input_sha256']}]})
    return summary,inputs,ROOT/'runs/agent-native-glm-dev-001/prepared.json'


class LocalProbeContracts(unittest.TestCase):
    def setUp(self):
        self.directory=OUTPUT/self._testMethodName
        with diagnostic_source(SOURCE) as module:
            self.raw,self.prep,self.aux,self.diag=fixture(self.directory,module)

    def test_complete_source_proof_only_creates_verified_cell_targets(self):
        before=fingerprint((self.raw,self.prep,self.aux,self.diag))
        with diagnostic_source(SOURCE) as module:
            grid=validate_grid(self.raw,self.prep,self.aux,self.diag,module)
        self.assertEqual(len(grid[0]['cells']),9)
        self.assertEqual(grid[0]['cells'][(2,2,1,1)]['box'],[220.0,115.0,340.0,165.0])
        self.assertEqual(before,fingerprint((self.raw,self.prep,self.aux,self.diag)))

    def test_captured_grid_pixel_anchor_or_topology_tampering_is_rejected(self):
        mutations=(
            lambda raw,prep,aux,diag:diag['grid_localization']['tables'][0]['cells'][0]['box'].__setitem__(0,31),
            lambda raw,prep,aux,diag:diag['grid_localization']['tables'][0]['anchors'][0].__setitem__('confidence',.95),
            lambda raw,prep,aux,diag:raw['tables'][0]['cells'][0].__setitem__('row_span',2),
            lambda raw,prep,aux,diag:aux['text'][0].__setitem__('text','different'),
            lambda raw,prep,aux,diag:prep.__setitem__('input_sha256','f'*64),
            lambda raw,prep,aux,diag:prep.__setitem__('width',371),
            lambda raw,prep,aux,diag:diag.__setitem__('version','screen-diagnostics-v1'),
            lambda raw,prep,aux,diag:diag.pop('grid_localization'),
        )
        with diagnostic_source(SOURCE) as module:
            for mutation in mutations:
                with self.subTest(mutation=str(mutation)):
                    raw,prep,aux,diag=deepcopy((self.raw,self.prep,self.aux,self.diag))
                    mutation(raw,prep,aux,diag)
                    with self.assertRaises((ValueError,KeyError)):
                        validate_grid(raw,prep,aux,diag,module)

    def test_source_pixels_changed_after_proof_are_rejected(self):
        path=Path(self.prep['input_path'])
        path.write_bytes(path.read_bytes()+b'changed')
        with diagnostic_source(SOURCE) as module:
            with self.assertRaisesRegex(ValueError,'digest'):
                validate_grid(self.raw,self.prep,self.aux,self.diag,module)

    def test_reference_payload_is_rejected_even_when_nested_in_tuple(self):
        with self.assertRaises(ValueError):
            no_reference(({'safe':[]},{'nested':[{'contains_reference_input':True}]}))
        no_reference(({'safe':[{'contains_reference_input':False}]},))

    def test_unverified_grid_is_no_cell_target_and_never_uniformly_guessed(self):
        self.prep['business_regions'][0]['observed_grid']['vertical_lines']=[30,130,230,340]
        with diagnostic_source(SOURCE) as module:
            diag=module.build_screen_diagnostics(self.raw,self.prep,self.aux)
            self.assertEqual(validate_grid(self.raw,self.prep,self.aux,diag,module),{})

    def test_crop_is_source_pixels_with_explicit_scale_and_coordinate_transform(self):
        geometry=self.diag['grid_localization']['tables'][0]['cells'][0]
        transform=crop_cell(source_rgb(self.prep['input_path']),geometry,self.directory/'crop.png')
        self.assertEqual(transform['integer_including_bbox'],[30,30,125,70])
        self.assertEqual(transform['source_crop_size'],[95,40])
        self.assertEqual(transform['output_size'],[349,184])
        self.assertEqual(transform['source_to_crop_affine'],[3,0,-58,0,3,-58])
        self.assertFalse(transform['pixels_reconstructed_or_corrected'])
        self.assertEqual(digest(transform['crop_path']),transform['crop_sha256'])
        with self.assertRaises(ValueError):
            crop_cell(source_rgb(self.prep['input_path']),geometry,self.directory/'crop.png')

    def test_crop_rejects_exterior_and_excessive_work(self):
        geometry=deepcopy(self.diag['grid_localization']['tables'][0]['cells'][0])
        for box in ([-1,30,125,70],[30,30,380,70],[30,30,30,70]):
            geometry['box']=box
            with self.assertRaises(ValueError):
                crop_cell(source_rgb(self.prep['input_path']),geometry,self.directory/'bad.png')
        geometry['box']=[30,30,125,70]
        for options in ({'scale':5},{'scale':True},{'border':65},{'border':-1}):
            with self.assertRaises(ValueError):
                crop_cell(source_rgb(self.prep['input_path']),geometry,self.directory/'bad.png',**options)

    def test_focus_preserves_shared_symbol_and_glyph_risks_without_votes(self):
        glyph=focus_categories(['OBFCI','OBFC1'],False)
        self.assertIn('letter_digit_ambiguity',glyph)
        shared=focus_categories(['$ -0.013 $','$ -0.013 $'],False)
        self.assertIn('numeric_or_mathematical_symbol',shared)
        self.assertNotIn('native_candidate_literal_difference',shared)
        self.assertIn('verified_cell_control',focus_categories(['项目','项目'],False))

    def test_cpu_preparation_and_model_validation_keep_full_denominator_and_no_edits(self):
        case=write_case(self.directory,self.raw,self.prep,self.aux,self.diag)
        manifest=prepare(*case,self.directory/'probe')
        validate_prepared(manifest)
        self.assertEqual(manifest['task_count'],9)
        self.assertEqual(len(list((self.directory/'probe/crops').glob('*.png'))),9)
        self.assertFalse(manifest['gpu_or_model_executed'])
        self.assertFalse(manifest['candidate_selection_modified'])
        self.assertFalse(manifest['native_review_or_delivery_modified'])
        self.assertFalse(manifest['local_reread_capability_enabled'])
        for mutation in (
            lambda m:m['tasks'][0]['geometry']['box'].__setitem__(0,31),
            lambda m:m['tasks'][0]['transform'].__setitem__('scale',2),
            lambda m:m['tasks'][0].__setitem__('automatic_apply',True),
            lambda m:m['tasks'][0]['candidate_values'].__setitem__('hunyuan','changed'),
            lambda m:m['tasks'][0].__setitem__('full_grid_proof_sha256','0'*64),
            lambda m:m.__setitem__('local_reread_capability_enabled',True),
            lambda m:(m['tasks'].pop(),m.__setitem__('task_count',8)),
        ):
            with self.subTest(mutation=str(mutation)):
                bad=deepcopy(manifest)
                mutation(bad)
                with self.assertRaises(ValueError):
                    validate_prepared(bad)

    def test_preparation_rejects_wrong_split_or_changed_receipt_before_cropping(self):
        case=write_case(self.directory,self.raw,self.prep,self.aux,self.diag)
        inputs=json.loads(case[1].read_text('utf-8'))
        inputs['samples'][0]['id']='holdout-fixture-only'
        case[1].write_text(json.dumps(inputs),'utf-8')
        with self.assertRaises(ValueError):
            prepare(*case,self.directory/'must-not-exist')
        self.assertFalse((self.directory/'must-not-exist').exists())


def main():
    global OUTPUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-root',required=True)
    parser.add_argument('--receipt',required=True)
    args=parser.parse_args()
    OUTPUT=fpath(args.output_root)
    OUTPUT.mkdir(parents=True,exist_ok=False)
    sources=[Path(__file__).resolve(),Path(__file__).with_name('local_cell_probe.py')]
    before={str(path):digest(path) for path in sources}
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(LocalProbeContracts)
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    text=stream.getvalue()
    receipt=fpath(args.receipt)
    log=receipt.with_suffix('.log')
    log.write_text(text,encoding='utf-8')
    after={str(path):digest(path) for path in sources}
    success=result.wasSuccessful() and before==after
    value={'format':'local-cell-probe-cpu-contract-v1','passed':success,'tests':result.testsRun,
        'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'helper_source_before':before,'helper_source_after':after,'source_unchanged':before==after,
        'synthetic_fixtures_only':True,'gpu_or_model_executed':False,'native_results_modified':False,
        'log':str(log),'output_root':str(OUTPUT)}
    frozen_json(receipt,value)
    xml=ET.Element('testsuite',name='local_cell_probe_cpu_contract',tests=str(result.testsRun),
        failures=str(len(result.failures)),errors=str(len(result.errors)),skipped=str(len(result.skipped)))
    for case,message in result.failures+result.errors:
        child=ET.SubElement(xml,'testcase',name=str(case))
        ET.SubElement(child,'failure').text=message
    ET.ElementTree(xml).write(receipt.with_suffix('.xml'),encoding='utf-8',xml_declaration=True)
    print(text)
    print(json.dumps(value,ensure_ascii=False))
    raise SystemExit(0 if success else 1)


if __name__=='__main__':
    main()
