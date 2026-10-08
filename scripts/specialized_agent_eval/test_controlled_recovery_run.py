"""Actual-continuation refusal gates; no service, controller or model runs."""
import argparse
import asyncio
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from common import fpath,frozen_json,read_json
import controlled_recovery_run as runner

OUTPUT=None


class RunGates(unittest.TestCase):
    def setUp(self):
        self.folder=OUTPUT/self._testMethodName;self.folder.mkdir()
        self.prepared=self.folder/'prepared.json'
        frozen_json(self.prepared,{'input_count':60,'successful_context_tasks':list(range(60)),
            'preserved_primary_tasks':list(range(26)),'runtime_root':str(self.folder)})
        self.valid={'sources':{'freeze':{'path':'F:/fixture-only'}},'runtime_root':str(self.folder)}
        self.state={'screen_items':[{'status':'paused'}]*60,'screen_batches':[{'status':'paused',
            'policy':'{"primary_engine":"hunyuan","request_timeout_seconds":180,"budget_seconds":360}'}]}

    def test_unknown_gpu_slot_refused_before_any_scope_read(self):
        with patch.object(runner.recovery,'validate_launch',side_effect=AssertionError('scope opened')):
            with self.assertRaisesRegex(ValueError,'root-owned'):
                runner.gate(self.prepared,'F:/unused','different-slot')

    def test_scope_expansion_or_missing_tasks_refused(self):
        for key,value in [('input_count',61),('successful_context_tasks',list(range(59))),('preserved_primary_tasks',list(range(25)))]:
            item=read_json(self.prepared);item[key]=value;path=self.folder/(key+'.json');frozen_json(path,item)
            with patch.object(runner.recovery,'validate_launch',return_value=self.valid):
                with self.assertRaisesRegex(ValueError,'H60 development scope'):
                    runner.gate(path,'F:/unused',runner.GPU_SLOT)

    def test_unpaused_scope_and_increased_request_budget_refused(self):
        for state in [dict(self.state,screen_items=[{'status':'running'}]),
                      dict(self.state,screen_batches=[{'status':'running','policy':self.state['screen_batches'][0]['policy']}]),
                      dict(self.state,screen_batches=[{'status':'paused','policy':'{"primary_engine":"hunyuan","request_timeout_seconds":181,"budget_seconds":360}'}])]:
            with patch.object(runner.recovery,'validate_launch',return_value=self.valid),patch.object(runner.recovery,'native_state',return_value=state):
                with self.assertRaises(ValueError):runner.gate(self.prepared,'F:/unused',runner.GPU_SLOT)

    def test_exact_failed_h26_to_h36_lineage_is_accepted_without_dispatch(self):
        item=read_json(self.prepared)
        item.update({'continuation_kind':'failed-recovery-followup','preserved_primary_tasks':list(range(36)),
                     'lineage':{'base_primary_task_ids':list(range(26)),'added_primary_task_ids':list(range(26,36)),
                                'closed_execution_segments':[{'task_id':0}]}})
        path=self.folder/'followup.json';frozen_json(path,item)
        with patch.object(runner.recovery,'validate_launch',return_value=self.valid),patch.object(runner.recovery,'native_state',return_value=self.state):
            self.assertEqual(runner.gate(path,'F:/unused',runner.GPU_SLOT)[1]['preserved_primary_tasks'],list(range(36)))

    def test_followup_gate_cannot_be_used_to_expand_or_reconstruct_existing_scope(self):
        for current,added,closed in [(list(range(37)),list(range(26,37)),[{}]),
                                    (list(range(36)),list(range(25,35)),[{}]),
                                    (list(range(36)),list(range(26,36)),[])]:
            item=read_json(self.prepared)
            item.update({'continuation_kind':'failed-recovery-followup','preserved_primary_tasks':current,
                         'lineage':{'base_primary_task_ids':list(range(26)),'added_primary_task_ids':added,
                                    'closed_execution_segments':closed}})
            path=self.folder/('bad-'+str(len(current))+'-'+str(len(closed))+'-'+str(added[0])+'.json');frozen_json(path,item)
            with patch.object(runner.recovery,'validate_launch',return_value=self.valid):
                with self.assertRaisesRegex(ValueError,'exact H26 to H36'):
                    runner.gate(path,'F:/unused',runner.GPU_SLOT)

    def test_absent_source_binding_refusal_is_not_caught_as_permission(self):
        with patch.object(runner.recovery,'validate_launch',side_effect=ValueError('frozen source missing')):
            with self.assertRaisesRegex(ValueError,'frozen source missing'):
                runner.gate(self.prepared,'F:/unused',runner.GPU_SLOT)

    def test_changed_actual_runner_refused_before_any_service_import(self):
        path=self.folder/'execution.json'
        frozen_json(path,{'stage':'prepared','gpu_slot':runner.GPU_SLOT,'helper':{'sha256':'wrong'}})
        with patch.object(runner.recovery,'require_environment'):
            with self.assertRaisesRegex(ValueError,'runner changed'):
                asyncio.run(runner.run(path,runner.GPU_SLOT))

    def test_resume_identity_is_distinct_per_execution_and_stable_for_retries(self):
        first={'output':str(self.folder/'execution-001'),'original_batch_id':'same-original-batch',
               'gpu_slot':runner.GPU_SLOT,'prepared':{'sha256':'same-prepared-scope'}}
        second={**first,'output':str(self.folder/'execution-002')}
        identity=runner.resume_request_id(first)
        self.assertEqual(identity,runner.resume_request_id(dict(first)))
        self.assertNotEqual(identity,runner.resume_request_id(second))
        self.assertLessEqual(len(identity),64)

    def test_resume_identity_tampering_is_refused_before_any_service_import(self):
        value={'stage':'prepared','gpu_slot':runner.GPU_SLOT,'helper':runner.recovery.bound(Path(runner.__file__)),
               'output':str(self.folder),'original_batch_id':'same-original-batch',
               'prepared':{'sha256':'frozen-prepared-scope'},'resume_request_id':'wrong-operation'}
        path=self.folder/'bad-resume-execution.json';frozen_json(path,value)
        with patch.object(runner.recovery,'require_environment'):
            with self.assertRaisesRegex(ValueError,'resume identity changed'):
                asyncio.run(runner.run(path,runner.GPU_SLOT))

    def test_followup_dispatch_requires_exact_independent_cpu_readback(self):
        binding=self.folder/'binding.json';frozen_json(binding,{'fixture':True})
        cpu=self.folder/'restore-cpu.json';frozen_json(cpu,{'success':True})
        original={'continuation_kind':'failed-recovery-followup','recovery_root':str(self.folder)}
        receipt={'success':True,'prepared':runner.recovery.bound(self.prepared),
                 'source_binding':runner.recovery.bound(binding),'actual_recovery_proven':False,
                 'service_or_model_started':False,'restore_cpu':runner.recovery.bound(cpu)}
        path=self.folder/'evidence/independent-readback-001/result.json';frozen_json(path,receipt)
        self.assertTrue(runner.readiness(self.prepared,binding,original)['fresh_root_gpu_slot_confirmation_required'])
        binding.write_text('{"changed":true}','utf-8')
        with self.assertRaisesRegex(ValueError,'independent readiness'):
            runner.readiness(self.prepared,binding,original)

    def partial_export_services(self,has_xlsx):
        sys.path[:0]=['F:/OCR/output/platform-specialized-agent-20261007/source-snapshots/content-native-dev-006/platform',
                      'F:/OCR/output/platform-specialized-agent-20261007/source-snapshots/content-native-dev-006/platform/src',
                      'F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/build/public-quality-20260916/tool/site-packages']
        source=self.folder/'artifact.zip'
        with zipfile.ZipFile(source,'w') as archive:
            if has_xlsx:
                from openpyxl import Workbook
                book=Workbook();book.active['A1']='123';data=BytesIO();book.save(data);book.close()
                archive.writestr('one.xlsx',data.getvalue())
            else:archive.writestr('manifest.json','{}')
        @contextmanager
        def download(*a,**k):yield source
        artifact=SimpleNamespace(artifact_id='original-export-artifact',revision=1,blob_hash=runner.recovery.file_sha(source))
        return SimpleNamespace(screen_batches=SimpleNamespace(export=lambda *a,**k:{'bundle_id':'real-fixture-bundle'}),
            artifact_center=SimpleNamespace(file=lambda *a,**k:artifact,store=SimpleNamespace(stage_download=download)))

    def test_first_excel_stage_requires_actual_zip_download_and_workbook_readback(self):
        result=runner.observe_first_downloadable_excel(self.partial_export_services(True),'existing-batch',self.folder)
        self.assertEqual(result['xlsx_files'],1)
        self.assertTrue(result['xlsx_readable'])
        self.assertFalse(result['content_quality_proven'])

    def test_manifest_or_candidate_alone_is_not_counted_as_a_downloadable_excel(self):
        with self.assertRaisesRegex(ValueError,'no downloadable Excel'):
            runner.observe_first_downloadable_excel(self.partial_export_services(False),'existing-batch',self.folder)


def main():
    global OUTPUT
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    OUTPUT=fpath(args.output)
    if OUTPUT.exists():raise ValueError('New refusal-test output required')
    OUTPUT.mkdir(parents=True)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RunGates))
    frozen_json(OUTPUT/'result.json',{'tests':result.testsRun,'success':result.wasSuccessful(),
        'failures':len(result.failures),'errors':len(result.errors),
        'runner_sha256':runner.recovery.file_sha(Path(runner.__file__)),
        'test_sha256':runner.recovery.file_sha(Path(__file__)),
        'service_or_model_started':False,'actual_recovery_proven':False})
    if not result.wasSuccessful():raise SystemExit(1)


if __name__=='__main__':main()
