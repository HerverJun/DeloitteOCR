"""Pinning and result safety tests; synthetic identities never qualify inference."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
from types import SimpleNamespace

import pytest

from packages.contracts.multimodal import AigcImageResult, ManualAnalysisCreate
from workbench_platform.multimodal import aigc_backend
from workbench_platform.multimodal.qualification import Qualification
from workbench_platform.multimodal.service import MultimodalService


def signal(**overrides):
    return {'schema_version':'1.0','state':'unable_to_assess','raw_score':.73,
        'score_direction':'higher_is_generated','threshold':None,'calibration':None,
        'qualification':'experimental','scope':['whole_image'],
        'limitations':['Uncalibrated research signal; not a probability.'],
        'preprocessing_version':'official-test-v1',**overrides}


def test_aigc_contract_never_turns_uncalibrated_score_into_a_verdict():
    assert AigcImageResult.model_validate(signal()).raw_score==.73
    for payload in (signal(state='signal_detected'),signal(state='no_significant_signal'),
                    signal(raw_score=float('nan')),signal(threshold=.5),signal(probability=.73)):
        with pytest.raises(ValueError):AigcImageResult.model_validate(payload)
    calibrated=signal(state='signal_detected',threshold=.7,calibration={'threshold':.7,'calibration_id':'frozen1'})
    assert AigcImageResult.model_validate(calibrated).state=='signal_detected'
    assert AigcImageResult.model_validate({**calibrated,'raw_score':.7,'state':'no_significant_signal'}).state=='no_significant_signal'
    with pytest.raises(ValueError):AigcImageResult.model_validate({**calibrated,'state':'no_significant_signal'})
    payload={'request_id':'request1','kind':'aigc_image','inputs':[{'asset_id':'a1','asset_revision':1,'blob_hash':'a'*64}]}
    assert ManualAnalysisCreate.model_validate(payload).kind=='aigc_image'


@pytest.fixture
def pinned(tmp_path,monkeypatch):
    runtime=tmp_path/'runtime/python.exe';runtime.parent.mkdir();runtime.write_bytes(b'fake executable; not inference')
    (runtime.parent/'python312._pth').write_text('python312.zip\n.\nimport site\n',encoding='utf-8')
    checkpoint=tmp_path/'models/test.safetensors';checkpoint.parent.mkdir();checkpoint.write_bytes(b'fake checkpoint')
    resource={'bytes':checkpoint.stat().st_size,'sha256':hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
    inventory={'format':'clear-aigc-assets-v1','primary':'community_forensics384','runtime':str(runtime),
        'runtime_closed_directories':[str(runtime.parent)],
        'runtime_resources':{str(path):{'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in runtime.parent.iterdir()},
        'engines':{'community_forensics384':{'version':'test-v1','code_revision':'fixed-revision',
            'preprocessing_version':'official-test-v1','settings':{'checkpoint':'models/test.safetensors'},
            'resources':{'models/test.safetensors':resource}}}}
    (tmp_path/'aigc-inventory.json').write_text(json.dumps(inventory),encoding='utf-8')
    service=MultimodalService.__new__(MultimodalService)
    service.config={'aigc_image':{'asset_root':str(tmp_path)}}
    service.resource_hash_cache={};service.qualification=Qualification(tmp_path/'qualification.json')
    monkeypatch.delenv('WORKBENCH_AIGC_ROOT',raising=False)
    monkeypatch.delenv('WORKBENCH_AIGC_ENGINE',raising=False)
    monkeypatch.setattr(aigc_backend,'PRODUCT_SOURCES',())
    return service,tmp_path,checkpoint,inventory


def test_binding_is_independent_and_rejects_weight_and_inventory_changes(pinned):
    service,root,checkpoint,_=pinned
    binding=aigc_backend.bind(service)
    assert binding['engine']=='community_forensics384' and binding['calibration'] is None
    assert binding['qualification']=='experimental' and not binding['business_accuracy_qualified']
    aigc_backend.verify(service,binding)
    checkpoint.write_bytes(b'changed checkpoint')
    with pytest.raises(ValueError,match='resource changed'):aigc_backend.verify(service,binding)


def test_persisted_binding_does_not_follow_new_default_or_root(pinned,monkeypatch):
    service,root,_,_=pinned
    binding=aigc_backend.bind(service)
    monkeypatch.setenv('WORKBENCH_AIGC_ROOT',str(root/'different-root'))
    monkeypatch.setenv('WORKBENCH_AIGC_ENGINE','spai')
    frozen=aigc_backend.item_binding({'model_binding':binding})
    aigc_backend.verify(service,frozen)
    frozen['settings']['checkpoint']='changed'
    assert binding['settings']['checkpoint']=='models/test.safetensors'
    (root/'aigc-inventory.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='inventory changed'):aigc_backend.verify(service,binding)


@pytest.mark.parametrize('name',['../escape','/absolute','F:/absolute','models\\escape','models/../escape','models//escape'])
def test_asset_inventory_cannot_escape_its_root(tmp_path,name):
    with pytest.raises(ValueError):aigc_backend.member(tmp_path,name)


def test_explicit_worker_dispatch_preserves_legacy_catnet():
    assert aigc_backend.worker_entry('catnet',{'engine':'catnet'})=='worker.py'
    assert aigc_backend.worker_entry('catnet',None)=='worker.py'
    assert aigc_backend.worker_entry('document_forensics',{'engine':'safire'})=='safire_worker.py'
    assert aigc_backend.worker_entry('aigc_image',{'engine':'community_forensics384'})=='aigc_worker.py'
    with pytest.raises(ValueError):aigc_backend.worker_entry('aigc_image',{'engine':'safire'})


def test_acceptance_rejects_calibration_substitution_and_pseudo_heatmaps(tmp_path):
    service=MultimodalService.__new__(MultimodalService)
    frozen={'engine':'community_forensics384','calibration':None,'preprocessing_version':'official-test-v1'}
    current={'kind':'aigc_image','model_binding':frozen,'options':{'device':'cuda'},'inputs':[{'blob_hash':'a'*64}]}
    result={'device':'cuda','model_binding':frozen,'aigc_image':signal(),'source_sha256':'a'*64}
    for invalid in ({**result,'aigc_image':signal(preprocessing_version='different')},
                    {**result,'aigc_image':signal(threshold=.5,calibration={'threshold':.5})},
                    {**result,'heatmap':'fake.png'}, {**result,'device':'cpu'},{**result,'source_sha256':'b'*64}):
        (tmp_path/'result.json').write_text(json.dumps(invalid),encoding='utf-8')
        with pytest.raises(ValueError):service._accept_result(deepcopy(current),tmp_path)


def test_candidate_launcher_verifies_independent_payload(pinned):
    from scripts.harness_candidate_launch import aigc_runtime
    service,root,checkpoint,inventory=pinned
    manifest={'aigc_integration':{'format':'clear-aigc-integration-v1','root':str(root),
        'inventory_sha256':service.file_hash(root/'aigc-inventory.json'),'engine':'community_forensics384',
        'runtime_sha256':service.file_hash(Path(inventory['runtime']))}}
    environment,reads=aigc_runtime(manifest)
    assert environment['WORKBENCH_AIGC_ENGINE']=='community_forensics384'
    assert root in reads
    checkpoint.write_bytes(b'changed model')
    with pytest.raises(ValueError,match='resource changed'):aigc_runtime(manifest)


def test_unlisted_runtime_module_or_changed_native_library_is_rejected(pinned):
    service,root,_,inventory=pinned
    binding=aigc_backend.bind(service)
    module=Path(inventory['runtime']).parent/'undeclared.py'
    module.write_text('raise RuntimeError("not an inference file")',encoding='utf-8')
    with pytest.raises(ValueError,match='undeclared'):aigc_backend.verify(service,binding)
    module.unlink()
    Path(inventory['runtime']).write_bytes(b'changed executable')
    with pytest.raises(ValueError,match='runtime changed'):aigc_backend.verify(service,binding)


def test_public_payloads_bound_size_preserve_private_large_runtime_closure():
    resources={f'F:/pinned/runtime/module-{index:05d}.py':{'bytes':4096,'sha256':'b'*64} for index in range(32572)}
    binding={'engine':'community_forensics384','version':'test-v1','code_revision':'c'*40,
        'preprocessing_version':'official-v1','runtime_sha256':'d'*64,'inventory_sha256':'e'*64,
        'runtime_resources':resources,'runtime_closed_directories':['F:/pinned/runtime'],
        'resources':{'models/detector.safetensors':{'bytes':87300000,'sha256':'f'*64}},
        'product_source_hashes':{'adapter.py':'a'*64},'calibration':None}
    binding['public_identity']=aigc_backend.public_identity(binding)
    item={'kind':'aigc_image','job_id':'mSize','model_binding':binding,
        'result':{'aigc_image':signal(),'model_binding':binding}}
    before=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
    public=MultimodalService.public(item)
    encoded=json.dumps(public).encode()
    assert len(encoded)<8192
    assert len(json.dumps({'items':[public]*10}).encode())<81920
    assert public['model_binding']['runtime_closure']['file_count']==32572
    assert public['result']['model_binding']==public['model_binding']
    assert 'runtime_resources' not in encoded.decode() and 'module-00000' not in encoded.decode()
    assert hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()==before
    assert len(item['model_binding']['runtime_resources'])==32572


def test_fresh_pycache_prefix_ignores_valid_unpinned_preexisting_bytecode(tmp_path):
    from workbench_platform.multimodal.runner import worker_environment
    module=tmp_path/'victim.py';cache=tmp_path/'__pycache__';cache.mkdir()
    module.write_text("VALUE='poison'\n",encoding='utf-8')
    stamp=module.stat()
    py_compile.compile(str(module),cfile=str(cache/f'victim.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc'),doraise=True)
    module.write_text("VALUE='source'\n",encoding='utf-8')
    os.utime(module,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    driver=tmp_path/'driver.py'
    driver.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\nimport victim\nprint(victim.VALUE)\n',encoding='utf-8')
    request=tmp_path/'request.json';request.write_text('{}',encoding='utf-8')
    env=worker_environment(tmp_path,Path(sys.executable))
    # The baseline deliberately reads a matching local pyc. The outer test
    # runner may already redirect caches to another F-drive directory.
    env.pop('PYTHONPYCACHEPREFIX',None)
    baseline=subprocess.run([sys.executable,'-B',str(driver)],env=env,cwd=tmp_path,capture_output=True,text=True,check=True)
    assert baseline.stdout.strip()=='poison'  # -B alone still READS matching pyc.
    command=aigc_backend.worker_command(sys.executable,driver,request)
    prefix=Path(command[3].split('=',1)[1]);assert prefix.is_dir() and not any(prefix.iterdir())
    protected=subprocess.run(command,env=env,cwd=tmp_path,capture_output=True,text=True,check=True)
    assert protected.stdout.strip()=='source'
    assert not any(prefix.iterdir())  # Explicit -B prevents writing new caches.


def test_unlisted_sourceless_bytecode_and_source_shadow_module_are_rejected(pinned):
    service,root,_,inventory=pinned
    source=root/'source';source.mkdir();(source/'detector.py').write_text('VALUE=1',encoding='utf-8')
    engine=inventory['engines']['community_forensics384'];engine['settings']['source_root']='source'
    engine['resources']['source/detector.py']={'bytes':(source/'detector.py').stat().st_size,'sha256':service.file_hash(source/'detector.py')}
    (root/'aigc-inventory.json').write_text(json.dumps(inventory),encoding='utf-8')
    binding=aigc_backend.bind(service)
    (source/'shadow.py').write_text('VALUE=2',encoding='utf-8')
    with pytest.raises(ValueError,match='source closure'):aigc_backend.verify(service,binding)
    (source/'shadow.py').unlink()
    py_compile.compile(str(source/'detector.py'),cfile=str(source/'shadow.pyc'),doraise=True)
    with pytest.raises(ValueError,match='sourceless'):aigc_backend.verify(service,binding)
