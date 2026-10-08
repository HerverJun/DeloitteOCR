"""CPU protocol/prompt gates; fake tensors never constitute model quality evidence."""
from __future__ import annotations
import argparse
import ast
import base64
from contextlib import nullcontext
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from common import digest,fpath,frozen_json
from local_text_probe import variant_source

SOURCE=fpath('F:/OCR/output/platform-specialized-agent-20261007/development-variants/local-text-dev-001/ocr')
BASE=fpath('F:/OCR/output/platform-specialized-agent-20261007/source-snapshots/glm-native-dev-001/ocr')
BUNDLE=fpath('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr')
OUTPUT=None


class InputBatch(dict):
    def to(self,device):
        self.device=device
        return self


def fake_session(text,*,token_count=2,max_tokens=8):
    from PIL import Image
    encoded=io.BytesIO()
    Image.new('RGB',(40,20),'white').save(encoded,format='JPEG')
    payload={'messages':[{'role':'user','content':[{'type':'image_url','image_url':{
        'url':'data:image/jpeg;base64,'+base64.b64encode(encoded.getvalue()).decode()}},
        {'type':'text','text':'Official text prompt fixture; never source labels'}]}],
        'max_tokens':max_tokens,'repetition_penalty':1.1}
    captures={}
    class Loader:
        def build_request_from_image(self,image,task_type):
            captures['loader_image_size']=image.size
            captures['task_type']=task_type
            return deepcopy(payload)
    class Processor:
        def apply_chat_template(self,messages,**kwargs):
            captures['messages']=messages
            captures['template_kwargs']=kwargs
            return InputBatch({'input_ids':SimpleNamespace(shape=(1,3))})
        def decode(self,ids,**kwargs):
            captures['decoded_ids']=ids
            captures['decode_kwargs']=kwargs
            return text
    class Model:
        device='fake-device-no-GPU'
        def generate(self,**kwargs):
            captures['generation_kwargs']=kwargs
            return [[101,102,103,*range(token_count)]]
    detector=SimpleNamespace(process=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Local text called page layout')))
    formatter=SimpleNamespace(process=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Local text created a formatted table')))
    return (detector,Model(),Processor(),Loader(),formatter,7.0,SimpleNamespace()),captures,encoded.getvalue()


class LocalTextContracts(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.directory=OUTPUT/self._testMethodName
        self.directory.mkdir(parents=True,exist_ok=False)
        self.image=self.directory/'fixture-only.png'
        Image.new('RGB',(180,90),'white').save(self.image)

    def test_explicit_mode_contract_rejects_unknown_and_non_glm_engines(self):
        with variant_source(SOURCE):
            validate=importlib.import_module('ocr_workbench.recognition_modes').validate_recognition_mode
            for engine in ('glm','ppocr','paddlevl','hunyuan','geometry','dewarp'):
                self.assertEqual(validate(engine),'page')
                if engine!='glm':
                    with self.subTest(engine=engine),self.assertRaises(ValueError):
                        validate(engine,'local_text')
            self.assertEqual(validate('glm','local_text'),'local_text')
            for mode in (None,True,0,'text','geometry','local-text',{},[]):
                with self.subTest(mode=mode),self.assertRaises(ValueError):
                    validate('glm',mode)

    def test_direct_decoder_uses_official_text_payload_and_preserves_raw_no_layout_formatter(self):
        session,capture,jpeg=fake_session('OBFC1')
        fake_torch=SimpleNamespace(inference_mode=nullcontext)
        with variant_source(SOURCE),patch.dict('sys.modules',{'torch':fake_torch}):
            module=importlib.import_module('ocr_workbench.glm')
            raw,blocks,loaded=module.recognize_local_text(BUNDLE/'models',self.image,session)
        self.assertEqual(capture['task_type'],'text')
        self.assertEqual(capture['loader_image_size'],(180,90))
        self.assertEqual(capture['messages'][0]['content'][0]['type'],'image')
        self.assertEqual(capture['messages'][0]['content'][1]['text'],raw['request']['prompt'][0]['content'][0]['text'])
        self.assertEqual(capture['generation_kwargs']['max_new_tokens'],8)
        self.assertFalse(capture['generation_kwargs']['do_sample'])
        self.assertEqual(capture['generation_kwargs']['repetition_penalty'],1.1)
        self.assertEqual(raw['input_image_sha256'],digest(self.image))
        self.assertEqual(raw['request']['image_bindings'][0]['encoded_image_sha256'],__import__('hashlib').sha256(jpeg).hexdigest())
        self.assertEqual(raw['generated'][0]['content'],'OBFC1')
        self.assertEqual(raw['generated'][0]['token_count'],2)
        self.assertEqual(blocks[0]['polygon'],[[0,0],[180,0],[180,90],[0,90]])
        self.assertIsNone(blocks[0]['confidence'])
        self.assertEqual(loaded,7.0)
        self.assertFalse(raw['layout_applied'])
        self.assertFalse(raw['formatter_applied'])
        self.assertNotIn('tables',raw)

    def test_empty_decoder_is_preserved_and_acceptance_is_explicit(self):
        session,capture,jpeg=fake_session('')
        with variant_source(SOURCE),patch.dict('sys.modules',{'torch':SimpleNamespace(inference_mode=nullcontext)}):
            module=importlib.import_module('ocr_workbench.glm')
            raw,blocks,loaded=module.recognize_local_text(BUNDLE/'models',self.image,session)
            adapter=importlib.import_module('ocr_workbench.adapter')
            result={'status':'success','text':blocks[0]['text'],'raw':raw}
            with self.assertRaises(RuntimeError):
                adapter.check_recognition_result(deepcopy(result))
            answer=adapter.check_recognition_result(result,allow_empty=True)
        self.assertEqual(raw['generated'][0]['content'],'')
        self.assertEqual(raw['generated'][0]['token_count'],2)
        self.assertEqual(answer['content_status'],'no_text_detected')
        self.assertFalse(raw['truncated'])

    def test_token_limit_rejects_success_and_preserves_generated_failure_evidence(self):
        session,capture,jpeg=fake_session('unfinished raw output',token_count=8,max_tokens=8)
        with variant_source(SOURCE),patch.dict('sys.modules',{'torch':SimpleNamespace(inference_mode=nullcontext)}):
            module=importlib.import_module('ocr_workbench.glm')
            with self.assertRaises(module.LocalTextTruncatedOutput) as found:
                module.recognize_local_text(BUNDLE/'models',self.image,session)
        evidence=found.exception.recognition_evidence
        self.assertTrue(evidence['truncated'])
        self.assertEqual(evidence['generated'][0]['content'],'unfinished raw output')
        self.assertEqual(evidence['generated'][0]['token_count'],8)
        self.assertEqual(evidence['input_image_sha256'],digest(self.image))
        self.assertEqual(evidence['request']['task_type'],'text')

    def test_default_page_glm_function_is_identical_to_existing_function(self):
        def function(path,name):
            parsed=ast.parse(path.read_text('utf-8'))
            return ast.dump(next(node for node in parsed.body if isinstance(node,ast.FunctionDef) and node.name==name),include_attributes=False)
        for name in ('recognize','load_session'):
            self.assertEqual(function(BASE/'src/ocr_workbench/glm.py',name),function(SOURCE/'src/ocr_workbench/glm.py',name))

    def test_adapter_page_wire_is_unchanged_and_local_text_is_explicit(self):
        with variant_source(SOURCE):
            module=importlib.import_module('ocr_workbench.adapter')
            value=module.EngineAdapter.__new__(module.EngineAdapter)
            value.engine='glm'
            value.ipc=self.directory/'ipc'
            value.ipc.mkdir()
            value.load_seconds=1.0
            value.process=SimpleNamespace(pid=99)
            value.wait_for=lambda path,timeout:{'id':'fixture-request','status':'success'}
            value.recognition_timeout=10
            requests=[]
            fixture={'status':'success','text':'OBFC1'}
            for name in ('page','local'):
                frozen_json(self.directory/name/'result.json',fixture)
            with (patch.object(module.uuid,'uuid4',return_value=SimpleNamespace(hex='fixture-request')),
                    patch.object(module,'publish',side_effect=lambda path,request:requests.append(request)),
                    patch.object(module,'read_json',return_value=deepcopy(fixture))):
                value.recognize(self.image,self.directory/'page')
                value.recognize_local_text(self.image,self.directory/'local')
                self.assertNotIn('recognition_mode',requests[0])
                self.assertEqual(set(requests[0]),{'id','image','output','geometry_request'})
                self.assertEqual(requests[1]['recognition_mode'],'local_text')
                self.assertIsNone(requests[1]['geometry_request'])
                for engine in ('hunyuan','ppocr','paddlevl','geometry','dewarp'):
                    value.engine=engine
                    with self.assertRaises(ValueError):
                        value.recognize_local_text(self.image,self.directory/'rejected')
                value.engine='glm'
                with self.assertRaises(ValueError):
                    value.recognize(self.image,self.directory/'rejected',{'provider':'context'},recognition_mode='local_text')
                self.assertEqual(len(requests),2)

    def test_resident_page_dispatch_preserves_original_call_and_local_mode_is_separate(self):
        with variant_source(SOURCE):
            host=importlib.import_module('ocr_workbench.engine_host')
            glm=importlib.import_module('ocr_workbench.glm')
            value=host.Resident.__new__(host.Resident)
            value.engine='glm';value.bundle=BUNDLE;value.session='fixture-session';value.loaded=7.;value.first=True
            with (patch.object(glm,'recognize',return_value=({'mode':'page'},[],7.)) as page,
                    patch.object(glm,'recognize_local_text',return_value=({'mode':'local_text'},[],7.)) as local):
                first=value.recognize(self.image,self.directory/'page')
                second=value.recognize(self.image,self.directory/'local',recognition_mode='local_text')
                page.assert_called_once_with(BUNDLE/'models',self.image,'fixture-session')
                local.assert_called_once_with(BUNDLE/'models',self.image,'fixture-session')
                self.assertEqual(first[2],7.)
                self.assertEqual(second[2],0)
                value.engine='hunyuan'
                with self.assertRaises(ValueError):
                    value.recognize(self.image,self.directory/'invalid',recognition_mode='local_text')

    def test_worker_text_keeps_full_raw_but_creates_no_table_or_xlsx(self):
        with variant_source(SOURCE):
            worker=importlib.import_module('ocr_workbench.worker')
            audit=importlib.import_module('ocr_workbench.runtime_audit')
            calls=[]
            class Session:
                def recognize(self,*args,**kwargs):
                    calls.append((args,kwargs))
                    return ({'recognition_mode':kwargs.get('recognition_mode','page')},
                        [{'kind':'text','text':'|ID|Amount|\n|---|---|\n|000123|10.00|','confidence':None}],7.)
            with (patch.dict('sys.modules',{'pillow_heif':SimpleNamespace(register_heif_opener=lambda:None)}),
                    patch.object(worker,'configure'),patch.object(audit,'inspect_runtime',return_value={'fixture_only':True})):
                for mode in ('page','local_text'):
                    output=self.directory/mode
                    args=SimpleNamespace(bundle=BUNDLE,engine='glm',image=self.image,output=output,recognition_mode=mode)
                    worker.run_image(args,Session())
                    result=json.loads((output/'result.json').read_text('utf-8'))
                    if mode=='page':
                        self.assertTrue(result['tables'])
                        self.assertEqual(calls[-1][1],{})
                        self.assertNotIn('recognition_mode',result)
                    else:
                        self.assertEqual(result['tables'],[])
                        self.assertFalse((output/'result.xlsx').exists())
                        self.assertEqual(result['recognition_mode'],'local_text')
                        self.assertEqual(calls[-1][1],{'recognition_mode':'local_text'})
                        self.assertIn('000123',result['text'])

    def test_worker_failure_retains_truncation_raw_and_unknown_engine_mode_does_not_run(self):
        with variant_source(SOURCE):
            worker=importlib.import_module('ocr_workbench.worker')
            glm=importlib.import_module('ocr_workbench.glm')
            evidence={'generated':[{'content':'unfinished','token_count':8}],'truncated':True,
                'request':{'task_type':'text'},'input_image_sha256':digest(self.image)}
            args=SimpleNamespace(bundle=BUNDLE,engine='glm',image=self.image,output=self.directory/'failed',recognition_mode='local_text')
            with patch.object(worker,'configure'),patch.object(worker,'process_image',side_effect=glm.LocalTextTruncatedOutput(evidence)):
                with self.assertRaises(glm.LocalTextTruncatedOutput):
                    worker.run_image(args)
            receipt=json.loads((args.output/'error.json').read_text('utf-8'))
            self.assertEqual(receipt['recognition_evidence'],evidence)
            self.assertEqual(receipt['status'],'failed')
            args.engine='ppocr';args.output=self.directory/'unsupported'
            with patch.object(worker,'process_image') as process:
                with self.assertRaises(ValueError):
                    worker.run_image(args)
                process.assert_not_called()
                self.assertFalse(args.output.exists())


def main():
    global OUTPUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-root',required=True)
    parser.add_argument('--receipt',required=True)
    args=parser.parse_args()
    OUTPUT=fpath(args.output_root)
    OUTPUT.mkdir(parents=True,exist_ok=False)
    paths=[Path(__file__).resolve(),Path(__file__).with_name('local_text_probe.py'),
        *[SOURCE/f'src/ocr_workbench/{name}.py' for name in ('recognition_modes','glm','adapter','engine_host','worker')]]
    before={str(path):digest(path) for path in paths}
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LocalTextContracts))
    after={str(path):digest(path) for path in paths}
    receipt=fpath(args.receipt)
    receipt.with_suffix('.log').write_text(stream.getvalue(),'utf-8')
    success=result.wasSuccessful() and before==after
    value={'format':'explicit-local-text-mode-cpu-contract-v1','passed':success,'tests':result.testsRun,
        'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'source_before':before,'source_after':after,'source_unchanged':before==after,
        'synthetic_tensors_only':True,'gpu_or_model_executed':False,'production_modified':False,
        'source_variant':str(SOURCE),'output_root':str(OUTPUT),'log':str(receipt.with_suffix('.log'))}
    frozen_json(receipt,value)
    suite=ET.Element('testsuite',name='local_text_mode_cpu',tests=str(result.testsRun),failures=str(len(result.failures)),errors=str(len(result.errors)))
    for test,message in result.failures+result.errors:
        case=ET.SubElement(suite,'testcase',name=str(test))
        ET.SubElement(case,'failure').text=message
    ET.ElementTree(suite).write(receipt.with_suffix('.xml'),encoding='utf-8',xml_declaration=True)
    print(stream.getvalue())
    print(json.dumps(value,ensure_ascii=False))
    raise SystemExit(0 if success else 1)


if __name__=='__main__':
    main()
