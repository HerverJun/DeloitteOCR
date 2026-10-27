"""An unqualified tool listing must not scan unrelated multi-GB runtime bytes."""
import json

import pytest

from workbench_platform.multimodal import aigc_backend, deep_backend
from workbench_platform.multimodal.qualification import FORMAT, Qualification
from workbench_platform.multimodal.service import MultimodalService


def unqualified_service(tmp_path):
    value=MultimodalService.__new__(MultimodalService)
    runtime=tmp_path/'runtime/python.exe'
    runtime.parent.mkdir()
    runtime.write_bytes(b'synthetic executable, never executed')
    value.config={'deep_forensics':{'asset_root':str(tmp_path),'runtime':str(runtime)},
                  'aigc_image':{'asset_root':str(tmp_path)},'gpu':{}}
    value.safire_qualification=Qualification(tmp_path/'safire-qualification.json')
    value.aigc_qualification=Qualification(tmp_path/'aigc-qualification.json')
    return value,runtime


def test_safire_without_receipt_checks_presence_without_hashing_runtime(tmp_path,monkeypatch):
    value,runtime=unqualified_service(tmp_path)
    checked=[]
    def presence(config,hash_file):
        checked.append(hash_file(runtime))
        return {}
    monkeypatch.setattr(deep_backend,'assets',presence)
    monkeypatch.setattr(deep_backend,'bind',lambda *_args:pytest.fail('listing must not bind an unqualified runtime'))
    value.safire_gpu_binding=lambda *_args:pytest.fail('listing must not recursively hash runtime libraries')
    result=value.safire_capability('catnet')
    assert checked==[None]
    assert result['state']=='resources_present' and not result['gpu_ready']
    runtime.unlink()
    assert value.safire_capability('document_forensics')['state']=='resources_missing'


def test_safire_with_receipt_revalidates_binding_and_never_assumes_ready(tmp_path,monkeypatch):
    value,runtime=unqualified_service(tmp_path)
    value.safire_qualification.path.write_text(json.dumps({'format':FORMAT,'items':{'catnet':{}}}))
    calls=[]
    monkeypatch.setattr(deep_backend,'bind',lambda *_args:{'runtime':str(runtime),'resources':{}})
    value.safire_gpu_binding=lambda *_args:calls.append('binding') or {'kind':'catnet'}
    value.safire_qualification.ready=lambda *_args,**_kwargs:False
    assert not value.safire_capability('catnet')['gpu_ready']
    assert calls==['binding']
    value.safire_qualification.ready=lambda *_args,**_kwargs:True
    assert value.safire_capability('catnet')['gpu_ready']
    assert calls==['binding','binding']


def aigc_inventory(tmp_path,runtime):
    checkpoint=tmp_path/'weights.bin'
    checkpoint.write_bytes(b'synthetic weight, not a model')
    inventory={'format':'clear-aigc-assets-v1','primary':'community_forensics384','runtime':str(runtime),
        'engines':{'community_forensics384':{'resources':{'weights.bin':{'bytes':checkpoint.stat().st_size,'sha256':'a'*64}}}}}
    (tmp_path/'aigc-inventory.json').write_text(json.dumps(inventory))
    return checkpoint


def test_aigc_without_receipt_checks_resources_without_import_closure_scan(tmp_path,monkeypatch):
    value,runtime=unqualified_service(tmp_path)
    checkpoint=aigc_inventory(tmp_path,runtime)
    monkeypatch.delenv('WORKBENCH_AIGC_ROOT',raising=False)
    monkeypatch.delenv('WORKBENCH_AIGC_ENGINE',raising=False)
    monkeypatch.setattr(aigc_backend,'bind',lambda *_args:pytest.fail('listing must not bind an unqualified runtime'))
    value.aigc_gpu_binding=lambda *_args:pytest.fail('listing must not hash transitive runtime dependencies')
    result=value.aigc_capability()
    assert result['state']=='resources_present' and not result['gpu_ready']
    checkpoint.write_bytes(b'changed length')
    assert value.aigc_capability()['state']=='resources_missing'


def test_aigc_malformed_resource_metadata_is_a_reported_missing_resource(tmp_path,monkeypatch):
    value,runtime=unqualified_service(tmp_path)
    aigc_inventory(tmp_path,runtime)
    monkeypatch.delenv('WORKBENCH_AIGC_ROOT',raising=False)
    monkeypatch.delenv('WORKBENCH_AIGC_ENGINE',raising=False)
    path=tmp_path/'aigc-inventory.json'
    inventory=json.loads(path.read_text())
    inventory['engines']['community_forensics384']['resources']['weights.bin']=None
    path.write_text(json.dumps(inventory))
    assert value.aigc_capability()['state']=='resources_missing'


def test_aigc_with_receipt_or_frozen_task_keeps_full_integrity_checks(tmp_path,monkeypatch):
    value,runtime=unqualified_service(tmp_path)
    binding={'engine':'community_forensics384','runtime':str(runtime),'calibration':None}
    value.aigc_qualification.path.write_text(json.dumps({'format':FORMAT,'items':{'aigc_image':{}}}))
    calls=[]
    monkeypatch.setattr(aigc_backend,'bind',lambda *_args:calls.append('bind') or binding)
    value.aigc_gpu_binding=lambda *_args:calls.append('gpu-binding') or {'kind':'aigc_image'}
    value.aigc_qualification.ready=lambda *_args,**_kwargs:False
    assert not value.aigc_capability()['gpu_ready']
    assert calls==['bind','gpu-binding']
    value.aigc_qualification.path.unlink()
    def corrupted(*_args):
        raise ValueError('Pinned AIGC runtime dependency changed')
    monkeypatch.setattr(aigc_backend,'verify',corrupted)
    with pytest.raises(ValueError,match='dependency changed'):
        value.capability_for_item({'kind':'aigc_image','model_binding':binding})


@pytest.mark.parametrize('body',[{}, {'format':FORMAT,'items':{'catnet':None}}, {'format':'old','items':{'catnet':{}}}])
def test_invalid_receipt_does_not_trigger_expensive_validation_or_enable_gpu(tmp_path,body):
    value,_runtime=unqualified_service(tmp_path)
    value.safire_qualification.path.write_text(json.dumps(body))
    assert not value._has_gpu_receipt(value.safire_qualification,'catnet')
