"""Prepare one uniquely named full-scope native model trial, CPU only.

The platform may intentionally remain at a prior freeze while OCR uses tested
working contents. This is explicit quality-development evidence, never final
Agent qualification or a same-source performance comparison. No truth is read.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import importlib.util
import re
import shutil
import sys
from types import SimpleNamespace

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, copy_bytes, directory_inventory, file_inventory, plain_path, require_environment
from freeze_sources import PARTS, inventory


def psquote(value):
    return "'"+str(value).replace("'","''")+"'"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('platform-freeze','ocr-source','controller-source','inputs',
                 'source-output','prepare-output','actual-output','trial-id'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--engine',choices=('hunyuan','glm','paddlevl'),required=True)
    parser.add_argument('--required-file-sha',action='append',default=[],help='relative OCR path=expected final SHA')
    args=parser.parse_args()
    require_environment()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,79}',args.trial_id):
        raise ValueError('Trial identity must be a unique plain ASCII identifier')
    base,ocr,controller,inputs,route,preparation,actual=map(plain_path,(args.platform_freeze,args.ocr_source,
        args.controller_source,args.inputs,args.source_output,args.prepare_output,args.actual_output))
    if route==preparation or route==actual or preparation==actual or any(p.exists() for p in (route,preparation,actual)):
        raise ValueError('Use three distinct unique F-drive directories')
    freeze=read_json(base/'freeze.json')
    scopes=read_json(inputs)
    split=inputs.parent.name
    if split not in {'development', 'validation'}:
        raise ValueError('This model-comparison preparer only handles development or validation inputs')
    if scopes.get('truth_available_to_inference') is not False or not scopes['samples']:
        raise ValueError('Requires independently frozen truth-free model inputs')
    for sample in scopes['samples']:
        if digest(plain_path(sample['image']))!=sample['image_sha256']:
            raise ValueError('Frozen original input image changed')
    ocr_inventory=inventory(ocr,PARTS['ocr'])
    required={}
    for item in args.required_file_sha:
        key,sha=item.split('=',1)
        if ocr_inventory.get(key)!=sha:
            raise ValueError('Final tested OCR file changed: '+key)
        required[key]=sha
    files={'ocr':dict(ocr_inventory)}
    for family in ('platform','classification'):
        expected=freeze['sources'][family]['files']
        if inventory(base/family,PARTS[family])!=expected:
            raise ValueError('Prior frozen '+family+' changed')
        files[family]=dict(expected)
    for family,expected in files.items():
        origin=ocr if family=='ocr' else base/family
        for name,sha in expected.items():
            path=plain_path(origin/name);target=plain_path(route/family/name)
            if digest(path)!=sha:
                raise ValueError('Source changed before copy: '+name)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
            if digest(target)!=sha:
                raise ValueError('Copied source bytes differ')
    if inventory(ocr,PARTS['ocr'])!=ocr_inventory:
        raise ValueError('Working OCR changed during freeze')
    harness=route/'platform/scripts/evaluate_screen_agent_native.py'
    original=harness.read_text('utf-8')
    replacements={'nativeProject20261007':args.trial_id+'_Project',
                  'nativeSession':args.trial_id+'_Session','nativeGoal':args.trial_id+'_Goal'}
    changed=original
    for before,after in replacements.items():
        if changed.count("'"+before+"'")!=1:
            raise ValueError('Expected unique original request identity')
        changed=changed.replace("'"+before+"'","'"+after+"'")
    class Restore(ast.NodeTransformer):
        def visit_Constant(self,node):
            if isinstance(node.value,str):
                node.value={v:k for k,v in replacements.items()}.get(node.value,node.value)
            return node
    if ast.dump(ast.parse(original))!=ast.dump(Restore().visit(ast.parse(changed))):
        raise ValueError('Harness change exceeds three identity literals')
    compile(changed,str(harness),'exec');harness.write_text(changed,'utf-8')
    files['platform']['scripts/evaluate_screen_agent_native.py']=digest(harness)
    policy_path=route/'ocr/config/screen-policy.json'
    policy=read_json(policy_path)
    policy.update(primary_engine=args.engine,fallback_engines=[],max_upgrades=0,require_human_review=True,
                  budget_seconds=360,request_timeout_seconds=180,batch_budget_seconds=36000,
                  group_size=32,context_schedule='whole_batch',allow_text_fixes=False,visual_review=False)
    policy_path.write_text(__import__('json').dumps(policy,ensure_ascii=False,indent=2)+'\n','utf-8')
    files['ocr']['config/screen-policy.json']=digest(policy_path)
    controller_files=file_inventory(controller)
    copy_bytes(controller,route/'controller',controller_files,directory_inventory(controller))
    bundle=fpath('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr')
    agent_layer=fpath('F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    pdf_layer=fpath('F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/build/public-quality-20260916/tool/site-packages/pdfplumber')
    if not (pdf_layer/'__init__.py').is_file():
        raise ValueError('Explicit F-drive PDF layer unavailable')
    python=bundle/'runtimes/control/python.exe'
    commands=[str(python),'-I','-B','-X','utf8',str(harness),
              '--output',str(actual),'--inputs',str(inputs),'--native-source',str(route/'ocr'),
              '--bundle',str(bundle),'--agent-layer',str(agent_layer),'--pdf-layer',str(pdf_layer),
              '--controller-store',str(route/'controller/operations.sqlite3'),'--timeout','14400','--run']
    binding={'format':'m1-unique-model-trial-source-v1','utc':datetime.now(timezone.utc).isoformat(),
             'base_platform_freeze':bound(base/'freeze.json'),'working_ocr_source':str(ocr),
             'sources':{family:{'path':str(route/family),'files':members,'file_count':len(members)} for family,members in files.items()},
             'ocr_original_files':ocr_inventory,'required_tested_file_hashes':required,
             'controller_byte_copy_only':True,'controller_source_sqlite_opened':False,
             'controller_files_sha256_only':{name:entry['sha256'] for name,entry in controller_files.items()},
             'request_identity_replacements':replacements,'policy':policy,'inputs':bound(inputs),
             'scope_inputs':len(scopes['samples']),'evaluation_split':split,
             'actual_output':str(actual),'launch_argv':commands,
             'quality_nonfinal_trial_only':True,'quality_development_trial_only':split=='development',
             'mixed_platform_and_ocr_freezes_explicit':True,
             'same_source_performance_comparison':False,'final_agent_qualification':False,
             'truth_read':False,'holdout_read':False,'model_invoked':False,'controller_invoked':False,
             'formal_current_changed':False,'helper':bound(__file__)}
    frozen_json(route/'trial-source-binding.json',binding)
    sys.path.insert(0,str(agent_layer))
    spec=importlib.util.spec_from_file_location('m1_preparation_harness',harness)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    native_args=SimpleNamespace(output=preparation,inputs=inputs,native_source=route/'ocr',bundle=bundle,
        agent_layer=agent_layer,context_manifest=None,run=False)
    configured=module.configure(native_args);mapping,prepared=module.prepare(native_args,configured)
    for family,members in files.items():
        if inventory(route/family,PARTS[family])!=members:
            raise ValueError('Trial source changed during preparation')
    if len(mapping)!=len(scopes['samples']) or prepared['screen_policy']!=policy:
        raise ValueError('CPU prepared scope/policy differs')
    readiness={'format':'m1-model-trial-cpu-readiness-v1','prepared':bound(preparation/'prepared.json'),
               'source_binding':bound(route/'trial-source-binding.json'),'input_bytes_verified':len(mapping),
               'source_files_verified':{family:len(members) for family,members in files.items()},
               'launch_argv':commands,'truth_read':False,'holdout_read':False,'gpu_or_controller_calls':0,
               'eligible_for_explicit_root_gpu_slot':True,'quality_or_final_agent_qualified':False}
    frozen_json(preparation/'launch-readiness.json',readiness)
    run_env=fpath('F:/OCR/output/platform-specialized-agent-20261007/run-env.ps1')
    (preparation/'launch-actual.ps1').write_text('. '+psquote(run_env)+'\n& '+' '.join(psquote(v) for v in commands)+'\nexit $LASTEXITCODE\n','utf-8')
    print({'inputs':len(mapping),'engine':args.engine,'source_binding':str(route/'trial-source-binding.json'),
           'readiness':str(preparation/'launch-readiness.json'),'launch':str(preparation/'launch-actual.ps1'),
           'gpu_or_controller_calls':0})


if __name__=='__main__':
    main()
