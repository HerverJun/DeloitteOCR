"""Actual final-item delivery state must reflect metadata conflicts."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import shutil
import unittest
import zipfile

from openpyxl import load_workbook
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import encoded
import test_screen_batches as batch_fixture

ROOT=Path('F:/OCR/output/platform-specialized-agent-20261007')
OBS=ROOT/'runs/agent-native-h-development-recovery-001/execution-002/actual-002/observations.json'

class MetadataDeliveryTests(unittest.TestCase):
    setUp=batch_fixture.ScreenBatchTests.setUp
    tearDown=batch_fixture.ScreenBatchTests.tearDown

    def execute(self, auxiliary, primary):
        if not OBS.is_file():
            self.skipTest('Requires the archived F-drive development OCR observations')
        base=json.loads(OBS.read_text('utf-8'))['records'][36]['candidate']['original']
        original=next(i for i in base['screen']['prepared']['prepared_images'] if i['kind']=='original')
        source=Path(original['path'])
        source_before=hashlib.sha256(source.read_bytes()).hexdigest()
        uploaded=self.root/'fixture-upload.png'
        shutil.copyfile(source,uploaded)
        self.assertTrue(uploaded.resolve().is_relative_to(self.root.resolve()))
        image=add_image(self.store,self.project,'saved-development-screen.png',uploaded)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),source_before)
        version=image['active_version']
        evidence=deepcopy(base['screen']['business_metadata_evidence']['auxiliary_evidence'])
        note=next(i['text'] for i in base['screen']['business_metadata'] if i['kind']=='note' and i['table']==0)
        for part in evidence['text']:
            if part.get('text')==note:part['text']=auxiliary
        evidence['provenance']['image_version']=version
        preparation=deepcopy(base['screen']['prepared'])
        def prepare(source,folder,**kwargs):
            answer=deepcopy(preparation)
            answer['input_path']=str(source)
            answer['input_sha256']=hashlib.sha256(Path(source).read_bytes()).hexdigest()
            for part in answer['prepared_images']:
                target=Path(folder)/(part['id']+'.png')
                shutil.copyfile(part['path'],target)
                part['path']=str(target)
            return answer
        self.manager.prepare_override=prepare
        batch=self.manager.submit(self.project,{'version_ids':[version],'request_id':'metadata-delivery',
            'policy':{'primary_engine':'glm','max_upgrades':0,'fallback_engines':[],
                'require_human_review':False,'layout_evidence':False,'upgrade_trigger':'failure_only'}})['batch_id']
        self.manager._update(batch,version,evidence=encoded(evidence))
        self.manager.step()
        tasks=[]
        while task:=self.store.claim():
            tasks.append(task['id'])
            self.store.complete(task['id'],{'status':'success','engine':task['engine'],
                'text':base['text'].replace(note,primary),'tables':[deepcopy(batch_fixture.TABLE)],
                'blocks':[],'project_image_version':task['version_id'],'elapsed_seconds':.1})
        self.manager.step()
        state=self.manager.get(self.project,batch)
        item=state['items'][0]
        result=self.store.result(item['result_id'])
        return state,item,result,tasks

    def test_sign_conflict_cannot_auto_deliver_even_when_human_review_preference_is_false(self):
        auxiliary='表注：本季度调整金额为-20.00万元。'
        primary='表注：本季度调整金额为+20.00万元。'
        state,item,result,tasks=self.execute(auxiliary,primary)
        self.assertFalse(state['policy']['require_human_review'])
        self.assertEqual(item['status'],'needs_review')
        self.assertNotEqual(item['review_state'],'accepted')
        self.assertTrue(any(i['code']=='business_metadata_review_required' for i in item['issues']))
        selected=result['original']['screen']['selection']
        self.assertTrue(selected['table_checks_passed'])
        self.assertFalse(selected['automatic_checks_passed'])
        self.assertFalse(selected['automatic_delivery'])
        self.assertTrue(selected['business_metadata_review_required'])
        self.assertTrue(selected['human_review_required'])
        part=next(i for i in result['original']['screen']['business_metadata'] if i['kind']=='note' and i['table']==0)
        self.assertEqual(part['text'],primary)
        self.assertEqual(part['alternative_text'],auxiliary)
        self.assertTrue(part['transcription_evidence']['automatic_apply'])
        self.assertFalse(part['human_confirmed'])
        ledger=result['original']['screen']['business_metadata_evidence']
        self.assertTrue(ledger['metadata_values_modified'])
        self.assertFalse(ledger['cell_values_modified'])
        self.assertFalse(ledger['semantic_correction_claimed'])
        self.assertEqual(len(tasks),1)
        self.assertEqual(state['counts']['needs_review'],1)

    def test_exact_independent_metadata_agreement_preserves_optional_review_policy(self):
        text='表注：括号表示冲回；空格为未填，重复业务保留原文。'
        state,item,result,tasks=self.execute(text,text)
        self.assertEqual(item['status'],'completed')
        self.assertTrue(result['original']['screen']['selection']['automatic_checks_passed'])
        self.assertFalse(result['original']['screen']['selection']['business_metadata_review_required'])
        self.assertFalse(result['original']['screen']['business_metadata_evidence']['metadata_values_modified'])
        self.assertEqual(len(tasks),1)

if __name__=='__main__':unittest.main()
