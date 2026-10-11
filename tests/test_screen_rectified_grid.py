"""Perspective fallback must prove source geometry, never logical indices."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from PIL import Image, ImageDraw

from ocr_workbench.coordinates import bounds, box_polygon, inverse, polygon
from ocr_workbench.screen_diagnostics import observe_screen_grid_cells, build_screen_diagnostics
from ocr_workbench.screen_evidence import build_screen_visual_snapshot


class ScreenRectifiedGridTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'original.png'

    def fixture(self, *, merged=False):
        image=Image.new('RGB',(700,550),'white')
        draw=ImageDraw.Draw(image)
        xs=[100,220,390,600];ys=[120,200,310,440]
        for x in xs:draw.line((x,ys[0],x,ys[-1]),fill='black',width=3)
        for y in ys:draw.line((xs[0],y,xs[-1],y),fill='black',width=3)
        cells=[{'row':r,'column':c,'text':f'item-{r}-{c}','row_span':1,'column_span':1}
            for r in range(3) for c in range(3)]
        if merged:
            draw.rectangle((xs[1]-3,ys[0]+4,xs[1]+4,ys[1]-4),fill='white')
            cells[0]['column_span']=2;cells.pop(1)
        # Tilt every boundary; their unequal spacing must survive the round trip.
        to_source=[1.,.09,32.,.025,1.,22.,.00004,.00003,1.]
        source=Image.fromarray(cv2.warpPerspective(np.asarray(image),np.asarray(to_source).reshape(3,3),
            (850,650),flags=cv2.INTER_CUBIC,borderValue=(255,255,255)))
        source.save(self.path)
        sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        readings=[]
        for cell in cells:
            r,c=cell['row'],cell['column']
            poly=polygon(to_source,box_polygon([xs[c]+25,ys[r]+25,xs[c]+80,ys[r]+45]))
            readings.append({'text':cell['text'],'box':bounds(poly),'polygon':poly,
                'confidence':.999,'source':'PP-OCRv6-auxiliary','kind':'text'})
        evidence={'text':readings,'provenance':{'image_version':'v1','contains_reference_input':False}}
        prep={'input_path':str(self.path),'input_sha256':sha,'width':850,'height':650,
            'business_regions':[{'id':'one','table_box':bounds(polygon(to_source,box_polygon([100,120,600,440])))}],
            'prepared_images':[{'id':'perspective','kind':'perspective','width':700,'height':550,
                'path':str(self.path.parent/'nonexistent-prepared.png'),
                'source_to_prepared':inverse(to_source),'prepared_to_source':to_source,
                'source_polygon':polygon(to_source,box_polygon([0,0,700,550]))}]}
        raw={'tables':[{'rows':3,'columns':3,'cells':cells}],'project_image_version':'v1',
            'screen':{'input_sha256':sha,'prepared':deepcopy(prep),
                'business_metadata_evidence':{'auxiliary_evidence':deepcopy(evidence)}}}
        return raw,prep,evidence,to_source,xs,ys

    def test_source_recomputation_recovers_nonuniform_cells_and_true_source_polygons(self):
        raw,prep,evidence,transform,xs,ys=self.fixture()
        before=deepcopy((raw,prep,evidence))
        result=observe_screen_grid_cells(raw,prep,evidence)
        self.assertEqual(result['cell_count'],9,result)
        row=result['tables'][0];cell=row['cells'][4]
        expected=polygon(transform,box_polygon([xs[1],ys[1],xs[2],ys[2]]))
        np.testing.assert_allclose(cell['polygon'],expected,atol=1.)
        self.assertNotEqual(cell['polygon'],box_polygon(cell['box']))
        self.assertFalse(row['rectification']['prepared_file_read'])
        self.assertTrue(row['rectification']['pixel_grid_redetected'])
        self.assertEqual(result['binding']['input_sha256'],prep['input_sha256'])
        self.assertEqual((raw,prep,evidence),before)
        issue={'code':'numeric_conflict','severity':'high','table':0,'row':1,'column':1}
        diagnostics=build_screen_diagnostics(raw,prep,evidence,assessment={'issues':[issue]})
        self.assertEqual(diagnostics['items'][0]['location']['polygon'],cell['polygon'])
        visual=build_screen_visual_snapshot(raw,prep,[issue],self.path)
        self.assertEqual(visual['targets'][0]['evidence']['polygon'],cell['polygon'])
        self.assertEqual(visual['targets'][0]['before'],'')

    def test_missing_interior_rule_is_observed_as_a_merge(self):
        raw,prep,evidence,*_=self.fixture(merged=True)
        result=observe_screen_grid_cells(raw,prep,evidence)
        self.assertEqual(result['cell_count'],8,result)
        self.assertEqual(result['tables'][0]['cells'][0]['column_span'],2)
        raw['tables'][0]['cells'][0]['column_span']=1
        self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_changed_source_version_anchors_or_transform_never_reuse_old_proof(self):
        for alteration in ('source','version','swapped','weak','inverse','nan','singular',
                'polygon','outside','huge','duplicate_transform','duplicate_region','two_tables'):
            with self.subTest(alteration=alteration):
                raw,prep,evidence,*_=self.fixture()
                candidate=prep['prepared_images'][0]
                if alteration=='source':Image.new('RGB',(850,650),'white').save(self.path)
                elif alteration=='version':evidence['provenance']['image_version']='v2'
                elif alteration=='swapped':
                    cells=raw['tables'][0]['cells'];cells[0]['text'],cells[-1]['text']=cells[-1]['text'],cells[0]['text']
                elif alteration=='weak':
                    for item in evidence['text']:item['confidence']=.5
                elif alteration=='inverse':candidate['prepared_to_source'][2]+=50
                elif alteration=='nan':candidate['source_to_prepared'][0]=float('nan')
                elif alteration=='singular':candidate['source_to_prepared']=[0.]*9
                elif alteration=='polygon':candidate['source_polygon'][0][0]+=1
                elif alteration=='outside':
                    candidate['prepared_to_source']=[1.,0.,-100.,0.,1.,0.,0.,0.,1.]
                    candidate['source_to_prepared']=inverse(candidate['prepared_to_source'])
                elif alteration=='huge':candidate['width']=24000001
                elif alteration=='duplicate_transform':prep['prepared_images'].append(deepcopy(candidate))
                elif alteration=='duplicate_region':prep['business_regions'].append(deepcopy(prep['business_regions'][0]))
                else:raw['tables'].append(deepcopy(raw['tables'][0]))
                self.assertEqual(observe_screen_grid_cells(raw,prep,evidence)['cell_count'],0)

    def test_reference_evidence_is_rejected_before_rectification(self):
        raw,prep,evidence,*_=self.fixture()
        evidence['provenance']['contains_reference_input']=True
        with self.assertRaises(ValueError):observe_screen_grid_cells(raw,prep,evidence)


if __name__=='__main__':unittest.main()
