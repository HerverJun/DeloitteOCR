"""Current visual-runtime VC lock transforms keep frozen task identities intact."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/package_portable.py'
spec=importlib.util.spec_from_file_location('clear_test_visual_vc_packager',SCRIPT)
packager=importlib.util.module_from_spec(spec);sys.modules[spec.name]=packager
spec.loader.exec_module(packager)
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'ocr/src'))
from ocr_workbench import multimodal_runtime as runtime


def identity(content):
    return {'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}


@pytest.fixture
def visual_package(tmp_path):
    output=tmp_path/'Clear';source=tmp_path/'source'
    bundle=output/'assets/program/bundles/ocr'
    original=json.loads((Path(__file__).resolve().parents[3]/'ocr/config/multimodal-review.json').read_text('utf-8'))
    original['runtime']['assets']=[]
    for name in ('llama-server.exe','ggml.dll','vcruntime140.dll'):
        member='runtimes/llama/'+name;content=('upstream:'+name).encode()
        path=bundle/member;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        original['runtime']['assets'].append({'path':member,**identity(content)})
    for profile in original['profiles'].values():
        for key in ('model','projector','license_asset'):
            asset=profile[key];content=('synthetic:'+asset['path']).encode()
            asset.update(identity(content))
            path=bundle/asset['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
    configs=[output/'app/ocr/config/multimodal-review.json',bundle/'config/multimodal-review.json']
    source_config=source/'ocr/config/multimodal-review.json'
    for path in (*configs,source_config):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(original,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    vc_files={};bindings={}
    for name in ('msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll','vcomp140.dll'):
        content=('fixed-14.51:'+name).encode();vc_files[name]=identity(content)
        path=bundle/'runtimes/llama'/name;path.write_bytes(content)
        bindings[path.relative_to(output).as_posix()]=identity(content)
    vc_files['LICENSE.txt']=identity(b'original upstream license')
    lock=source/'platform/config/runtime-locks/vc-runtime.json';lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({'format':'clear-vc-runtime-lock-v1','version':'14.51.36247.0',
        'archive':{'sha256':'a'*64},'files':vc_files}),encoding='utf-8')
    return output,{'source':str(source)},bindings,bundle,original,configs,source_config


def test_current_vc_merge_is_pinned_idempotent_and_preserves_model_profiles(visual_package):
    output,settings,bindings,bundle,original,configs,source_config=visual_package
    frozen=runtime.load_config(bundle,config_path=source_config)
    generated=packager.portable_visual_review_vc(output,settings,bindings)
    assert packager.portable_visual_review_vc(output,settings,bindings)==generated
    transformed=json.loads(configs[0].read_text('utf-8'))
    assert configs[0].read_bytes()==configs[1].read_bytes()
    assert transformed['profiles']==original['profiles']
    assert transformed['limits']==original['limits']
    assert transformed['prompt_version']==original['prompt_version']
    assert transformed['runtime']['version']==original['runtime']['version']
    assets={row['path']:row for row in transformed['runtime']['assets']}
    assert len(assets)==6
    for member,expected in bindings.items():
        relative=member.removeprefix('assets/program/bundles/ocr/')
        assert assets[relative]=={'path':relative,**expected}
    current=runtime.load_config(bundle)
    assert runtime.review_readiness(bundle,current,verify_hashes=True)['ready']
    old=runtime.review_readiness(bundle,frozen,verify_hashes=True)
    assert not old['ready'] and '未锁定' in old['reason']
    receipt=json.loads((output/'manifests/visual-review-runtime.json').read_text('utf-8'))
    assert not receipt['profiles_changed'] and not receipt['frozen_task_bindings_rewritten']
    assert receipt['runtime_sha256']!=receipt['upstream_runtime_sha256']
    assert receipt['config_files']==generated


def test_unknown_and_same_size_tampered_runtime_dll_still_fail_readiness(visual_package):
    output,settings,bindings,bundle,_original,_configs,_source=visual_package
    packager.portable_visual_review_vc(output,settings,bindings)
    current=runtime.load_config(bundle)
    assert runtime.review_readiness(bundle,current,verify_hashes=True)['ready']
    extra=bundle/'runtimes/llama/unknown.dll';extra.write_bytes(b'unknown')
    assert '未锁定' in runtime.review_readiness(bundle,current,verify_hashes=True)['reason']
    with pytest.raises(ValueError,match='Unreviewed DLL'):
        packager.portable_visual_review_vc(output,settings,bindings)
    extra.unlink()
    path=bundle/'runtimes/llama/vcomp140.dll';info=path.stat()
    path.write_bytes(b'X'*info.st_size);os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
    assert 'SHA256' in runtime.review_readiness(bundle,current,verify_hashes=True)['reason']
    with pytest.raises(ValueError,match='fixed app-local binding'):
        packager.portable_visual_review_vc(output,settings,bindings)


def test_output_config_cannot_expand_whitelist_and_duplicate_source_assets_fail(visual_package):
    output,settings,bindings,_bundle,_original,configs,source=visual_package
    value=json.loads(configs[0].read_text('utf-8'))
    value['runtime']['assets'].append({'path':'runtimes/llama/unknown.dll',**identity(b'unknown')})
    configs[0].write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError,match='unreviewed output'):
        packager.portable_visual_review_vc(output,settings,bindings)
    value=json.loads(source.read_text('utf-8'))
    value['runtime']['assets'].append(dict(value['runtime']['assets'][0]))
    source.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError,match='Duplicate'):
        packager.portable_visual_review_vc(output,settings,bindings)


def test_generated_config_component_keeps_all_current_refreshed_source_identities():
    prior={'files':{'app/config.json':identity(b'old config'),'app/code.py':identity(b'old code')}}
    planned=[packager.Member(Path('unused'),name,'ocr-source') for name in
        ('app/config.json','app/code.py','app/new.py')]
    current={'app/config.json':identity(b'current source config'),
        'app/code.py':identity(b'current code'),'app/new.py':identity(b'new source')}
    generated={'app/config.json':identity(b'transformed config')}
    result=packager.vendor_component_identities(prior,planned,[],generated=generated,refreshed=current)
    assert result['ocr-source']=={**current,**generated}
