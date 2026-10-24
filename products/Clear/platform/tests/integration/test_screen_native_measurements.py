"""Validate measurement semantics without a model, GPU query or service."""
import json

from scripts.evaluate_screen_agent_native import device_sample_summary, native_timing_receipts


def test_device_and_native_measurements_preserve_distinct_samples_and_costs():
    device=device_sample_summary([
        {'exit_code':0,'observed':'GPU A, 1000, 32000, 30\nGPU A, 2000, 32000, 50'},
        {'exit_code':0,'observed':'GPU A, 1500, 32000, 40\nGPU A, 2500, 32000, 90'},
        {'unavailable':'TimeoutExpired'},
        {'exit_code':0,'observed':'GPU A, nan, 32000, 50'}])
    assert device['devices']['0']['sampled_peak_memory_used_mib']==1500
    assert device['devices']['1']['sampled_peak_memory_used_mib']==2500
    assert device['devices']['0']['sample_count']==2
    assert 'no per-process' in device['measurement']
    tables={'tasks':[{'id':'taskA','result_id':'resultA','kind':'screen_ocr','engine':'hunyuan',
        'status':'succeeded','started':'2026-10-07T01:00:00Z','finished':'2026-10-07T01:00:10Z'}],
        'results':[{'id':'resultA','original':json.dumps({'elapsed_seconds':6,'inference_seconds':4,'load_seconds':2,'cache_hit':{'source_task_id':'old'}})}],
        'screen_tasks':[{'task_id':'taskA','batch_id':'batchA','input_version_id':'imageA','pass_index':1,'spent_seconds':3}],
        'screen_items':[{'batch_id':'batchA','input_version_id':'imageA','status':'needs_review','preparation':json.dumps({'preparation_seconds':1.5})}]}
    value=native_timing_receipts(tables)
    task=value['tasks'][0]
    assert task['last_timestamp_segment_wall_seconds']==10
    assert task['prior_active_segment_seconds']==3
    assert task['raw_timing']['elapsed_seconds']==6
    assert task['raw_timing']['cache_hit']=={'source_task_id':'old'}
    assert value['preparation'][0]['preparation_seconds']==1.5
    assert 'whole-route wall time' in value['measurement']
