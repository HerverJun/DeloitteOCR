"""Lifecycle/identity checks; synthetic adapters are never inference evidence."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest

from workbench_platform.multimodal import deep_backend
from workbench_platform.multimodal.service import MultimodalService
from packages.contracts.multimodal import ManualAnalysisCreate


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_legacy_history_never_inherits_new_default(tmp_path,monkeypatch):
    model=tmp_path/'models/catnet/CAT_full_v2.pth.tar'
    model.parent.mkdir(parents=True);model.write_bytes(b'legacy identity')
    service=SimpleNamespace(config={'deep_forensics':{'default_backend':'safire'}},
        program=tmp_path,models=tmp_path/'models',file_hash=digest)
    monkeypatch.setenv('WORKBENCH_DEEP_FORENSICS_BACKEND','safire')
    assert deep_backend.item_binding(service,{'kind':'catnet'})['engine']=='catnet'
    pinned={'engine':'safire','asset_root':str(tmp_path),'resources':{},'runtime':'frozen-runtime'}
    monkeypatch.setenv('WORKBENCH_DEEP_FORENSICS_BACKEND','catnet')
    retrieved=deep_backend.item_binding(service,{'model_binding':pinned})
    assert retrieved==pinned
    retrieved['resources']['changed']={}
    assert not pinned['resources']


def test_retry_detects_changed_weights(tmp_path):
    model=tmp_path/'models/catnet/CAT_full_v2.pth.tar'
    model.parent.mkdir(parents=True);model.write_bytes(b'original')
    service=SimpleNamespace(config={},program=tmp_path,models=tmp_path/'models',file_hash=digest)
    binding=deep_backend.bind(service,'catnet')
    deep_backend.verify(service,binding)
    model.write_bytes(b'replaced')
    with pytest.raises(ValueError,match='historical retry'):
        deep_backend.verify(service,binding)


def test_document_contract_preserves_explicit_cpu():
    body={'request_id':'docCreate','kind':'document_forensics',
        'inputs':[{'asset_id':'assetOne','asset_revision':1,'blob_hash':'a'*64}],
        'options':{'device':'cpu'}}
    assert ManualAnalysisCreate.model_validate(body).options.device=='cpu'


def test_public_binding_is_copy_and_history_unchanged():
    legacy={'job_id':'mOld','kind':'catnet','result':{'algorithm':'CAT-Net v2'},'status':'succeeded'}
    before=deepcopy(legacy)
    assert 'model_binding' not in MultimodalService.public(legacy)
    assert legacy==before
    current={**legacy,'model_binding':{'engine':'safire','resources':{'a':{'sha256':'a'*64}}}}
    public=MultimodalService.public(current)
    public['model_binding']['resources'].clear()
    assert current['model_binding']['resources']
