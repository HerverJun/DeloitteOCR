"""Tool contracts and empty-region recovery, independent of public documents."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ocr_workbench.adapter import check_recognition_result
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.geometry_providers import adapt_prediction
from ocr_workbench.page_processing import merge_page, map_region_result


ROOT=Path(__file__).resolve().parents[1]
PDF_RUNTIME=ROOT/'build/document-workflow/bundle/runtimes/pdf/python.exe'


class RegionOutcomeTests(unittest.TestCase):
    def test_empty_region_is_reviewable_but_error_and_empty_whole_image_still_fail(self):
        result={'status':'success','engine':'ppocr','text':'','blocks':[],'tables':[]}
        with self.assertRaises(RuntimeError):check_recognition_result(deepcopy(result))
        with self.assertRaises(RuntimeError):check_recognition_result(dict(result,status='failed'),allow_empty=True)
        checked=check_recognition_result(result,allow_empty=True)
        mapped=map_region_result(checked,[10,20,90,70],'v')
        native={'units':[{'text':'000123','polygon':box_polygon([1,1,9,9]),'source':'pdf-native','native_index':0}]}
        page=merge_page(native,[mapped],{'id':'v','sha256':'h','width':100,'height':100},
                        {'id':'d','sha256':'pdf'},{'id':'p','page_number':1},'auto')
        self.assertEqual(page['text'],'000123')
        issue=page['document']['conflicts'][0]
        self.assertEqual(issue['reason'],'region_no_text')
        self.assertEqual(issue['ocr']['polygon'],box_polygon([10,20,90,70]))
        self.assertEqual(issue['state'],'pending')

    def test_successful_text_does_not_acquire_an_empty_region_warning(self):
        value={'status':'success','text':'001','blocks':[]}
        self.assertEqual(check_recognition_result(deepcopy(value),allow_empty=True),value)


class PdfToolAdapterTests(unittest.TestCase):
    def test_native_table_conversion_preserves_interleaved_ocr_and_conflict(self):
        from ocr_workbench.native_tables import native_table_preview
        from test_structure_workflow import prediction
        pred,_=prediction([['001','002'],['003','004']])
        blocks=[];text=''
        for index,value in enumerate(['001','OCR disagreement','002','003','004']):
            cell_index=index if index<1 else index-1
            r,c=divmod(cell_index,2)
            start=len(text);text+=value+'\n'
            blocks.append({'text':value,'text_range':[start,len(text)-1],
                'polygon':box_polygon([c*100+5,r*40+5,c*100+90,r*40+30]),
                'source':'ppocr' if index==1 else 'pdf-native'})
        raw={'text':text,'tables':[],'blocks':blocks,'document':{'conflicts':[{'id':'pending'}]},'engine_info':{}}
        preview=native_table_preview(raw,pred,200,80)
        self.assertIsNotNone(preview)
        self.assertIn('OCR disagreement',preview['text'])
        self.assertEqual(preview['document']['conflicts'],[{'id':'pending'}])
        self.assertEqual([c['text'] for c in preview['tables'][0]['cells']],['001','002','003','004'])
        preserved=preview['blocks'][0];a,b=preserved['text_range']
        self.assertEqual(preview['text'][a:b],'OCR disagreement')
        self.assertEqual(raw['text'],text)

    def test_spans_coordinates_and_upstream_identity_are_not_reestimated(self):
        cell={'id':'17','row':2,'column':1,'row_span':3,'column_span':2,
              'original_box':[13,27,59,88],'polygon':box_polygon([6,14,98,136]),'text':''}
        prediction={'component':'pdfplumber','tool_version':'locked','settings_sha256':'settings',
            'pdfplumber_tables':[{'id':'table-4','cells':[cell],'rows':5,'columns':3,
                'polygon':box_polygon([0,0,180,180]),'transform':[2,0,-20,0,2,-40,0,0,1]}]}
        candidate=adapt_prediction(prediction,200,200)[0]['cells'][0]
        self.assertEqual((candidate.row_start,candidate.row_end,candidate.column_start,candidate.column_end),(2,5,1,3))
        self.assertEqual(candidate.original_cell_id,'17')
        self.assertEqual(candidate.original_polygon,box_polygon(cell['original_box']))
        self.assertEqual(candidate.cell_polygon,cell['polygon'])
        self.assertFalse(candidate.index_mapping['compression_applied'])
        from ocr_workbench.geometry_contract import evidence_v2
        evidence=evidence_v2(cell_polygons=[candidate.cell_polygon],display_polygon=candidate.cell_polygon,
                             origin=candidate.geometry_origin)
        self.assertEqual(evidence['range_semantics'],'full_cell')

    @unittest.skipUnless(PDF_RUNTIME.is_file(),'Isolated PDF runtime required')
    def test_tool_on_generated_grids_across_rotation_crop_and_dpi(self):
        script=r'''
import sys,json,random
from pathlib import Path
from fpdf import FPDF
import pikepdf
sys.path.insert(0,sys.argv[1])
from ocr_workbench.pdf_tables import extract_tables
from ocr_workbench.pdf_worker import page_metadata
from ocr_workbench.coordinates import bounds
root=Path(sys.argv[2]);rng=random.Random(814725)
checks=0
for variant in range(3):
    # Unequal widths/heights, top colspan and a lower rowspan; literal numbers
    # are independent of the geometry and cannot drive table detection.
    xs=[45.];ys=[55.]
    for _ in range(3):xs.append(xs[-1]+rng.uniform(45,70))
    for _ in range(4):ys.append(ys[-1]+rng.uniform(30,45))
    pdf=FPDF(unit='pt',format=(360,320));pdf.add_page();pdf.set_font('Helvetica',size=11)
    spans=[(0,0,1,2),(0,2,1,1),(1,0,2,1),(1,1,1,1),(1,2,1,1),(2,1,1,1),(2,2,1,1),(3,0,1,1),(3,1,1,1),(3,2,1,1)]
    for index,(r,c,rs,cs) in enumerate(spans):
        pdf.rect(xs[c],ys[r],xs[c+cs]-xs[c],ys[r+rs]-ys[r]);pdf.text(xs[c]+5,ys[r]+15,f'{index:05}')
    source=root/f'grid-{variant}.pdf';pdf.output(source)
    for rotation in (0,90,180,270):
        target=root/f'grid-{variant}-{rotation}.pdf'
        with pikepdf.open(source) as p:
            p.pages[0].obj['/CropBox']=pikepdf.Array([20,25,340,300]);p.pages[0].obj['/Rotate']=rotation;p.save(target)
        for dpi in (72,150,300):
            with pikepdf.open(target) as p:metadata=page_metadata(p.pages[0],1,dpi)
            value=extract_tables(target,1,metadata)
            assert len(value['pdfplumber_tables'])==1,(variant,rotation,dpi,value)
            table=value['pdfplumber_tables'][0]
            assert len(table['cells'])==10,(variant,rotation,dpi)
            assert sum(c['row_span']*c['column_span'] for c in table['cells'])==12
            assert sum(max(c['row_span'],c['column_span'])>1 for c in table['cells'])==2
            for cell in table['cells']:
                actual=bounds(cell['polygon']);box=cell['original_box'];m=table['transform']
                expected=[box[0]*m[0]+m[2],box[1]*m[4]+m[5],box[2]*m[0]+m[2],box[3]*m[4]+m[5]]
                assert max(abs(a-b) for a,b in zip(actual,expected))<1e-6
            checks+=1
print(json.dumps({'generated_pdf_transform_cases':checks,'tool_settings':'unmodified defaults'}))
'''
        with tempfile.TemporaryDirectory() as temporary:
            done=subprocess.run([str(PDF_RUNTIME),'-X','utf8','-c',script,str(ROOT/'src'),temporary],
                capture_output=True,text=True,encoding='utf-8',timeout=120)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertEqual(json.loads(done.stdout)['generated_pdf_transform_cases'],36)


if __name__=='__main__':unittest.main()
