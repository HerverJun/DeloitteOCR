"""Verify cold restoration against retained actual native engineering outputs."""
import argparse
import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from fastapi.testclient import TestClient
from scripts.backup_workspace import create,verify,restore
from scripts.agent_native_preview import configure
from workbench_platform.agent.registry import ToolContext
from workbench_platform.application_services.references import verify as verify_ref,asset_allowed,data_reference
from workbench_platform.application_services.native import prepare
from workbench_platform.agent.tools import ocr,classification


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def check(source,output,platform_source,program,verify_existing=False):
    from workbench_platform.supervisor.processes import require_f_path
    source,output,platform_source,program=[require_f_path(p) for p in (source,output,platform_source,program)]
    if output.exists() and not verify_existing:
        raise ValueError('Evidence output must be new')
    sys.path.insert(0,str(platform_source.parent/'classification/src'))
    from image_platform import backup as class_backup
    output.mkdir(parents=True,exist_ok=verify_existing)
    backups=output/'backups';restored=output/'restored'
    manifests={'platform':verify(backups/'platform') if verify_existing else create(source,backups/'platform'),
        'ocr':verify(backups/'ocr') if verify_existing else create(source/'native/ocr',backups/'ocr',kind='ocr')}
    if not verify_existing:
        restore(backups/'platform',restored)
        restore(backups/'ocr',restored/'native/ocr')
    if (source/'native/classification/platform.sqlite3').is_file():
        manifests['classification']=class_backup.verify(backups/'classification') if verify_existing else class_backup.backup(source/'native/classification',backups/'classification')
        if not verify_existing:
            class_backup.restore(backups/'classification',restored/'native/classification')
    for kind,value in manifests.items():
        checker=class_backup.verify if kind=='classification' else verify
        assert checker(backups/kind)==value
    from workbench_platform.app import app
    configure(platform_source,program,restored)
    os.environ['WORKBENCH_AGENT_ENABLED']='1'
    evidence={'source':str(source),'restored':str(restored),'is_real_controller':False,
              'components':{k:{'files':len(v['files']),'manifest_sha256':sha(backups/k/'manifest.json')} for k,v in manifests.items()},
              'references':0,'derived_sources':0,'downloads':0,'native_rebind':[], 'legacy_unpinned_revalidation':[]}
    with TestClient(app,base_url='http://127.0.0.1:8867'):
        services=app.state;agent=services.agent
        for session in agent.store.rows('SELECT id FROM sessions'):
            session=agent.store.session(session['id'])
            runs=agent.store.rows('SELECT * FROM runs WHERE session_id=?',(session['id'],))
            if not runs:
                continue
            run=runs[0]
            ctx=ToolContext(session['workspace_id'],session['id'],run['id'],run['generation'],0,'',tuple(session['selected_assets']),services,agent.store)
            operations=agent.store.rows('SELECT * FROM operations WHERE run_id=? AND result IS NOT NULL',(run['id'],))
            for operation in operations:
                value=json.loads(operation['result'])
                for reference in [*value.get('artifact_refs',[]),*[r for r in value.get('result_refs',[]) if r['application']=='data']]:
                    if reference['application']=='data' and reference['kind'] in {'table','result'} and reference.get('sha256') is None:
                        # Early retained engineering runs preceded content pins.
                        # Revalidate these explicitly, retaining the original
                        # ledger; never weaken the production verifier.
                        reference={**reference,'sha256':data_reference(ctx,reference['parent_id'],reference['kind'],reference['object_id'])['content_sha256']}
                        member='data-workbench/sessions/'+reference['parent_id']+'/views.sqlite3'
                        table,key=('table_views','revision') if reference['kind']=='table' else ('result_views','id')
                        rows='table_rows' if reference['kind']=='table' else 'result_rows'
                        with closing(sqlite3.connect((source/member).as_uri()+'?mode=ro',uri=True)) as before, \
                             closing(sqlite3.connect((restored/member).as_uri()+'?mode=ro',uri=True)) as after:
                            for query in (f'SELECT * FROM {table} WHERE {key}=?',f'SELECT * FROM {rows} WHERE {key}=? ORDER BY ordinal'):
                                assert before.execute(query,(reference['object_id'],)).fetchall()==after.execute(query,(reference['object_id'],)).fetchall()
                        evidence['legacy_unpinned_revalidation'].append(reference)
                    verify_ref(reference,ctx);evidence['references']+=1
                for reference in value.get('job_refs',[]):
                    family=reference['application']
                    if family not in {'ocr','classification'} or reference.get('coverage',{}).get('kind')=='pdf_pages':
                        continue
                    service=services.ocr_handoff if family=='ocr' else services.classification_handoff
                    table='ocr_handoff' if family=='ocr' else 'classification_handoff'
                    with closing(sqlite3.connect(service.db_path)) as db:
                        row=db.execute('SELECT link_id FROM '+table+' WHERE handoff_id=?',(reference['parent_id'],)).fetchone()
                    if not row:
                        raise ValueError('Original native handoff missing')
                    prepare(replace(ctx,operation_id='cold_'+family),family,row[0])
                    handler=ocr.settle if family=='ocr' else classification.settle
                    result=handler(reference,ctx)
                    assert result and result['status']=='success',result
                    for ref in result['result_refs']:
                        verify_ref(ref,ctx);evidence['references']+=1
                    evidence['native_rebind'].append({'family':family,'original_job_id':reference['object_id'],'original_parent_id':reference['parent_id'],'status':result['status']})
            for row in agent.store.rows('SELECT asset_id FROM derived_inputs WHERE workspace_id=?',(ctx.workspace_id,)):
                assert asset_allowed(ctx,row['asset_id'])
                evidence['derived_sources']+=1
            center=services.artifact_center
            with closing(sqlite3.connect(center.store.db_path)) as db:
                artifacts=db.execute('SELECT artifact_id FROM published_artifact WHERE workspace_id=?',(ctx.workspace_id,)).fetchall()
            for identifier, in artifacts:
                artifact=center.store.get(ctx.workspace_id,identifier)
                with center.store.stage_download(ctx.workspace_id,artifact.artifact_id,expected_revision=artifact.revision,expected_hash=artifact.blob_hash) as path:
                    assert sha(path)==artifact.blob_hash
                    evidence['downloads']+=1
    (output/'restore-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','output','platform_source','program'):
        parser.add_argument('--'+name.replace('_','-'),type=Path,required=True)
    parser.add_argument('--verify-existing',action='store_true')
    check(**vars(parser.parse_args()))
