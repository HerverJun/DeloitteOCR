"""Cell locations require pixels, complete topology and independent anchors."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from PIL import Image,ImageDraw

from ocr_workbench.screen_diagnostics import observe_screen_grid_cells,build_screen_diagnostics
from ocr_workbench.screen_evidence import build_screen_visual_snapshot
from ocr_workbench.screen_quality import assess_screen_result


class ScreenGridLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'grid.png'
        self.xs=[100,185,310,470]
        self.ys=[200,242,305,390]

    def fixture(self, *, merged=False):
        image=Image.new('RGB',(600,500),'white');draw=ImageDraw.Draw(image)
        for y in self.ys:draw.line((self.xs[0],y,self.xs[-1],y),fill='black',width=2)
        for x in self.xs:draw.line((x,self.ys[0],x,self.ys[-1]),fill='black',width=2)
        cells=[{'row':row,'column':column,'row_span':1,'column_span':1,'text':f'item-{row}-{column}'}
               for row in range(3) for column in range(3)]
        if merged:
            draw.rectangle((self.xs[1]-3,self.ys[0]+4,self.xs[1]+4,self.ys[1]-4),fill='white')
            cells[0]['column_span']=2;cells.pop(1)
        image.save(self.path)
        image_sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        readings=[]
        for cell in cells:
            r,c,rs,cs=[cell[key] for key in ('row','column','row_span','column_span')]
            readings.append({'text':cell['text'],'box':[self.xs[c]+8,self.ys[r]+8,
                min(self.xs[c+cs]-8,self.xs[c]+65),self.ys[r]+27],
                'confidence':.99,'source':'PP-OCRv6-auxiliary','kind':'text'})
        raw={'tables':[{'rows':3,'columns':3,'cells':cells}], 'screen':{'input_sha256':image_sha}}
        prep={'input_path':str(self.path),'input_sha256':image_sha,'width':600,'height':500,
            'business_regions':[{'id':'grid','table_box':[self.xs[0],self.ys[0],self.xs[-1],self.ys[-1]],
                'sources':['pixel_grid'],'grid_reliable':True,'observed_grid_rows':3,'observed_grid_columns':3,
                'observed_grid':{'horizontal_lines':self.ys[:],'vertical_lines':self.xs[:],
                    'source':'pixel_grid','range_semantics':'observed_line_positions',
                    'coordinate_space':'exif_normalized_original'}}]}
        evidence={'text':readings,'provenance':{'contains_reference_input':False}}
        raw['screen']['business_metadata_evidence']={'auxiliary_evidence':deepcopy(evidence)}
        return raw,prep,evidence

    def cell_issue(self,row=1,column=1):
        return {'code':'numeric_conflict','severity':'high','upgrade_kind':'text','table':0,'row':row,'column':column}

    def test_observed_nonuniform_boundaries_and_independent_anchors_produce_cell_locations(self):
        raw,prep,evidence=self.fixture();before=deepcopy((raw,prep,evidence))
        mapping=observe_screen_grid_cells(raw,prep,evidence)
        self.assertEqual(mapping['cell_count'],9)
        self.assertEqual(mapping['tables'][0]['cells'][4]['box'],[185,242,310,305])
        self.assertEqual(mapping['binding']['input_sha256'],prep['input_sha256'])
        report=build_screen_diagnostics(raw,prep,evidence,assessment={'issues':[self.cell_issue()]})
        self.assertEqual(report['items'][0]['location']['level'],'cell')
        self.assertEqual(report['items'][0]['location']['box'],[185,242,310,305])
        self.assertEqual((raw,prep,evidence),before)
        self.assertFalse(mapping['values_modified'])

    def test_merge_is_observed_from_absent_interior_edges_before_candidate_comparison(self):
        raw,prep,evidence=self.fixture(merged=True)
        mapping=observe_screen_grid_cells(raw,prep,evidence)
        self.assertEqual(mapping['cell_count'],8)
        merged=mapping['tables'][0]['cells'][0]
        self.assertEqual((merged['row_span'],merged['column_span']),(1,2))
        self.assertEqual(merged['box'],[100,200,310,242])
        raw['tables'][0]['cells'][0]['column_span']=1
        self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_dimensions_or_partition_conflicts_never_use_matching_logical_indices(self):
        for alteration in ('columns','span','duplicate','missing'):
            with self.subTest(alteration=alteration):
                raw,prep,evidence=self.fixture()
                if alteration=='columns':raw['tables'][0]['columns']=4
                elif alteration=='span':raw['tables'][0]['cells'][0]['column_span']=2
                elif alteration=='duplicate':raw['tables'][0]['cells'].append(deepcopy(raw['tables'][0]['cells'][0]))
                else:raw['tables'][0]['cells'].pop()
                mapping=observe_screen_grid_cells(raw,prep,evidence)
                self.assertEqual(mapping['cell_count'],0)
                report=build_screen_diagnostics(raw,prep,evidence,assessment={'issues':[self.cell_issue()]})
                self.assertEqual(report['items'][0]['location']['level'],'table')

    def test_swapped_values_reject_mapping_even_with_complete_same_dimensions(self):
        raw,prep,evidence=self.fixture()
        cells=raw['tables'][0]['cells'];cells[0]['text'],cells[-1]['text']=cells[-1]['text'],cells[0]['text']
        mapping=observe_screen_grid_cells(raw,prep,evidence)
        self.assertEqual(mapping['cell_count'],0)
        self.assertEqual(mapping['tables'][0]['reason'],'independent_text_anchor_position_conflict')
        self.assertEqual(len(mapping['tables'][0]['anchor_conflicts']),2)

    def test_weak_or_repeated_literals_cannot_prove_independent_row_column_mapping(self):
        for alteration in ('low_confidence','repeated','cross_cell_line','missing_column','candidate_derived_source'):
            with self.subTest(alteration=alteration):
                raw,prep,evidence=self.fixture()
                if alteration=='low_confidence':
                    for part in evidence['text']:part['confidence']=.5
                elif alteration=='repeated':
                    for part in evidence['text']:part['text']='100.00'
                    for part in raw['tables'][0]['cells']:part['text']='100.00'
                elif alteration=='cross_cell_line':
                    for part in evidence['text']:part['box'][2]=460
                elif alteration=='missing_column':evidence['text']=[part for part in evidence['text'] if part['box'][0]<310]
                else:
                    for part in evidence['text']:part['source']='candidate-transcription'
                self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_broken_or_missing_outer_borders_are_not_repaired_by_candidate_spans(self):
        for alteration in ('partial_interior','absent_exterior','invented_uniform_lines'):
            with self.subTest(alteration=alteration):
                raw,prep,evidence=self.fixture()
                if alteration=='invented_uniform_lines':
                    prep['business_regions'][0]['observed_grid']['vertical_lines']=[100,223,347,470]
                else:
                    with Image.open(self.path) as opened:image=opened.copy()
                    draw=ImageDraw.Draw(image)
                    if alteration=='partial_interior':draw.rectangle((182,250,189,278),fill='white')
                    else:draw.rectangle((105,195,465,205),fill='white')
                    image.save(self.path)
                    digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
                    prep['input_sha256']=raw['screen']['input_sha256']=digest
                self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_source_image_binding_and_dimensions_are_mandatory(self):
        for alteration in ('missing_hash','candidate_hash','source_bytes','dimensions','out_of_bounds'):
            with self.subTest(alteration=alteration):
                raw,prep,evidence=self.fixture()
                if alteration=='missing_hash':prep.pop('input_sha256')
                elif alteration=='candidate_hash':raw['screen']['input_sha256']='other'
                elif alteration=='source_bytes':Image.new('RGB',(600,500),'black').save(self.path)
                elif alteration=='dimensions':prep['width']=601
                else:prep['business_regions'][0]['observed_grid']['vertical_lines'][-1]=610
                self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_source_bound_localization_does_not_hide_missing_zero_sign_or_identifier(self):
        for candidate,reading in (('10','1'),('-10','10'),('00001008','0001008')):
            with self.subTest(candidate=candidate,reading=reading):
                raw,prep,evidence=self.fixture()
                raw['tables'][0]['cells'][4]['text']=candidate;evidence['text'][4]['text']=reading
                assessment=assess_screen_result(raw,prep,evidence)
                self.assertEqual(assessment['grid_localization']['cell_count'],9)
                issue=next(part for part in assessment['issues'] if part['code']=='localized_text_disagreement')
                self.assertEqual((issue['row'],issue['column']),(1,1))
                self.assertEqual(issue['severity'],'high')
                report=build_screen_diagnostics(raw,prep,evidence)
                self.assertEqual(next(part for part in report['items'] if part['code']=='localized_text_disagreement')['location']['box'],[185,242,310,305])

    def test_old_or_spoofed_receipt_cannot_localize_changed_candidate(self):
        raw,prep,evidence=self.fixture()
        assessment=assess_screen_result(raw,prep,evidence)
        assessment['issues']=[self.cell_issue()]
        cells=raw['tables'][0]['cells'];cells[0]['text'],cells[-1]['text']=cells[-1]['text'],cells[0]['text']
        report=build_screen_diagnostics(raw,prep,evidence,assessment=assessment)
        self.assertEqual(report['grid_localization']['cell_count'],0)
        self.assertEqual(report['items'][0]['location']['level'],'table')

    def test_visual_reader_receives_observed_pixels_without_candidate_values(self):
        raw,prep,evidence=self.fixture()
        snapshot=build_screen_visual_snapshot(raw,prep,[self.cell_issue()],self.path)
        self.assertEqual(len(snapshot['targets']),1)
        self.assertEqual(snapshot['targets'][0]['before'],'')
        self.assertNotIn('item-1-1',snapshot['targets'][0]['context'])
        self.assertEqual(snapshot['targets'][0]['evidence']['polygon'],[[185,242],[310,242],[310,305],[185,305]])
        self.assertEqual(snapshot['source_targets'][0]['before'],'item-1-1')
        self.assertNotIn('box',raw['tables'][0]['cells'][4])

    def test_reference_label_evidence_is_rejected(self):
        raw,prep,evidence=self.fixture();evidence['provenance']['contains_reference_input']=True
        with self.assertRaises(ValueError):observe_screen_grid_cells(raw,prep,evidence)

    def test_other_image_auxiliary_readings_cannot_anchor_current_candidate(self):
        raw,prep,evidence=self.fixture();raw['project_image_version']='current-image'
        evidence['provenance']['image_version']='other-image'
        self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)
        evidence['provenance']['image_version']='current-image'
        self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],9)


if __name__=='__main__':unittest.main()
