"""AIGC lifecycle integration uses explicit synthetic workers, not accuracy data."""
from copy import deepcopy
import json

import pytest

from packages.contracts.multimodal import AigcImageResult
from workbench_platform.multimodal import aigc_backend
from tests.integration.test_multimodal_dispatch import service, until
from tests.unit.test_aigc_backend import signal


@pytest.fixture
def aigc(service,monkeypatch):
    value,create,calls,outcomes=service
    original=value.capabilities
    monkeypatch.setattr(value,'capabilities',lambda **kwargs:{'items':original(**kwargs)['items']+[
        {'kind':'aigc_image','runtime':'synthetic','cpu_ready':False,'gpu_ready':True,'missing':[]}]})
    binding={'engine':'community_forensics384','version':'test-v1','runtime':__import__('sys').executable,
        'calibration':None,'preprocessing_version':'official-test-v1'}
    monkeypatch.setattr(aigc_backend,'bind',lambda *_args,**_kwargs:deepcopy(binding))
    monkeypatch.setattr(aigc_backend,'verify',lambda *_args:None)
    monkeypatch.setattr(value,'capability_for_item',lambda item:{'cpu_ready':False,'gpu_ready':True})
    runner=value.runner.run
    commands=[]
    def run(command,output,env,request,**kwargs):
        commands.append(command)
        outcome=runner(command,output,env,request,**kwargs)
        if outcome.status=='succeeded' and request['kind']=='aigc_image':
            path=output/'result.json';result=json.loads(path.read_text('utf-8'))
            result['aigc_image']=signal()
            result['source_sha256']=request['inputs'][0]['blob_hash']
            path.write_text(json.dumps(result),encoding='utf-8')
        return outcome
    monkeypatch.setattr(value.runner,'run',run)
    return value,create,calls,outcomes,commands


def test_aigc_oom_only_reduces_feature_batch_and_retains_explicit_identity(aigc):
    value,create,calls,outcomes,commands=aigc
    outcomes.extend(['oom','succeeded'])
    created=create('aigcOom',kind='aigc_image')
    item=until(lambda:(item if item['status']=='succeeded' else None) if (item:=value.get('wTest',created['job_id'])) else None)
    assert [call['runner']['aigc_feature_batch_size'] for call in calls]==[32,16]
    assert all('safire_points_per_batch' not in call['runner'] for call in calls)
    assert all(call['options']['device']=='cuda' for call in calls)
    assert all(any(argument.endswith('aigc_worker.py') for argument in command) for command in commands)
    assert all(command[1:3]==['-B','-X'] and command[3].startswith('pycache_prefix=') for command in commands)
    assert calls[0]['model_binding']==calls[1]['model_binding']
    assert aigc_backend.public_identity(calls[0]['model_binding'])==item['model_binding']
    assert item['actual_device']=='cuda'
    assert AigcImageResult.model_validate(item['result']['aigc_image']).state=='unable_to_assess'
    marker=json.loads((value.root.parent/'product-reader-requirements.json').read_text('utf-8'))
    assert 'aigc-image-v1' in marker['reader_features']
    assert create('aigcOom',kind='aigc_image')['job_id']==item['job_id']


def test_aigc_second_oom_fails_and_cannot_implicitly_retry_cpu(aigc):
    value,create,calls,outcomes,_=aigc
    outcomes.extend(['oom','oom'])
    created=create('aigcDoubleOom',kind='aigc_image')
    item=until(lambda:(item if item['status']=='failed' else None) if (item:=value.get('wTest',created['job_id'])) else None)
    assert len(calls)==2
    assert {call['options']['device'] for call in calls}=={'cuda'}
    with pytest.raises(ValueError):value.control('wTest',created['job_id'],'retry_cpu')


def test_aigc_fixed_snapshot_review_and_export_keep_experimental_signal(aigc):
    value,create,_,_,_=aigc
    created=create('aigcReviewExport',kind='aigc_image')
    item=until(lambda:(item if item['status']=='succeeded' else None) if (item:=value.get('wTest',created['job_id'])) else None)
    original=value.snapshot('wTest',item['job_id'])
    reviewed=value.review('wTest',item['job_id'],{'request_id':'reviewAigc','expected_revision':item['revision'],
        'actor':'Human tester','note':'Synthetic worker lifecycle only; not detection evidence.','assessment':'inconclusive'})
    latest=value.snapshot('wTest',item['job_id'])
    assert original['review_state']=='unreviewed' and latest['review_state']=='reviewed'
    assert original['result_hash']==latest['result_hash'] and original['snapshot_hash']!=latest['snapshot_hash']
    assert reviewed['result']['aigc_image']['qualification']=='experimental'
    archive,files=value.snapshot_export('wTest',item['job_id'],latest['snapshot_id'])
    exported=json.loads(files['result.json'].read_text('utf-8'))
    assert archive.is_file() and exported['inputs']==item['inputs']
    assert exported['result']['aigc_image']['state']=='unable_to_assess'
    assert value.snapshot('wTest',item['job_id'],original['snapshot_id'])['review_state']=='unreviewed'
    # Historical review receipts expose compact identities. Recovery must
    # rehydrate only the same private task binding before checking raw output.
    with value.transaction() as db:
        db.execute('DELETE FROM manual_result_snapshot WHERE snapshot_id=?',(latest['snapshot_id'],))
        private=value.row(db,'wTest',item['job_id'])
        assert private['model_binding'].get('identity_format') is None
    recovered=value.snapshot_at_revision('wTest',item['job_id'],latest['revision'])
    assert recovered['snapshot_hash']==latest['snapshot_hash']


def test_old_reader_rejects_aigc_even_without_feature_marker(aigc):
    from scripts.product_compatibility import DATA_COMPATIBILITY,check
    value,create,_,_,_=aigc
    created=create('aigcCompatibility',kind='aigc_image')
    until(lambda:value.get('wTest',created['job_id'])['status']=='succeeded')
    descriptor=deepcopy(DATA_COMPATIBILITY)
    descriptor['reader_features'].remove('aigc-image-v1')
    with pytest.raises(ValueError,match='读取能力'):check(value.root.parent,descriptor)
    (value.root.parent/'product-reader-requirements.json').unlink()
    with pytest.raises(ValueError,match='AIGC'):check(value.root.parent,descriptor)
