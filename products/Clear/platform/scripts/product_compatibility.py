"""Read-only product data admission, using schema and record capabilities."""
from pathlib import Path
from contextlib import closing
import argparse, json, sqlite3, sys

READER_FEATURES = ('agent-request-snapshot-v1','formal-direct-evidence-v1','fixed-draft-transfer-v1','manual-multimodal-v1','source-media-segment-v1')
DATA_COMPATIBILITY = {
    'format':'clear-data-compatibility-v1', 'reader_features':list(READER_FEATURES),
    'sqlite': {
        'workspaces.sqlite3': {'user_versions':[0,1,2], 'tables':{},
            'reject_nonempty_unversioned':True, 'ignore_internal_tables':True,
            'require_versioned_tables':True, 'exact_table_versions':[1], 'tables_by_version':{
                '1':{'workspace':['id','name'],
                     'module_project_link':['id','workspace_id','module_id','native_project_id','display_name']},
                '2':{'workspace':['workspace_id','name','revision','created_at','updated_at'],
                     'module_project_link':['link_id','workspace_id','app_id','module_project_id','role','revision']}}},
        'agent/operations.sqlite3': {'user_versions':[0], 'tables':{
            'sessions':['id','workspace_id','request_id'], 'runs':['id','status','generation'],
            'events':['kind','payload'], 'model_requests':['id','state','response']}},
        'audit/audit.sqlite3': {'user_versions':[0,1], 'tables':{
            'projects':['project_id','workspace_id','revision','body'],
            'evidence_snapshots':['evidence_id','project_id','body']}},
        'forensics/forensics.sqlite3': {'user_versions':[0,1], 'reject_nonempty_unversioned':True, 'tables':{
            'inspections':['inspection_id','workspace_id','request_id','payload_hash','body'],
            'review_requests':['workspace_id','request_id','inspection_id','payload_hash','receipt']}},
        'native/ocr/workbench.sqlite3': {'user_versions':list(range(18)), 'tables':{}},
        'native/classification/platform.sqlite3': {'user_versions':list(range(6)), 'tables':{}},
    },
    'records': {'audit_evidence_versions':['1.0'], 'direct_evidence_snapshot_versions':[1,2],
                'request_snapshot_formats':['clear-model-input-v1']},
}


def f_path(value):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('项目路径必须在本机 F 盘')
    for item in (path,*path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError('产品入口不接受链接路径')
    return path


def read_database(path):
    wal=f_path(path.with_name(path.name+'-wal'))
    # Immutable reads avoid creating empty SQLite sidecars for a quiescent
    # WAL-mode file. Pending WAL must use the normal read-only reader so its
    # latest committed migration version is included, never silently ignored.
    options='?mode=ro' if wal.is_file() and wal.stat().st_size else '?mode=ro&immutable=1'
    return sqlite3.connect(path.as_uri()+options,uri=True,timeout=15)


def check(data, descriptor):
    data=f_path(data)
    if descriptor.get('format')!='clear-data-compatibility-v1':
        raise ValueError('候选没有可识别的数据兼容声明')
    marker=data/'product-reader-requirements.json'
    if marker.exists():
        requirements=json.loads(marker.read_text(encoding='utf-8-sig'))
        if requirements.get('format')!='clear-product-reader-requirements-v1':
            raise ValueError('工作区读取能力记录不可识别，原文件保留')
        if set(requirements['reader_features'])-set(descriptor['reader_features']):
            raise ValueError('该候选缺少工作区所需记录读取能力，不能直接降级')
    jobs=data/'jobs.sqlite3'
    if jobs.is_file():
        with closing(read_database(jobs)) as db:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'manual_analysis' in tables:
                required={'job_id','workspace_id','request_id','payload_hash','body'}
                if not required<={row[1] for row in db.execute('PRAGMA table_info(manual_analysis)')}:
                    raise ValueError('多模态任务数据库结构不兼容')
                rows=db.execute('SELECT body FROM manual_analysis').fetchall()
                if rows and 'manual-multimodal-v1' not in descriptor['reader_features']:
                    raise ValueError('该候选缺少多模态任务读取能力，不能直接降级')
                if rows:
                    from packages.contracts.multimodal import ManualAnalysis
                    for row in rows:ManualAnalysis.model_validate_json(row[0])
    checked=[]
    for relative,requirements in descriptor['sqlite'].items():
        path=f_path(data/relative)
        if not path.exists():
            continue
        with closing(read_database(path)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in requirements['user_versions']:
                raise ValueError('数据库迁移版本不兼容: '+relative)
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            unversioned_tables={name for name in tables if not name.startswith('sqlite_')} if requirements.get('ignore_internal_tables') else tables
            if version==0 and requirements.get('reject_nonempty_unversioned') and unversioned_tables:
                raise ValueError('数据库缺少有效迁移版本: '+relative)
            if relative=='audit/audit.sqlite3' and version==0 and tables:
                raise ValueError('正式审计数据库缺少有效迁移版本')
            required_tables=requirements.get('tables_by_version',{}).get(str(version),requirements['tables'])
            for table,required in required_tables.items():
                columns={row[1] for row in db.execute('PRAGMA table_info("'+table+'")')}
                must_check=bool(tables) or version!=0 and requirements.get('require_versioned_tables',False)
                matches=set(required)==columns if version in requirements.get('exact_table_versions',[]) else set(required)<=columns
                if must_check and not matches:
                    raise ValueError('数据库结构不兼容: '+relative+'/'+table)
            if relative=='audit/audit.sqlite3' and 'evidence_snapshots' in tables:
                from pydantic import TypeAdapter
                from packages.contracts.audit import AuditEvidenceSnapshot, AuditFinding
                validator=TypeAdapter(AuditEvidenceSnapshot)
                for (encoded,) in db.execute('SELECT body FROM evidence_snapshots'):
                    value=json.loads(encoded)
                    if value.get('scenario')=='direct_tool':
                        if 'formal-direct-evidence-v1' not in descriptor['reader_features']:
                            raise ValueError('该候选缺少正式直接证据读取能力')
                        if value.get('snapshot_version',1) not in descriptor['records']['direct_evidence_snapshot_versions']:
                            raise ValueError('直接证据记录版本不兼容')
                    elif value.get('audit_version') not in descriptor['records']['audit_evidence_versions']:
                        raise ValueError('正式证据记录版本不兼容')
                    validator.validate_python(value)
                for project_id,workspace_id,revision,encoded in db.execute('SELECT project_id,workspace_id,revision,body FROM projects'):
                    project=json.loads(encoded)
                    if (project.get('project_id'),project.get('workspace_id'),project.get('revision'))!=(project_id,workspace_id,revision):
                        raise ValueError('正式档案身份与数据库记录不一致')
                    for finding_id,item in project.get('findings',{}).items():
                        if any(ref.get('workspace_id')!=workspace_id for ref in item.get('evidence_refs',[])):
                            raise ValueError('正式证据引用不属于档案工作区')
                        if item.get('origin') and 'fixed-draft-transfer-v1' not in descriptor['reader_features']:
                            raise ValueError('该候选缺少固定版本转档读取能力')
                        if any(ref.get('scenario')=='direct_tool' for ref in item.get('evidence_refs',[])) and 'formal-direct-evidence-v1' not in descriptor['reader_features']:
                            raise ValueError('该候选缺少正式直接证据引用读取能力')
                        # Stored entities keep signatures as a role map; the
                        # existing public Finding contract validates their read projection.
                        AuditFinding.model_validate({'schema_version':'1.0','audit_version':'1.0',
                            'finding_id':finding_id,'procedure_id':item.get('procedure_id'),
                            'project_id':project_id,'project_revision':revision,
                            'project_context_revision':project.get('project_context_revision'),
                            'content_revision':item.get('content_revision'),'finding':item.get('finding'),
                            'evidence_refs':item.get('evidence_refs'),'origin':item.get('origin'),
                            'records':list(item.get('records',{}).values()),'records_current':False,
                            'created_at':item.get('created_at'),'updated_at':item.get('updated_at')})
            if relative=='agent/operations.sqlite3' and 'events' in tables:
                for (encoded,) in db.execute("SELECT payload FROM events WHERE kind='model_request'"):
                    snapshot=json.loads(encoded).get('request_snapshot')
                    if snapshot is not None and (not isinstance(snapshot,dict) or
                        snapshot.get('format') not in descriptor['records']['request_snapshot_formats']):
                        raise ValueError('Agent请求快照记录格式不兼容')
            checked.append({'database':relative,'user_version':version,'tables':len(tables)})
    return {'compatible':True,'data':str(data),'reader_features':descriptor['reader_features'],
            'databases':checked,'writes':0}


def active_work(data):
    data=f_path(data);active=[]
    queries=(('agent/operations.sqlite3','runs',"SELECT id,status FROM runs WHERE status IN ('running','waiting_job','waiting_user','needs_verification')"),
             ('native/ocr/workbench.sqlite3','tasks',"SELECT id,status FROM tasks WHERE status IN ('queued','running')"),
             ('native/ocr/workbench.sqlite3','document_stages',"SELECT id,status FROM document_stages WHERE status IN ('queued','running','waiting_gpu')"),
             ('native/classification/platform.sqlite3','jobs',"SELECT id,state FROM jobs WHERE state IN ('queued','running','pausing','cancelling')"),
             ('batches/batches.sqlite3','batches',"SELECT batch_id,json_extract(body,'$.status') FROM batches WHERE json_extract(body,'$.status') IN ('queued','running','waiting_resource','cancelling')"),
             ('forensics/forensics.sqlite3','inspections',"SELECT inspection_id,json_extract(body,'$.status') FROM inspections WHERE json_extract(body,'$.status') IN ('queued','running')"),
             ('jobs.sqlite3','manual_analysis',"SELECT job_id,json_extract(body,'$.status') FROM manual_analysis WHERE json_extract(body,'$.status') IN ('queued','running','cancelling')"),
             ('data-workbench/sessions.sqlite3','jobs',"SELECT id,status FROM jobs WHERE status IN ('queued','running','cancelling')"))
    for relative,table,sql in queries:
        path=f_path(data/relative)
        if path.exists():
            with closing(read_database(path)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    active.extend({'database':relative,'id':row[0],'status':row[1]} for row in db.execute(sql))
    return {'active_work':active}


if __name__=='__main__':
    sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'src')]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True,type=Path)
    parser.add_argument('--candidate',type=Path)
    parser.add_argument('--active',action='store_true')
    args=parser.parse_args()
    if args.active:
        result=active_work(args.data)
    else:
        manifest=json.loads(f_path(args.candidate/'candidate.json').read_text(encoding='utf-8'))
        result=check(args.data,manifest.get('data_compatibility',{}))
    print(json.dumps(result,ensure_ascii=False))
