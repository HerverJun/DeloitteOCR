"""Real classical worker and artifact publication identify the derived toolbox."""
import hashlib
import json
from pathlib import Path
from fastapi.testclient import TestClient
from workbench_platform.app import app
from workbench_platform.forensics.service import SOURCE_ROOT,source_identity
from workbench_platform.batches.store import fingerprint
from tests.integration.test_image_forensics import png,wait
from tests.integration.test_processing_batches import LOCAL,PREFIX,workspace


def test_real_classic_report_and_artifacts_bind_derived_011(tmp_path,monkeypatch):
    data=tmp_path/'workspace';data.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(data))
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        ws=workspace(client);raw=png()
        response=client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads',headers=LOCAL,params={'name':'派生版本工程样例.png'},content=raw)
        assert response.status_code==201,response.text
        asset=response.json();base=f'{PREFIX}/workspaces/{ws}/forensics'
        tools=['metadata','noise','frequency','jpeg','jpeg_grid','copy_move']
        response=client.post(base,headers=LOCAL,json={'schema_version':'1.0','request_id':'derivedVersionReal',
            'asset_id':asset['asset_id'],'asset_revision':asset['revision'],'blob_hash':asset['blob_hash'],'tools':tools})
        assert response.status_code==202,response.text
        identifier=response.json()['inspection_id'];item=wait(client,base,identifier)
        assert item['status']=='succeeded',item
        report=item['report']
        expected=json.loads((SOURCE_ROOT/'modules/image_forensics/source-manifest.json').read_text('utf-8'))
        assert report['toolbox_source']==expected
        assert len(report['toolbox_source']['files'])==6 and len(report['toolbox_source']['derived']['files'])==7
        assert report['runtime']['toolbox']=={'path':str((SOURCE_ROOT/'modules/image_forensics/forensic_toolbox').resolve()),'version':'0.1.1'}
        assert all(row['version']=='0.1.1' for row in report['evidence'])
        execution=report['toolbox_execution']
        assert execution['source_identity_sha256']==fingerprint(source_identity(expected))
        response=client.post(base+'/'+identifier+'/collect',headers=LOCAL)
        assert response.status_code==200,response.text
        bundle=response.json()
        for record in bundle['files']:
            artifact=app.state.artifact_center.store.get(ws,record['artifact_id'])
            assert artifact.application_version=='forensic-toolbox-0.1.1'
            assert artifact.rule_or_model_version==fingerprint({'tools':tools,'toolbox_source_identity':execution['source_identity_sha256']})
        assert client.post(base+'/'+identifier+'/collect',headers=LOCAL).json()==bundle
        fixed=client.get(base+'/'+identifier+'/files/report.json')
        assert hashlib.sha256(fixed.content).hexdigest()==item['report_hash']
        assert client.get(f'{PREFIX}/workspaces/{ws}/assets/{asset["asset_id"]}/content?revision=1').content==raw
