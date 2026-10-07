"""Offline replay rejects stale evidence/crops and preserves OCR model values."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from PIL import Image


TEMP_ROOT=Path('F:/OCR/output/ocr-screen-benchmark-20261007/temp')
TOOLS=Path('F:/OCR/scripts/screen_benchmark')


class ScreenQualityReportTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='quality-replay-',dir=TEMP_ROOT)
        self.root=Path(self.temp.name)
        self.driver=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/screen_quality_report.py'))
        self.runtime=self.driver['production_runtime'](Path(__file__).resolve().parents[1],TOOLS)
        self.digest=self.driver['digest']
        self.image=self.root/'source.png'
        Image.new('RGB',(200,100),'white').save(self.image)
        self.sample={'id':'sample','image':str(self.image),'image_sha256':self.digest(self.image),'width':200,'height':100}
        self.context_root=self.root/'context'
        context_folder=self.context_root/'sample'
        context_folder.mkdir(parents=True)
        self.context={'status':'success','image_version':self.sample['image_sha256'],
            'image':{'width':200,'height':100},'coordinate_space':'exif_normalized_original',
            'component':'actual-mock-context','ocr_source':'independent-ocr',
            'layout_blocks':[{'label':'table','box':[0,0,200,100],'confidence':.99}],
            'ocr_blocks':[{'text':'AB1000','polygon':[[5,32],[85,32],[85,54],[5,54]],'confidence':.99}]}
        self.write(context_folder/'context.json',self.context)
        self.write(context_folder/'evidence.json',self.runtime['normalize'](self.context))
        context_record={'id':'sample','status':'success','image_sha256':self.sample['image_sha256'],
            'context_sha256':self.digest(context_folder/'context.json'),
            'evidence_sha256':self.digest(context_folder/'evidence.json')}
        self.write(context_folder/'record.json',context_record)
        self.write(self.context_root/'binding.json',{'labels_or_ideal_regions_provided':False})
        self.binding={'context_run':str(self.context_root),'context_binding_sha256':self.digest(self.context_root/'binding.json')}
        self.runroot=self.root/'run'
        self.attempt=self.runroot/'sample/attempt-0001'
        self.attempt.mkdir(parents=True)
        self.prepared_image={'id':'original','path':str(self.image),'kind':'original','width':200,'height':100,
            'prepared_to_source':[1,0,0,0,1,0,0,0,1]}
        self.preparation={'input_sha256':self.sample['image_sha256'],'width':200,'height':100,
            'policy_version':self.runtime['preparation_policy'],'prepared_images':[self.prepared_image],
            'quality':{'flags':[],'unreadable':False},'business_regions':[],'excluded_regions':[]}
        self.write(self.attempt/'preprocess.json',self.preparation)
        self.record={'id':'sample','status':'success','output':str(self.attempt),
            'context_sha256':context_record['context_sha256'],'context_evidence_sha256':context_record['evidence_sha256'],
            'prepared_images':[{**self.prepared_image,'sha256':self.digest(self.image)}],
            'result_sha256':'test-result-hash','quality':{'automatic_delivery':True,'original_marker':'retain'}}
        self.raw={'text':'AB100','tables':[{'rows':1,'columns':1,'cells':[
            {'row':0,'column':0,'row_span':1,'column_span':1,'text':'AB100','box':[0,30,100,60],
             'confidence':.1,'geometry_origin':'direct_detection'}]}],'quality':{'original_marker':'model-retain'}}

    def tearDown(self):
        self.temp.cleanup()

    def write(self,path,value):
        path.write_text(json.dumps(value,ensure_ascii=False),'utf-8')

    def replay(self):
        return self.driver['replay_production_quality'](self.record,self.raw,self.sample,
            self.binding,self.runroot,self.runtime)

    def test_quality_replay_keeps_both_original_quality_and_numeric_model_values(self):
        before=deepcopy(self.raw)
        recorded=deepcopy(self.record)
        output=self.replay()
        self.assertEqual(self.raw,before)
        self.assertEqual(self.record,recorded)
        self.assertEqual(output['original_record_quality'],recorded['quality'])
        self.assertEqual(output['original_result_quality'],before['quality'])
        self.assertIs(output['model_values_unchanged'],True)
        self.assertEqual(output['preparation_manifest_sha256'],self.digest(self.attempt/'preprocess.json'))
        self.assertEqual(output['context_sha256'],self.record['context_sha256'])
        self.assertNotIn('original_marker',output['effective_quality'])

    def test_replay_rejects_changed_consumed_crop_transform_and_roi_v1(self):
        self.record['prepared_images'][0]['prepared_to_source'][2]=10
        with self.assertRaisesRegex(ValueError,'crop metadata'):
            self.replay()
        self.record['prepared_images'][0]['prepared_to_source'][2]=0
        self.preparation['policy_version']='screen-imaging-v1'
        self.write(self.attempt/'preprocess.json',self.preparation)
        with self.assertRaisesRegex(ValueError,'ROI v2'):
            self.replay()

    def test_replay_rejects_altered_frozen_actual_context(self):
        self.context['ocr_blocks'][0]['text']='changed after inference'
        self.write(self.context_root/'sample/context.json',self.context)
        with self.assertRaisesRegex(ValueError,'Saved context changed'):
            self.replay()


if __name__=='__main__':
    unittest.main()
