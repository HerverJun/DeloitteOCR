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

    def recovery_fixture(self):
        binding={**self.binding,'inputs_sha256':'frozen-inputs','worker_source_sha256':'same-worker',
            'engine':'glm','engine_config_sha256':'same-config','preprocess_module_sha256':'same-preparation',
            'request_timeout_seconds':120,'first_pass':True,'labels_or_ideal_regions_provided':False}
        failed={**self.record,'image_sha256':self.sample['image_sha256'],'status':'failed','engine':'glm','error_type':'FileNotFoundError',
            'error':f"[Errno 2] No such file or directory: {str(self.image)!r}"}
        self.write(self.runroot/'sample/record.json',failed)
        recovery_root=self.root/'recovery'
        replacement_attempt=recovery_root/'sample/attempt-0001'
        replacement_attempt.mkdir(parents=True)
        self.write(recovery_root/'binding.json',{**binding,'include_sample_ids':['sample']})
        self.write(replacement_attempt/'preprocess.json',self.preparation)
        result_path=recovery_root/'sample/result.json'
        self.write(result_path,self.raw)
        replacement={**self.record,'image_sha256':self.sample['image_sha256'],'engine':'glm','output':str(replacement_attempt),
            'result_sha256':self.digest(result_path),'inference_seconds':3,'wall_seconds':5}
        self.write(recovery_root/'sample/record.json',replacement)
        self.write(self.runroot/'run-summary.json',{'completed':1,'total':1,'attempt_wall_seconds':11})
        self.write(recovery_root/'run-summary.json',{'completed':1,'total':1,'attempt_wall_seconds':7})
        truth={**self.sample,'tables':deepcopy(self.raw['tables']),'cohort':'chinese','level':'normal'}
        score=self.driver['load_module'](TOOLS/'score.py')
        kwargs={'runroot':self.runroot,'binding':binding,'samples':{'sample':self.sample},'truths':[truth],
            'original_records':[failed],'original_results':{'sample':{}},'score':score,'runtime':self.runtime}
        return {'run':str(recovery_root),'sample_ids':['sample']},kwargs,recovery_root,replacement

    def test_input_failure_recovery_preserves_original_receipt_and_separates_wall_time(self):
        spec,kwargs,recovery_root,_=self.recovery_fixture()
        source_hash=self.digest(self.runroot/'sample/record.json')
        recovery_hash=self.digest(recovery_root/'sample/record.json')
        output=self.driver['recovered_content'](spec,**kwargs)
        self.assertEqual(output['restored_successes'],1)
        self.assertEqual(output['source_input_failures_remaining'],[])
        self.assertIs(output['content_measurement_complete'],True)
        self.assertEqual(output['quality']['ratios']['whole_image_correct_rate'],1)
        self.assertEqual(output['quality']['totals']['images'],1)
        self.assertEqual(output['timing']['first_pass']['recorded_attempt_wall_seconds'],11)
        self.assertEqual(output['timing']['independent_recovery']['recorded_attempt_wall_seconds'],7)
        self.assertEqual(self.digest(self.runroot/'sample/record.json'),source_hash)
        self.assertEqual(self.digest(recovery_root/'sample/record.json'),recovery_hash)
        self.assertEqual(kwargs['original_records'][0]['status'],'failed')

    def test_recovery_rejects_original_success_real_model_failure_and_changed_binding(self):
        spec,kwargs,recovery_root,_=self.recovery_fixture()
        for status,error_type in (('success','FileNotFoundError'),('failed','RuntimeError')):
            with self.subTest(status=status,error_type=error_type):
                kwargs['original_records'][0].update(status=status,error_type=error_type)
                with self.assertRaisesRegex(ValueError,'Only an ended FileNotFoundError'):
                    self.driver['recovered_content'](spec,**kwargs)
        kwargs['original_records'][0].update(status='failed',error_type='FileNotFoundError')
        binding=json.loads((recovery_root/'binding.json').read_text('utf-8'))
        binding['request_timeout_seconds']=180
        self.write(recovery_root/'binding.json',binding)
        with self.assertRaisesRegex(ValueError,'binding differs'):
            self.driver['recovered_content'](spec,**kwargs)

    def test_unsuccessful_recovery_does_not_replace_the_original_input_failure(self):
        spec,kwargs,recovery_root,replacement=self.recovery_fixture()
        replacement.update(status='failed',error_type='RuntimeError',error='actual model recovery failure')
        self.write(recovery_root/'sample/record.json',replacement)
        output=self.driver['recovered_content'](spec,**kwargs)
        self.assertEqual(output['restored_successes'],0)
        self.assertEqual(output['source_input_failures_remaining'],['sample'])
        self.assertIs(output['content_measurement_complete'],False)
        self.assertEqual(output['quality']['ratios']['whole_image_correct_rate'],0)
        self.assertEqual(output['receipts'][0]['recovery_status'],'failed')
        self.assertFalse(output['receipts'][0]['substituted'])


if __name__=='__main__':
    unittest.main()
