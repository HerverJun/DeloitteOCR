import hashlib
import io
import json
import os
from pathlib import Path
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from packages.contracts.models import SourceLocator
from workbench_platform.app import app
from tests.integration.test_processing_batches import LOCAL,PREFIX,workspace
from tests.integration.test_processing_batches import DATA_PYTHON

FIXTURES=Path(os.environ.get('CLEAR_MULTIMODAL_FIXTURES','F:/OCR/output/multimodal-manual-upgrade-20261013-001/fixtures'))
DATA_PYTHON=Path(os.environ.get('WORKBENCH_DATA_PYTHON',str(DATA_PYTHON)))

def wait(client,base,identifier):
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        response=client.get(base+'/jobs/'+identifier)
        assert response.status_code==200,response.text
        item=response.json()
        if item['status'] not in {'queued','running','cancelling'}:return item
        time.sleep(.05)
    raise AssertionError('manual worker did not finish')

def test_temporal_locator_and_legacy_identity():
    value={'schema_version':'1.0','kind':'file','asset_id':'aTest','asset_revision':1,'blob_hash':'a'*64,'precision':'exact'}
    old=SourceLocator.model_validate(value).model_dump(mode='json')
    assert not {'start_ms','end_ms','channel'} & old.keys()
    temporal={**value,'kind':'media_segment','start_ms':50,'end_ms':150,'channel':0,'precision':'approximate'}
    assert SourceLocator.model_validate(temporal).end_ms==150
    for invalid in ({**temporal,'end_ms':50},{**temporal,'page_number':1},{**value,'start_ms':1},{**temporal,'channel':-1}):
        with pytest.raises(ValueError):SourceLocator.model_validate(invalid)

def test_real_face_project_library_review_artifacts_and_replay(tmp_path,monkeypatch):
    data=tmp_path/'workspace';data.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(data));monkeypatch.setenv('WORKBENCH_AGENT_ENABLED','0')
    raw=(FIXTURES/'face-upstream-demo.jpg').read_bytes()
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        ws=workspace(client)
        uploaded=client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads',params={'name':'upstream-engineering-demo.jpg'},headers=LOCAL,content=raw)
        assert uploaded.status_code==201,uploaded.text
        asset=uploaded.json();base=f'{PREFIX}/workspaces/{ws}/multimodal'
        command={'request_id':'faceRealDetect','kind':'face','inputs':[{'asset_id':asset['asset_id'],'asset_revision':asset['revision'],'blob_hash':asset['blob_hash']}],
                 'options':{'action':'detect','device':'cpu'}}
        response=client.post(base+'/jobs',headers=LOCAL,json=command)
        assert response.status_code==202,response.text
        identifier=response.json()['job_id']
        assert client.post(base+'/jobs',headers=LOCAL,json=command).json()['job_id']==identifier
        assert client.post(base+'/jobs',headers=LOCAL,json={**command,'options':{'action':'search'}}).status_code==409
        job=wait(client,base,identifier)
        assert job['status']=='succeeded',job
        assert job['result']['images'][0]['faces']
        assert 'feature' not in job['result']['images'][0]['faces'][0]
        jobs=client.get(f'{PREFIX}/workspaces/{ws}/jobs').json()
        assert any(item['platform_job_id']==identifier and item['status']=='succeeded' for item in jobs['items']),jobs
        reference={'request_id':'referenceOne','job_id':identifier,'face_index':0,'input_index':0,'label':'官方工程样例'}
        response=client.post(base+'/face-references',headers=LOCAL,json=reference)
        assert response.status_code==201,response.text
        assert 'feature' not in response.json()
        assert client.post(base+'/face-references',headers=LOCAL,json=reference).json()==response.json()
        search=client.post(base+'/jobs',headers=LOCAL,json={**command,'request_id':'faceRealSearch','options':{'action':'search'}})
        assert search.status_code==202,search.text
        searched=wait(client,base,search.json()['job_id'])
        assert searched['status']=='succeeded',searched
        assert searched['result']['candidates'][0]['score']>.99
        other=workspace(client)
        assert client.get(f'{PREFIX}/workspaces/{other}/multimodal/jobs/{identifier}').status_code==404
        assert client.get(f'{PREFIX}/workspaces/{other}/multimodal/face-references').json()=={'items':[]}
        review={'request_id':'reviewRealFace','expected_revision':1,'actor':'工程验证人','assessment':'inconclusive','note':'工程样例，只验证流程，不作为身份或业务精度结论。','edits':[]}
        response=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json=review)
        assert response.status_code==200,response.text
        assert response.json()['revision']==2
        assert client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json=review).json()==response.json()
        assert client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json={**review,'request_id':'staleReview'}).status_code==409
        exported=client.get(base+'/jobs/'+identifier+'/download')
        assert exported.status_code==200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
            result=json.loads(bundle.read('result.json'))
            assert result['revision']==2 and result['inputs'][0]['blob_hash']==hashlib.sha256(raw).hexdigest()
            assert 'feature' not in result['result']['images'][0]['faces'][0]
        assert client.post(base+'/jobs/'+identifier+'/collect',headers=LOCAL).status_code==409
        confirmed=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json={**review,
            'request_id':'confirmRealFace','expected_revision':2,'assessment':'confirmed',
            'note':'核准工程结果版本与来源；不把工程样例当作业务身份结论。'})
        assert confirmed.status_code==200,confirmed.text
        collected=client.post(base+'/jobs/'+identifier+'/collect',headers=LOCAL)
        assert collected.status_code==200,collected.text
        bundle=collected.json();assert bundle['kind']=='manual_analysis' and bundle['files']
        assert client.post(base+'/jobs/'+identifier+'/collect',headers=LOCAL).json()==bundle
        artifacts=f'{PREFIX}/workspaces/{ws}/artifacts'
        listing=client.get(artifacts).json()
        assert any(item['bundle_id']==bundle['bundle_id'] for item in listing['items'])
        original=client.get(artifacts+'/'+bundle['bundle_id']+'/files/result.json')
        assert original.status_code==200,original.text
        assert hashlib.sha256(original.content).hexdigest()==next(f['blob_hash'] for f in bundle['files'] if f['name']=='result.json')
        actor={'id':'manualTester','display_name':'工程验证人','source':'local'}
        created=client.post(f'{PREFIX}/workspaces/{ws}/audit-projects',headers=LOCAL,json={
            'schema_version':'1.0','request_id':'manualAuditProject','actor':actor,
            'project':{'title':'多模态工程复核','audit_object':'公开样例','period_start':'2026-01-01','period_end':'2026-10-13','objective':'验证固定成果与原件定位'}})
        assert created.status_code==201,created.text
        audit=f"{PREFIX}/audit-projects/{created.json()['project_id']}"
        def audit_command(request_id,**fields):return {'schema_version':'1.0','request_id':request_id,'actor':actor,'expected_revision':client.get(audit).json()['revision'],**fields}
        procedure=client.post(audit+'/procedures',headers=LOCAL,json=audit_command('manualAuditProcedure',
            procedure={'title':'人工核对','basis':'公开工程样例','scope':'一张照片','note':'','conclusion':''},batches=[]))
        assert procedure.status_code==201,procedure.text
        finding=client.post(audit+'/findings',headers=LOCAL,json=audit_command('manualAuditFinding',procedure_id=procedure.json()['entity_id'],
            finding={'title':'工程样例核查','priority':'normal','status':'draft','explanation':'仅验证流程','conclusion':''},evidence=[{'manual_bundle_id':bundle['bundle_id']}]))
        assert finding.status_code==201,finding.text
        result=client.get(audit+'/findings/'+finding.json()['entity_id']).json()
        assert result['evidence_refs'][0]['scenario']=='direct_tool',result
        assert any(source['kind']=='region' for source in result['evidence_refs'][0]['sources'])
        assert client.post(audit+'/findings',headers=LOCAL,json={**audit_command('wrongManualProject',procedure_id=procedure.json()['entity_id'],
            finding={'title':'越界验证','priority':'normal','status':'draft','explanation':'','conclusion':''}),
            'evidence':[{'manual_bundle_id':'bMissing'}]}).status_code in {404,409}
        path=app.state.multimodal_service.output(ws,identifier,'preview.png')[0]
        saved=path.read_bytes();path.write_bytes(b'changed')
        assert client.get(base+'/jobs/'+identifier+'/files/preview.png').status_code==409
        path.write_bytes(saved)
        assert client.post(base+'/jobs',headers=LOCAL,json={**command,'request_id':'cudaUntested','options':{'action':'detect','device':'cuda'}}).status_code==409
        assert client.post(base+'/jobs',headers=LOCAL,json={**command,'request_id':'badSource','inputs':[{**command['inputs'][0],'blob_hash':'0'*64}]}).status_code==409


def test_real_seal_correction_to_data_and_original_region(tmp_path,monkeypatch):
    data=tmp_path/'seal-workspace';data.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(data));monkeypatch.setenv('WORKBENCH_AGENT_ENABLED','0')
    monkeypatch.setenv('WORKBENCH_DATA_PYTHON',str(DATA_PYTHON))
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        ws=workspace(client);base=f'{PREFIX}/workspaces/{ws}/multimodal'
        asset=client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads',params={'name':'合成印章工程样例.pdf'},headers=LOCAL,
            content=(FIXTURES/'seal-synthetic.pdf').read_bytes()).json()
        created=client.post(base+'/jobs',headers=LOCAL,json={'request_id':'sealRealAnalyze','kind':'seal',
            'inputs':[{'asset_id':asset['asset_id'],'asset_revision':asset['revision'],'blob_hash':asset['blob_hash']}]})
        assert created.status_code==202,created.text
        identifier=created.json()['job_id'];job=wait(client,base,identifier)
        assert job['status']=='succeeded',job
        seal=job['result']['seals'][0]
        assert seal['source']['kind']=='region' and seal['source']['page_number']==1
        assert job['result']['pages'][0]['preview']=='page-1.png'
        review=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json={'request_id':'sealReview','expected_revision':1,
            'actor':'工程验证人','note':'按合成原件校正，仅验证流程','assessment':'inconclusive',
            'edits':[{'index':0,'subject':'合成审计有限公司','number':'1234567890123'}]})
        assert review.status_code==200,review.text
        assert client.post(base+'/jobs/'+identifier+'/to-data',headers=LOCAL).status_code==409
        confirmed=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json={
            'request_id':'confirmSealFields','expected_revision':2,'actor':'工程验证人',
            'note':'按合成原件核准字段与来源，仅验证工程流程。','assessment':'confirmed',
            'edits':[{'index':0,'subject':'合成审计有限公司','number':'1234567890123'}]})
        assert confirmed.status_code==200,confirmed.text
        response=client.post(base+'/jobs/'+identifier+'/to-data',headers=LOCAL)
        assert response.status_code==200,response.text
        transferred=response.json();replayed=client.post(base+'/jobs/'+identifier+'/to-data',headers=LOCAL).json()
        assert replayed['session_id']==transferred['session_id'] and replayed['job']['id']==transferred['job']['id']
        session=transferred['session_id'];deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            work=client.get(f'{PREFIX}/data/sessions/{session}').json()
            if not any(row['status'] in {'queued','running','cancelling'} for row in work['jobs']):break
            time.sleep(.1)
        assert work['state']['tables']['left']['rows']==len(job['result']['seals']),work
        assert work['jobs'][0]['status']=='succeeded',work
        store=app.state.multimodal_service.provenance_store
        derived=store.rows('SELECT * FROM derived_inputs WHERE asset_id=?',(transferred['asset']['asset_id'],))[0]
        provenance=json.loads((store.root/derived['provenance_path']).read_text('utf-8'))
        assert provenance['provenance']['rows'][0]['source']['asset_id']==asset['asset_id']
        assert provenance['provenance']['review']['edits'][0]['subject']=='合成审计有限公司'


@pytest.mark.parametrize('kind,filename',[('catnet','catnet-upstream-example.jpg'),('audio','audio-upstream-example.wav')])
def test_real_temporal_or_heatmap_review_reopen_export_and_backup(kind,filename,tmp_path,monkeypatch):
    from scripts.backup_workspace import create,restore
    data=tmp_path/'workspace';data.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(data));monkeypatch.setenv('WORKBENCH_AGENT_ENABLED','0')
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        ws=workspace(client);base=f'{PREFIX}/workspaces/{ws}/multimodal'
        raw=(FIXTURES/filename).read_bytes()
        response=client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads',params={'name':filename},headers=LOCAL,content=raw)
        assert response.status_code==201,response.text
        asset=response.json()
        created=client.post(base+'/jobs',headers=LOCAL,json={'request_id':'real'+kind,'kind':kind,
            'inputs':[{'asset_id':asset['asset_id'],'asset_revision':asset['revision'],'blob_hash':asset['blob_hash']}]})
        assert created.status_code==202,created.text
        identifier=created.json()['job_id'];job=wait(client,base,identifier)
        assert job['status']=='succeeded',job
        assert job['result']['network_policy']=='all_outbound_connections_denied'
        review={'request_id':'review'+kind,'expected_revision':1,'actor':'工程验证人','assessment':'inconclusive',
            'note':'公开样例，仅验证真实处理与来源链','edits':[],'regions':[]}
        if kind=='audio':
            assert job['result']['segments'] and job['result']['speaker_segments']
            segment=job['result']['segments'][0]
            assert segment['source']['kind']=='media_segment' and segment['source']['start_ms']==segment['start_ms']
            assert 0<=segment['start_ms']<segment['end_ms']<=job['result']['audio']['duration_ms']
            review['edits']=[{'index':0,'text':'工程验证修订文本','speaker':'人工标注说话人'}]
        else:
            assert {'heatmap.png','heatmap-probabilities.npy'}<=job['result']['output_files'].keys()
            assert job['result']['processing']['jpeg_recompressed'] is False
            review['regions']=[{'box':[.1,.2,.4,.5],'note':'工程区域标注'}]
        saved=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json=review)
        assert saved.status_code==200,saved.text
        assert client.post(base+'/jobs/'+identifier+'/collect',headers=LOCAL).status_code==409
        saved=client.post(base+'/jobs/'+identifier+'/review',headers=LOCAL,json={**review,
            'request_id':'confirm'+kind,'expected_revision':2,'assessment':'confirmed',
            'note':'核准工程结果版本与原件定位，不核准自动业务结论。'})
        assert saved.status_code==200,saved.text
        response=client.get(base+'/jobs/'+identifier+'/download')
        assert response.status_code==200,response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            value=json.loads(archive.read('result.json'))
            if kind=='audio':
                assert {'transcript.txt','transcript.srt','transcript.vtt','asr-original.json','playback.wav'}<=set(archive.namelist())
                assert '工程验证修订文本' in archive.read('transcript.srt').decode()
                assert value['result']['segments'][0]['speaker']=='人工标注说话人'
                assert '工程验证修订文本' not in archive.read('asr-original.json').decode()
            else:assert value['review']['regions'][0]['source']['kind']=='region'
        collected=client.post(base+'/jobs/'+identifier+'/collect',headers=LOCAL)
        assert collected.status_code==200,collected.text
        bundle=collected.json()
        artifact=app.state.artifact_center.store.get(ws,bundle['files'][0]['artifact_id'])
        assert any(ref.kind==('media_segment' if kind=='audio' else 'region') for ref in artifact.source_refs)
    archive=tmp_path/'backup';restored=tmp_path/'restored'
    manifest=create(data,archive)
    assert 'product-reader-requirements.json' in manifest['files']
    assert any(name.startswith('manual-analysis/') for name in manifest['files'])
    restore(archive,restored)
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(restored))
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        reopened=client.get(base+'/jobs/'+identifier)
        assert reopened.status_code==200,reopened.text
        assert reopened.json()['review']==saved.json()['review']
        original=client.get(f'{PREFIX}/workspaces/{ws}/assets/{asset["asset_id"]}/content?revision=1')
        assert original.content==raw
