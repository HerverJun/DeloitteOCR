"""Runner configuration survives synthetic OOM without changing task semantics."""
from tests.integration.test_multimodal_dispatch import service, until


def test_catnet_context_is_preserved_when_only_input_limit_is_reduced(service):
    value,create,calls,outcomes=service
    value.config['catnet']['halo']=64
    outcomes.append('oom')
    created=create('catnetContextRetry')
    until(lambda:value.get('wTest',created['job_id'])['status']=='succeeded')
    assert [call['runner']['catnet_tile_size'] for call in calls]==[1024,512]
    assert [call['runner']['catnet_halo'] for call in calls]==[64,64]
    assert all(call['runner']['generation']==1 for call in calls)
    assert value.coordinator.snapshot().owner is None


def test_missing_context_config_keeps_legacy_zero_halo(service):
    value,create,calls,_=service
    value.config['catnet'].pop('halo',None)
    created=create('catnetLegacyConfig')
    until(lambda:value.get('wTest',created['job_id'])['status']=='succeeded')
    assert calls[0]['runner']['catnet_halo']==0
