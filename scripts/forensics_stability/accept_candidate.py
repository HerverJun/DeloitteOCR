"""Isolated CPU acceptance over a sealed candidate, never development app code.

Run with the candidate's own program/runtimes/platform/python.exe -I -B.
--output must be a new directory within the current F-drive stability task.
This harness uses engineering-only public/synthetic inputs and does not measure
business accuracy, an independent device, or physical network isolation.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import traceback
import zipfile

TASK_ROOT=Path('F:/OCR/output/forensics-stability-20261014')
FIXTURES=Path('F:/OCR/output/multimodal-manual-upgrade-20261013-001/fixtures')
ROTATION_INPUT=TASK_ROOT/'evaluation/dataset-001/base-05/original.jpg'
PREFIX='/api/platform/v1'
HEADERS={'host':'127.0.0.1:8766','origin':'http://127.0.0.1:8766'}
TOOLS=['metadata','noise','frequency','jpeg','jpeg_grid','copy_move']


def local_f(value):
    requested=Path(value).absolute()
    if requested.drive.upper()!='F:' or requested.resolve().drive.upper()!='F:':
        raise ValueError('Acceptance paths must remain on local F: '+str(requested))
    if any(parent.is_symlink() or parent.is_junction() for parent in (requested,*requested.parents)):
        raise ValueError('Acceptance paths cannot be redirected: '+str(requested))
    return requested.resolve()


def digest_bytes(value):return hashlib.sha256(value).hexdigest()


def digest_file(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def checked(response, statuses=(200,201,202)):
    if response.status_code not in statuses:
        raise AssertionError(f'HTTP {response.status_code}: {response.text[:4000]}')
    return response.json()


def wait(client,url,active=('queued','running','cancelling'),timeout=1200):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=checked(client.get(url))
        if value['status'] not in active:return value
        time.sleep(.1)
    raise TimeoutError('Acceptance task did not finish: '+url)


def png_size(content):
    if content[:8]!=b'\x89PNG\r\n\x1a\n' or content[12:16]!=b'IHDR':
        raise AssertionError('Expected PNG with IHDR')
    return list(struct.unpack('>II',content[16:24]))


def npy_shape(content):
    if content[:6]!=b'\x93NUMPY':raise AssertionError('Expected raw NPY')
    major=content[6]
    offset=10 if major==1 else 12
    length=struct.unpack('<H' if major==1 else '<I',content[8:offset])[0]
    header=ast.literal_eval(content[offset:offset+length].decode('latin1' if major<3 else 'utf-8'))
    if header['fortran_order'] or header['descr'] not in ('<f4','=f4'):
        raise AssertionError('Expected row-major float32 probability map')
    return list(header['shape'])


def map_box(box,matrix,source_size,target_size):
    points=[]
    for x,y in ((box[0],box[1]),(box[0],box[3]),(box[2],box[1]),(box[2],box[3])):
        px,py=x*source_size[0],y*source_size[1]
        points.append(((matrix[0][0]*px+matrix[0][1]*py+matrix[0][2])/target_size[0],
                       (matrix[1][0]*px+matrix[1][1]*py+matrix[1][2])/target_size[1]))
    return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]


def same_box(left,right):
    assert len(left)==len(right)==4
    assert all(math.isfinite(value) for value in [*left,*right])
    assert max(abs(a-b) for a,b in zip(left,right))<1e-12,(left,right)


def verify_manual_zip(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names=archive.namelist()
        assert len(names)==len(set(names)), 'Duplicate ZIP member'
        manifest=json.loads(archive.read('manifest.json'))
        assert set(names)==set(manifest)|{'manifest.json'}
        for name,identity in manifest.items():
            assert Path(name).name==name and ':' not in name
            raw=archive.read(name)
            assert len(raw)==identity['bytes'] and digest_bytes(raw)==identity['sha256'],name
        return json.loads(archive.read('result.json')),manifest


def setup_environment(candidate,output,native):
    data=output/'workspace'
    for key in list(os.environ):
        if key.startswith('WORKBENCH_') or key.upper() in {'PYTHONPATH','PYTHONHOME','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'}:
            os.environ.pop(key,None)
    names=('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','HF_HOME','TORCH_HOME','MODELSCOPE_CACHE',
           'XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','MPLCONFIGDIR','NUMBA_CACHE_DIR',
           'PADDLE_PDX_CACHE_HOME','PADDLEOCR_HOME','PIP_CACHE_DIR','npm_config_cache','CUDA_CACHE_PATH',
           'TORCHINDUCTOR_CACHE_DIR','TRITON_CACHE_DIR','PYTHONPYCACHEPREFIX','HF_DATASETS_CACHE',
           'HUGGINGFACE_HUB_CACHE','JUPYTER_CONFIG_DIR','JUPYTER_DATA_DIR')
    for name in names:
        directory=output/'runtime'/name.lower();directory.mkdir(parents=True)
        os.environ[name]=str(directory)
    for name in ('TEMP','TMP','TMPDIR','CLEAR_PADDLE_TEMP','CLEAR_NATIVE_TEMP_ROOT'):
        os.environ[name]=str(native)
    os.environ.update(WORKBENCH_DATA_DIR=str(data),WORKBENCH_PROGRAM_ROOT=str(candidate/'program'),
        WORKBENCH_AGENT_LAYER=str(candidate/'dependencies/agent'),WORKBENCH_AGENT_ENABLED='0',
        WORKBENCH_DATA_PYTHON=str(candidate/'program/runtimes/data/python.exe'),
        WORKBENCH_FORENSICS_PYTHON=str(candidate/'program/runtimes/forensics/python.exe'),
        WORKBENCH_PORTAL_DIST=str(candidate/'platform/apps/portal/dist'),
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',DO_NOT_TRACK='1',
        PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',CUDA_VISIBLE_DEVICES='',
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')
    return data


def verify_import_sources(candidate,names):
    locations={}
    for name in names:
        module=sys.modules[name]
        path=Path(module.__file__).resolve()
        assert path.is_relative_to(candidate),'Application import escaped candidate: '+str(path)
        locations[name]=str(path)
    return locations


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,default=FIXTURES)
    parser.add_argument('--rotation-input',type=Path,default=ROTATION_INPUT)
    args=parser.parse_args()
    candidate,output,fixtures,rotation=map(local_f,(args.candidate,args.output,args.fixtures,args.rotation_input))
    if not output.is_relative_to(TASK_ROOT.resolve()) or output.exists():
        raise ValueError('--output must be a new directory beneath the stability task root')
    if candidate.is_relative_to(TASK_ROOT/'worktrees') or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError('Use an independent candidate and distinct acceptance output')
    expected_runtime=(candidate/'program/runtimes/platform/python.exe').resolve()
    if Path(sys.executable).resolve()!=expected_runtime:
        raise ValueError('Run acceptance with the candidate platform runtime: '+str(expected_runtime))
    for path in (candidate/'platform/src/workbench_platform/app.py',candidate/'platform/config/multimodal.json',rotation):
        if not path.is_file():raise FileNotFoundError(path)
    output.mkdir(parents=True)
    native=local_f(TASK_ROOT/'temp'/('candidate-accept-'+hashlib.sha256(str(output).encode()).hexdigest()[:20]))
    if native.exists():raise ValueError('Use fresh acceptance native cache')
    native.mkdir(parents=True)
    data=setup_environment(candidate,output,native)
    (output/'downloads').mkdir()
    report={'format':'clear-forensics-candidate-acceptance-v1','passed':False,'candidate':str(candidate),
        'workspace':str(data),'output':str(output),'runtime':str(expected_runtime),'empty_cache':True,
        'scope':'Same-computer engineering CPU/API/geometry acceptance; no business accuracy, GPU benchmark or independent physical-disconnect claim',
        'stages':[],'write_roots':[str(output),str(native)],'fixtures':{},'blocked_actions':[]}
    report_path=output/'report.json'
    helper=Path(__file__).with_name('candidate_guard.py').resolve()
    try:
        spec=importlib.util.spec_from_file_location('_candidate_acceptance_guard',helper)
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        guard_config={'candidate':str(candidate),'write_roots':[str(output),str(native)],
            'forbidden_roots':['F:/OCR/output/clear-automation-dev-20261002','F:/OCR/output/clear-workbench-preview-20261002',
                'F:/OCR/products/Clear',str(TASK_ROOT/'worktrees')],
            'harness_files':[str(Path(__file__).resolve()),str(helper)],
            'violation_log':str(output/'guard-violations.jsonl')}
        os.environ[guard.ENV_KEY]=json.dumps(guard_config)
        report['blocked_actions']=guard.install(guard_config)
        sys.path[:0]=[str(candidate/'platform'),str(candidate/'platform/src')]
        from scripts.agent_native_preview import configure
        from fastapi.testclient import TestClient
        from workbench_platform.app import app
        from scripts.backup_workspace import create,restore
        report['import_sources']=verify_import_sources(candidate,('scripts.agent_native_preview','workbench_platform.app',
            'workbench_platform.forensics.service','workbench_platform.multimodal.service','scripts.backup_workspace'))
        configure(candidate/'platform',candidate/'program',data)
        originals={}
        snapshots={}
        classic_snapshot=None
        with TestClient(app,base_url='http://127.0.0.1:8766') as client:
            ws=checked(client.post(PREFIX+'/workspaces',headers=HEADERS,json={'schema_version':'1.0','name':'取证稳定性候选工程验收'}))['workspace_id']
            report['workspace_id']=ws
            base=f'{PREFIX}/workspaces/{ws}/multimodal'
            caps=checked(client.get(PREFIX+'/multimodal/capabilities'))
            assert {row['kind'] for row in caps['items']}=={'face','catnet','audio','seal'}
            assert all(row['cpu_ready'] and not row['gpu_ready'] for row in caps['items']),caps
            report['capabilities']=[{key:row[key] for key in ('kind','cpu_ready','gpu_ready')} for row in caps['items']]
            (output/'capabilities.json').write_text(json.dumps(caps,ensure_ascii=False,indent=2),encoding='utf-8')

            def upload(path,name=None):
                raw=path.read_bytes()
                name=name or path.name
                value=checked(client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads',headers=HEADERS,params={'name':name},content=raw))
                asset={key:value[key] for key in ('asset_id','blob_hash')}
                asset['asset_revision']=value['revision']
                assert asset['blob_hash']==digest_bytes(raw)
                originals[asset['asset_id']]={'asset':asset,'raw':raw,'path':str(path)}
                report['fixtures'][str(path)]={'sha256':digest_bytes(raw),'bytes':len(raw),'classification':'engineering_regression_only'}
                return asset

            for kind,path in [('face',fixtures/'face-upstream-demo.jpg'),('catnet',rotation),
                              ('audio',fixtures/'audio-upstream-example.wav'),('seal',fixtures/'seal-synthetic.pdf')]:
                asset=upload(path,'EXIF6工程诊断.jpg' if kind=='catnet' else None)
                command={'request_id':'accept'+kind,'kind':kind,'inputs':[asset],
                    'options':{'device':'cpu','action':'detect' if kind=='face' else 'analyze'}}
                started=time.monotonic()
                created=checked(client.post(base+'/jobs',headers=HEADERS,json=command))
                identifier=created['job_id'];url=base+'/jobs/'+identifier
                job=wait(client,url)
                assert job['status']=='succeeded' and job['actual_device']=='cpu',job
                result=job['result'];assert result['network_policy']=='all_outbound_connections_denied'
                review={'request_id':'review'+kind,'expected_revision':job['revision'],'actor':'候选工程验收',
                    'assessment':'inconclusive','note':'既有公开或自有合成工程样例；仅验证处理、坐标和来源链，不作为业务准确率结论。',
                    'edits':[],'regions':[]}
                if kind=='face':assert result['images'][0]['faces']
                elif kind=='audio':
                    assert result['segments']
                    review['edits']=[{'row_id':result['segments'][0]['row_id'],'text':'候选短音频工程校订','speaker':'人工匿名标注'}]
                elif kind=='seal':
                    assert result['seals']
                    review['edits']=[{'row_id':result['seals'][0]['row_id'],'subject':'合成审计有限公司'}]
                else:
                    geometry=result['processing']['geometry']
                    assert geometry['coordinate_version']=='exif-pixel-edges-v1' and geometry['exif_orientation']==6,geometry
                    assert geometry['persisted_region_coordinate_system']=='stored_pixels'
                    stored=[result['width'],result['height']]
                    assert geometry['stored_size']==stored and geometry['display_size']==stored[::-1]
                    width,height=stored
                    assert geometry['stored_to_display']==[[0,-1,height],[1,0,0],[0,0,1]]
                    assert geometry['display_to_stored']==[[0,1,0],[-1,0,height],[0,0,1]]
                    for name,identity in result['output_files'].items():
                        response=client.get(url+'/files/'+name)
                        assert response.status_code==200
                        content=response.content
                        assert len(content)==identity['bytes'] and digest_bytes(content)==identity['sha256'],name
                        if name in ('preview-display.png','heatmap-display.png'):assert png_size(content)==stored[::-1]
                        elif name in ('preview.png','heatmap.png'):assert png_size(content)==stored
                        elif name=='heatmap-probabilities.npy':assert npy_shape(content)==stored[::-1]
                    display_box=[.15,.2,.65,.7]
                    stored_box=map_box(display_box,geometry['display_to_stored'],geometry['display_size'],stored)
                    same_box(stored_box,[.2,.35,.7,.85])
                    same_box(map_box(stored_box,geometry['stored_to_display'],stored,geometry['display_size']),display_box)
                    review['regions']=[{'box':stored_box,'note':'EXIF6显示框逆映射为原始JPEG存储框'}]
                    catnet_asset=asset
                    report['exif6']={'stored_size':stored,'display_size':stored[::-1],
                        'display_box':display_box,'stored_box':stored_box,'geometry':geometry,
                        'scope':'API-level equivalent of viewer inverse mapping; browser pointer interaction is separately verified'}
                saved=checked(client.post(url+'/review',headers=HEADERS,json=review))
                assert saved['revision']==job['revision']+1
                reopened=checked(client.get(url));assert reopened['review']==saved['review']
                exported=client.get(url+'/download');assert exported.status_code==200
                archive_item,manifest=verify_manual_zip(exported.content)
                assert archive_item['review']==saved['review']
                if kind=='catnet':
                    region=archive_item['review']['regions'][0]
                    same_box(region['box'],stored_box);same_box(region['source']['box'],stored_box)
                    assert region['source']['coordinate_space']=='normalized'
                    assert region['source']['image_version_id']=='img'+asset['blob_hash'][:32]+'p1'
                    assert region['source']['transform_revision']==1
                    assert region['source']['blob_hash']==asset['blob_hash']
                (output/'downloads'/f'{kind}-reviewed.zip').write_bytes(exported.content)
                bundle=checked(client.post(url+'/collect',headers=HEADERS,json={}))
                repeated=checked(client.post(url+'/collect',headers=HEADERS,json={}))
                assert repeated['bundle_id']==bundle['bundle_id'] and bundle['files']
                if kind=='catnet':
                    artifact=app.state.artifact_center.store.get(ws,bundle['files'][0]['artifact_id'])
                    regions=[source for source in artifact.source_refs if source.kind=='region']
                    assert regions,'Collected CAT artifact lost region provenance'
                    same_box(regions[0].box,stored_box)
                    assert regions[0].blob_hash==asset['blob_hash'] and regions[0].coordinate_space=='normalized'
                    report['exif6']['artifact_region_source_verified']=True
                snapshots[identifier]={'review':saved['review'],'result_hash':saved['result_hash'],'bundle_files':bundle['files']}
                report['stages'].append({'kind':kind,'job_id':identifier,'outcome':result['outcome'],
                    'seconds':round(time.monotonic()-started,3),'bundle_id':bundle['bundle_id'],
                    'export_members':len(manifest),'zip_sha256':digest_bytes(exported.content)})
                print('accepted',kind,report['stages'][-1]['seconds'],flush=True)

            # Exercise actual running cancellation, owned-process drain and explicit retry.
            created=checked(client.post(base+'/jobs',headers=HEADERS,json={'request_id':'acceptCancelRetry','kind':'catnet',
                'inputs':[catnet_asset],'options':{'device':'cpu'}}))
            identifier=created['job_id'];url=base+'/jobs/'+identifier
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                running=checked(client.get(url))
                if running['status']=='running' and identifier in app.state.multimodal_service.processes:break
                if running['status'] not in ('queued','running'):raise AssertionError(running)
                time.sleep(.01)
            assert running['status']=='running' and identifier in app.state.multimodal_service.processes,running
            cancelled_process=app.state.multimodal_service.processes[identifier]
            checked(client.post(url+'/control/cancel',headers=HEADERS,json={}))
            cancelled=wait(client,url);assert cancelled['status']=='cancelled',cancelled
            assert cancelled_process.poll() is not None,'Cancelled owned worker is still active'
            checked(client.post(url+'/control/retry',headers=HEADERS,json={}))
            retried=wait(client,url)
            assert retried['status']=='succeeded' and retried['generation']==2 and retried['actual_device']=='cpu',retried
            assert app.state.multimodal_service.coordinator.snapshot().owner is None
            deadline=time.monotonic()+5
            while app.state.multimodal_service.processes and time.monotonic()<deadline:time.sleep(.02)
            assert not app.state.multimodal_service.processes
            report['running_cancel_retry']={'passed':True,'job_id':identifier,'generation':2,'device':'cpu',
                'cancelled_worker_pid':cancelled_process.pid,'cancelled_worker_exit_confirmed':True}

            classic=f'{PREFIX}/workspaces/{ws}/forensics'
            created=checked(client.post(classic,headers=HEADERS,json={'schema_version':'1.0','request_id':'acceptClassic',
                **catnet_asset,'tools':TOOLS}))
            classic_url=classic+'/'+created['inspection_id']
            inspection=wait(client,classic_url,active=('queued','running'))
            assert inspection['status']=='succeeded',inspection
            evidence=inspection['report']['evidence']
            assert {row['tool'] for row in evidence}==set(TOOLS) and all(row['status']=='completed' for row in evidence),evidence
            assert inspection['report']['verdict'] is None
            runtime_toolbox=inspection['report']['runtime']['toolbox']
            assert runtime_toolbox=={'version':'0.1.1','path':str((candidate/'platform/modules/image_forensics/forensic_toolbox').resolve())}
            expected_source=json.loads((candidate/'platform/modules/image_forensics/source-manifest.json').read_text('utf-8'))
            assert inspection['report']['toolbox_source']==expected_source
            classic_execution=inspection['report']['toolbox_execution']
            assert classic_execution['kind']=='derived' and classic_execution['version']=='0.1.1'
            geometry=inspection['report']['input']['geometry']
            assert geometry['exif_orientation']==6 and geometry['display_size']==report['exif6']['display_size']
            for record in inspection['files']:
                content=client.get(classic_url+'/files/'+record['name']).content
                assert len(content)==record['size_bytes'] and digest_bytes(content)==record['sha256']
                if record['name'].endswith('.png'):assert png_size(content)==geometry['display_size']
            for row in evidence:
                if row['map_path']:
                    assert row['metrics']['map_coordinate_system']=='exif_transposed_pixels'
                    assert row['metrics']['coordinate_version']=='exif-pixel-edges-v1'
            inspected=checked(client.post(classic_url+'/review',headers=HEADERS,json={'schema_version':'1.0',
                'request_id':'reviewClassic','expected_revision':inspection['revision'],
                'actor':{'source':'local','id':'candidateReviewer','display_name':'候选工程验收'},
                'assessment':'inconclusive','note':'旋转合成JPEG，仅检查六工具、地图坐标、复核和导出，不作为真实篡改判断。'}))
            download=client.get(classic_url+'/download');assert download.status_code==200
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                manifest=json.loads(archive.read('download-manifest.json'))
                assert manifest['report_hash']==inspection['report_hash']
                for record in manifest['files']:
                    raw=archive.read(record['name'])
                    assert len(raw)==record['size_bytes'] and digest_bytes(raw)==record['sha256']
            (output/'downloads/classic-reviewed.zip').write_bytes(download.content)
            bundle=checked(client.post(classic_url+'/collect',headers=HEADERS,json={}))
            assert bundle['files']
            classic_rule=digest_bytes(json.dumps({'tools':TOOLS,'toolbox_source_identity':classic_execution['source_identity_sha256']},
                ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8'))
            for record in bundle['files']:
                artifact=app.state.artifact_center.store.get(ws,record['artifact_id'])
                assert artifact.application_version=='forensic-toolbox-0.1.1' and artifact.rule_or_model_version==classic_rule
            classic_snapshot={'inspection_id':inspection['inspection_id'],'review':inspected['review'],'report_hash':inspection['report_hash'],
                'bundle_files':bundle['files'],'rule_or_model_version':classic_rule}
            report['stages'].append({'kind':'classic_forensics','inspection_id':inspection['inspection_id'],
                'tools':6,'bundle_id':bundle['bundle_id'],'zip_sha256':digest_bytes(download.content),
                'application_version':'forensic-toolbox-0.1.1','source_identity_sha256':classic_execution['source_identity_sha256']})
            for saved in originals.values():
                asset=saved['asset']
                response=client.get(f'{PREFIX}/workspaces/{ws}/assets/{asset["asset_id"]}/content?revision={asset["asset_revision"]}')
                assert response.status_code==200 and response.content==saved['raw']
                assert digest_file(saved['path'])==asset['blob_hash']
            report['originals_unchanged']=True
        # A completed TestClient lifespan has stopped all platform services.
        backup=output/'cold-backup';restored=output/'restored-workspace'
        manifest=create(data,backup);restore(backup,restored)
        configure(candidate/'platform',candidate/'program',restored)
        with TestClient(app,base_url='http://127.0.0.1:8766') as client:
            for identifier,snapshot in snapshots.items():
                url=base+'/jobs/'+identifier
                reopened=checked(client.get(url))
                assert reopened['review']==snapshot['review'] and reopened['result_hash']==snapshot['result_hash']
                response=client.get(url+'/download');assert response.status_code==200
                archive_item,_=verify_manual_zip(response.content)
                assert archive_item['review']==snapshot['review']
                for bundle_file in snapshot['bundle_files']:
                    artifact=app.state.artifact_center.store.get(ws,bundle_file['artifact_id'])
                    assert artifact.blob_hash==bundle_file['blob_hash'] and artifact.size_bytes==bundle_file['size_bytes']
            url=f'{PREFIX}/workspaces/{ws}/forensics/'+classic_snapshot['inspection_id']
            reopened=checked(client.get(url))
            assert reopened['review']==classic_snapshot['review'] and reopened['report_hash']==classic_snapshot['report_hash']
            for record in classic_snapshot['bundle_files']:
                artifact=app.state.artifact_center.store.get(ws,record['artifact_id'])
                assert artifact.application_version=='forensic-toolbox-0.1.1'
                assert artifact.rule_or_model_version==classic_snapshot['rule_or_model_version']
            for saved in originals.values():
                asset=saved['asset']
                response=client.get(f'{PREFIX}/workspaces/{ws}/assets/{asset["asset_id"]}/content?revision={asset["asset_revision"]}')
                assert response.status_code==200 and response.content==saved['raw']
        report['cold_backup_restore']={'passed':True,'backup_files':len(manifest['files']),'restored_workspace':str(restored)}
        report['passed']=True
        assert not report['blocked_actions'],'Unexpected parent guard violation'
        if (output/'guard-violations.jsonl').exists():
            assert not (output/'guard-violations.jsonl').read_text('utf-8').strip(),'Child guard violation'
    except BaseException as error:
        report.update(passed=False,failure=repr(error),traceback=traceback.format_exc())
        raise
    finally:
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'passed':report['passed'],'report':str(report_path),'stages':len(report['stages'])},ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
