from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack, contextmanager
import copy
import csv
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile

from packages.contracts.multimodal import Create, Review, Reference, ManualControl, ManualReferenceDelete, ManualDraftTransfer
from packages.contracts.models import SourceLocator
from workbench_platform.audit.service import validated
from workbench_platform.batches.store import encoded, fingerprint, now
from workbench_platform.data_api.service import DataError
from workbench_platform.forensics.service import f_path
from workbench_platform.resources.gpu import GpuCoordinator, ResourceBusy, ReleaseUnconfirmed, StaleOwner
from workbench_platform.resources.recovery import local_path, recover_owned
from .runner import WorkerRunner, worker_environment, PROTOCOL
from .qualification import Qualification, GPU_KINDS
from . import deep_backend, aigc_backend

SOURCE = Path(__file__).resolve().parents[3]
ACTIVE = {'queued','running','cancelling'}
LABELS = {'face':'人脸比对','audio':'音频分析','seal':'印章识别','catnet':'深度图像取证','document_forensics':'文档篡改排查','aigc_image':'整图 AI 生成检测'}
AUDIO_DISK_BUDGET = 4*3600*16000*2*3 + 256*1024*1024
DISK_MARGIN = 64*1024*1024

class MultimodalService:
    def __init__(self, app, root):
        self.app, self.root = app, f_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root.parent/'jobs.sqlite3'
        self.registry = app.state.asset_library[0]
        self.config = json.loads((SOURCE/'config/multimodal.json').read_text('utf-8-sig'))
        configured = Path(os.environ.get('WORKBENCH_PROGRAM_ROOT',self.config['program_root']))
        self.program = f_path(configured if configured.is_absolute() else SOURCE/configured)
        self.models = self.program/'models/multimodal'
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='manual-cpu')
        self.gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='manual-gpu')
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.inflight = {}
        self.processes = {}
        self.resource_hash_cache = {}
        ledger = local_path(self.root.parent/'gpu.sqlite3')
        if os.environ.get('WORKBENCH_GPU_LEDGER') and local_path(os.environ['WORKBENCH_GPU_LEDGER']) != ledger:
            raise DataError('多模态必须使用平台统一 GPU 账本',503)
        self.coordinator = GpuCoordinator(ledger)
        self.ownership_root = local_path(self.root.parent/'gpu-owners')
        self.runner = WorkerRunner(self.coordinator,self.ownership_root)
        self.qualification = Qualification(self.root.parent/self.config.get('gpu',{}).get('qualification_file','gpu-qualification.json'))
        self.safire_qualification = Qualification(self.root.parent/'safire-gpu-qualification.json')
        self.aigc_qualification = Qualification(self.root.parent/'aigc-gpu-qualification.json')
        self.launch_id, self.instance_id = 'manual'+uuid.uuid4().hex, 'manual'+uuid.uuid4().hex
        self.gpu_recovery_error = None
        from .artifacts import configure
        configure(app.state.artifact_center)
        with closing(self.db()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS manual_analysis(
                job_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS manual_review(
                workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                body TEXT NOT NULL,PRIMARY KEY(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS manual_face_reference(
                reference_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS manual_operation(
                workspace_id TEXT NOT NULL,operation_type TEXT NOT NULL,request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,resource_id TEXT NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(workspace_id,operation_type,request_id));
                CREATE TABLE IF NOT EXISTS manual_result_snapshot(
                snapshot_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,job_id TEXT NOT NULL,
                body TEXT NOT NULL);''')
            for row in db.execute('SELECT body FROM manual_analysis').fetchall():
                item=json.loads(row[0])
                if item['status'] in ACTIVE or item['status']=='needs_verification':
                    if item.get('gpu_request_id') in self.coordinator.snapshot().pending:
                        self.coordinator.cancel_pending(item['gpu_request_id'],'manual')
                    self._recover_interrupted(db,item)
                    self.save(db,item)
        self.dispatcher = threading.Thread(target=self._dispatch,name='manual-dispatch',daemon=True)
        self.dispatcher.start()

    def db(self):
        db=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        db.row_factory=sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        with self.lock, closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK');raise

    def save(self,db,item):
        if item.get('result') and item.get('status')=='succeeded':self._snapshot(db,item)
        db.execute('UPDATE manual_analysis SET body=? WHERE job_id=?',(encoded(item),item['job_id']))
        self.sync_projection(db,item)

    @staticmethod
    def sync_projection(db,item):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='projected_jobs'").fetchone():return
        identifier=item['job_id']
        previous=db.execute('SELECT payload,event_seq FROM projected_jobs WHERE platform_job_id=?',(identifier,)).fetchone()
        counts={'schema_version':'1.0','total':1,'succeeded':int(item['status']=='succeeded'),
                'failed':int(item['status']=='failed'),'cancelled':int(item['status']=='cancelled'),
                'pending':int(item['status'] not in {'succeeded','failed','cancelled'})}
        sequence=previous['event_seq']+1 if previous else 1
        value={'schema_version':'1.0','platform_job_id':identifier,'app_id':item['kind'],'instance_id':'localManual',
            'native_job_id':identifier,'native_job_ids':[identifier],'operation_id':'op'+fingerprint(item['request_id'])[:32],
            'input_manifest_hash':fingerprint(item['inputs']),'native_status':item['status'],
            'status':'unknown' if item['status']=='needs_verification' else item['status'],'stage':item['stage'],
            'counts':counts,'capabilities':['cancel'] if item['status'] in ACTIVE else ['retry_failed'] if item['status'] in {'failed','cancelled','needs_verification'} else [],
            'last_event_seq':sequence,'updated_at':item['updated_at'],'availability':'ready','generation':item['generation']}
        db.execute('''INSERT INTO projected_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform_job_id)
            DO UPDATE SET generation=excluded.generation,event_seq=excluded.event_seq,payload=excluded.payload''',
            (identifier,item['workspace_id'],'manual',item['kind'],item['workspace_id'],'localManual',item['generation'],
             value['operation_id'],value['input_manifest_hash'],encoded([identifier]),sequence,encoded(value)))
        db.execute('INSERT INTO projection_events(platform_job_id,workspace_id,event_seq,instance_id,generation,payload) VALUES(?,?,?,?,?,?)',
            (identifier,item['workspace_id'],sequence,'localManual',item['generation'],encoded(value)))

    def close(self):
        self.stopping.set()
        self.wake.set()
        self.dispatcher.join(timeout=10)
        with self.transaction() as db:
            for row in db.execute('SELECT body FROM manual_analysis').fetchall():
                item=json.loads(row[0])
                if item['status'] in ACTIVE:
                    self._cancel_pending(item)
                    if item['status']=='queued' and not item.get('worker_epoch'):
                        item.update(stage='queued',gpu_request_id=None,updated_at=now());self.save(db,item)
                        continue
                    self._feature()
                    item.update(recovery_intent=item['status'],status='needs_verification',stage='interrupted',updated_at=now());self.save(db,item)
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.gpu_pool.shutdown(wait=True,cancel_futures=True)

    def _cancel_pending(self,item):
        identifier=item.get('gpu_request_id')
        if identifier and identifier in self.coordinator.snapshot().pending:
            self.coordinator.cancel_pending(identifier,'manual')

    def _admit_gpu(self,db,item):
        if item.get('gpu_request_id') not in self.coordinator.snapshot().pending:
            identifier='manual-'+uuid.uuid4().hex
            self.coordinator.request(identifier,'manual')
            try:
                item['gpu_request_id']=identifier
                item.update(stage='waiting_gpu',updated_at=now())
                self.save(db,item)
            except BaseException:
                self.coordinator.cancel_pending(identifier,'manual')
                raise

    def _check_disk_budget(self,db,*,additional_audio=0,copy_bytes=0):
        reserved=additional_audio
        for row in db.execute('SELECT body FROM manual_analysis').fetchall():
            item=json.loads(row[0])
            if item['kind']=='audio' and item['status'] in ACTIVE:
                reserved+=AUDIO_DISK_BUDGET
        needed=reserved+copy_bytes+DISK_MARGIN
        if shutil.disk_usage(self.root).free<needed:
            raise DataError(f'F 盘可用空间不足：处理及并行音频需预留至少 {needed/1024**3:.2f} GiB；已有原件和检查点已保留',503)

    def _stage_input(self,db,source,destination,blob_hash):
        source,destination=local_path(source),local_path(destination)
        if destination.exists():
            if self.file_hash(destination)!=blob_hash:
                raise DataError('已有输入副本校验失败；原文件已保留，请核实后重试',409)
            return
        try:
            os.link(source,destination)
        except OSError:
            self._check_disk_budget(db,copy_bytes=source.stat().st_size)
            # No deletion or replacement of old inputs; only a new assigned path.
            with source.open('rb') as origin,destination.open('xb') as target:
                shutil.copyfileobj(origin,target,length=8*1024*1024)
        if self.file_hash(destination)!=blob_hash:
            raise DataError('原件副本校验失败，原文件已保留',409)

    def _dispatch(self):
        last_recovery=0
        last_job_recovery=time.monotonic()
        while not self.stopping.is_set():
            try:
                snapshot=self.coordinator.snapshot()
                if snapshot.owner and snapshot.expires_at <= time.time() and time.monotonic()-last_recovery>5:
                    last_recovery=time.monotonic()
                    try:
                        recover_owned(self.coordinator,self.ownership_root)
                        self.gpu_recovery_error=None
                    except (OSError,ValueError,KeyError) as error:
                        self.gpu_recovery_error=str(error)
                with self.transaction() as db:
                    rows=[json.loads(row[0]) for row in db.execute('SELECT body FROM manual_analysis ORDER BY rowid').fetchall()]
                    reconcile=time.monotonic()-last_job_recovery>=5
                    if reconcile:last_job_recovery=time.monotonic()
                    for item in rows:
                        if reconcile and item['status']=='needs_verification' and item.get('worker_epoch') and item['job_id'] not in self.inflight:
                            before=encoded(item);self._recover_interrupted(db,item)
                            if encoded(item)!=before:self.save(db,item)
                        if item['status']!='queued' or item['job_id'] in self.inflight:continue
                        gpu=item['options']['device']=='cuda'
                        lane='gpu' if gpu else 'cpu'
                        if gpu:
                            try:self._admit_gpu(db,item)
                            except ResourceBusy:continue
                            snapshot=self.coordinator.snapshot()
                            position=snapshot.pending.index(item['gpu_request_id'])+1+int(snapshot.owner is not None)
                            information={**item.get('progress',{}),'queue_position':position}
                            if information!=item.get('progress'):
                                item['progress']=information;self.save(db,item)
                            if snapshot.owner or not snapshot.pending or snapshot.pending[0]!=item['gpu_request_id']:continue
                        if lane in self.inflight.values():continue
                        self.inflight[item['job_id']]=lane
                        executor=self.gpu_pool if gpu else self.pool
                        executor.submit(self.run,item['workspace_id'],item['job_id'])
            except Exception as error:
                self.gpu_recovery_error=str(error)[:600]
            self.wake.wait(self.config.get('dispatch_poll_seconds',.25))
            self.wake.clear()

    def row(self,db,workspace,job):
        row=db.execute('SELECT body FROM manual_analysis WHERE workspace_id=? AND job_id=?',(workspace,job)).fetchone()
        if row is None: raise DataError('分析任务不属于当前项目',404)
        return json.loads(row[0])

    def get(self,workspace,job):
        with closing(self.db()) as db:return self.public(self.row(db,workspace,job))

    def _feature(self):
        from workbench_platform.resources.reader_features import require_features
        require_features(self.root.parent,'agent-multimodal-v1')

    def _snapshot(self,db,item):
        if item['status']!='succeeded' or not item.get('result') or not item.get('result_hash'):
            raise DataError('结果尚未完成，不能创建固定引用',409)
        analysis=self.public(item)
        identity={'workspace_id':item['workspace_id'],'job_id':item['job_id'],'kind':item['kind'],
            'generation':item['generation'],'revision':item['revision'],
            'review_revision':item['revision'] if item.get('review') else 0,'result_hash':item['result_hash'],
            'model_identity':aigc_backend.public_identity(item.get('model_binding')) if item['kind']=='aigc_image' else copy.deepcopy(item['result'].get('model_hashes',{})),
            'sources':copy.deepcopy(item['inputs']),
            'reference_scope':[{key:copy.deepcopy(value) for key,value in row.items() if key!='feature'}
                for row in item.get('frozen_references',[])]}
        identifier='ms'+fingerprint(identity)[:48]
        previous=db.execute('SELECT body FROM manual_result_snapshot WHERE snapshot_id=?',(identifier,)).fetchone()
        if previous:return json.loads(previous['body'])
        value={'schema_version':'1.0','snapshot_id':identifier,**identity,
            'review_state':'reviewed' if item.get('review') else 'unreviewed','analysis':analysis}
        value['snapshot_hash']=fingerprint(value)
        self._feature()
        db.execute('INSERT INTO manual_result_snapshot VALUES(?,?,?,?)',
            (identifier,item['workspace_id'],item['job_id'],encoded(value)))
        return value

    def snapshot(self,workspace,job,snapshot_id=None):
        with self.transaction() as db:
            self.row(db,workspace,job)
            if snapshot_id is None:return self._snapshot(db,self.row(db,workspace,job))
            row=db.execute('SELECT body FROM manual_result_snapshot WHERE workspace_id=? AND job_id=? AND snapshot_id=?',
                (workspace,job,snapshot_id)).fetchone()
            if row is None:raise DataError('固定结果不存在或不属于当前项目任务',404)
            value=json.loads(row['body']);digest=value.pop('snapshot_hash',None)
            if fingerprint(value)!=digest:raise DataError('固定结果身份校验失败',409)
            value['snapshot_hash']=digest
            return value

    def snapshot_at_revision(self,workspace,job,revision):
        with self.transaction() as db:
            current=self.row(db,workspace,job)
            rows=db.execute('SELECT snapshot_id FROM manual_result_snapshot WHERE workspace_id=? AND job_id=? AND json_extract(body,\'$.revision\')=?',
                (workspace,job,revision)).fetchall()
            if not rows:
                history=db.execute('SELECT body FROM manual_review WHERE workspace_id=? AND json_extract(body,\'$.job_id\')=? AND json_extract(body,\'$.revision\')=?',
                    (workspace,job,revision)).fetchall()
                if len(history)!=1:raise DataError('固定复核版本不存在或身份不唯一，必须指定快照',409)
                receipt=json.loads(history[0]['body'])
                if (receipt.get('workspace_id')!=workspace or receipt.get('job_id')!=job or receipt.get('revision')!=revision
                        or receipt.get('status')!='succeeded' or not receipt.get('review')
                        or receipt.get('inputs')!=current['inputs'] or not receipt.get('result_hash')):
                    raise DataError('历史复核回执身份无法核实',409)
                restored=copy.deepcopy(receipt)
                if current['kind']=='aigc_image':
                    if aigc_backend.public_identity(current.get('model_binding'))!=receipt.get('model_binding'):
                        raise DataError('历史 AIGC 复核回执与冻结模型身份不一致',409)
                    restored['model_binding']=copy.deepcopy(current['model_binding'])
                output=self.root/job/f"attempt-{receipt['generation']}"
                try:self._accept_result(restored,output)
                except (OSError,ValueError,KeyError) as error:raise DataError('历史原生结果或输出文件不可核实',409) from error
                if (restored['result_hash']!=receipt['result_hash']
                        or self.public(restored)['result']!=self.public(receipt)['result']):
                    raise DataError('历史原生结果与固定复核回执不一致',409)
                # Snapshot only the durable old receipt, never the current job;
                # a retry or later review must not alter its original meaning.
                value=self._snapshot(db,receipt)
                return value
        if len(rows)!=1:raise DataError('固定复核版本不存在或身份不唯一，必须指定快照',409)
        return self.snapshot(workspace,job,rows[0]['snapshot_id'])

    def snapshot_output(self,workspace,job,snapshot_id,name):
        value=self.snapshot(workspace,job,snapshot_id);item=value['analysis']
        if name in item['result'].get('output_files',{}):return self._output_item(item,name)
        if Path(name).name!=name:raise DataError('结果文件不存在',404)
        _,files=self.snapshot_export(workspace,job,snapshot_id)
        if name not in files:raise DataError('结果文件不存在',404)
        return files[name],mimetypes.guess_type(name)[0] or 'application/octet-stream'

    def snapshot_export(self,workspace,job,snapshot_id):
        from types import SimpleNamespace
        from .exports import export_files
        from workbench_platform.resources.recovery import atomic_json
        with self.lock:
            frozen=self.snapshot(workspace,job,snapshot_id);item=frozen['analysis']
            destination=self.root/job/'snapshot-exports'/snapshot_id
            identity_path=destination/'export.identity.json'
            if identity_path.is_file():
                identity=json.loads(identity_path.read_text('utf-8'))
                if identity.get('snapshot_hash')!=frozen['snapshot_hash']:raise DataError('导出快照身份校验失败',409)
                files={}
                for name,record in identity.get('files',{}).items():
                    if Path(name).name!=name:raise DataError('导出文件身份无效',409)
                    path=self._output_item(item,name)[0] if name in item['result'].get('output_files',{}) else destination/name
                    if not path.is_file() or path.stat().st_size!=record['bytes'] or self.file_hash(path)!=record['sha256']:
                        raise DataError('固定导出文件校验失败，请保留成果并核实',409)
                    files[name]=path
                archive=destination/'results.zip';record=identity.get('archive',{})
                if not archive.is_file() or archive.stat().st_size!=record.get('bytes') or self.file_hash(archive)!=record.get('sha256'):
                    raise DataError('固定导出包校验失败，请保留成果并核实',409)
                return archive,files
            view=SimpleNamespace(root=self.root,get=lambda *_:copy.deepcopy(item),file_hash=self.file_hash,
                output=lambda _workspace,_job,name:self._output_item(item,name))
            archive,files=export_files(view,workspace,job,destination=destination)
            identity={'snapshot_hash':frozen['snapshot_hash'],'files':{name:{'bytes':path.stat().st_size,'sha256':self.file_hash(path)}
                for name,path in files.items()},'archive':{'bytes':archive.stat().st_size,'sha256':self.file_hash(archive)}}
            atomic_json(identity_path,identity)
            return archive,files

    def _operation(self,db,workspace,operation_type,request_id,digest=None):
        if not request_id:return None
        row=db.execute('SELECT * FROM manual_operation WHERE workspace_id=? AND operation_type=? AND request_id=?',
            (workspace,operation_type,request_id)).fetchone()
        if row is None:return None
        if digest is not None and row['payload_hash']!=digest:raise DataError('操作请求身份已经绑定其他参数',409)
        return json.loads(row['body'])

    def _receipt(self,db,workspace,operation_type,request_id,digest,resource_id,body):
        if request_id:
            self._feature()
            db.execute('INSERT INTO manual_operation VALUES(?,?,?,?,?,?)',
                (workspace,operation_type,request_id,digest,resource_id,encoded(body)))

    def lookup_operation(self,workspace,operation_type,request_id):
        allowed={'create','prepare','review','reference_add','reference_delete','draft_to_data','control:cancel','control:retry','control:retry_cpu','recover'}
        if operation_type not in allowed:raise DataError('未知多模态操作类型')
        with closing(self.db()) as db:
            result=self._operation(db,workspace,operation_type,request_id)
            if result is not None:
                resource=result.get('job_id') or result.get('reference_id') or result.get('session_id')
            elif operation_type in {'create','prepare','review','reference_add'}:
                table={'create':'manual_analysis','prepare':'manual_analysis','review':'manual_review','reference_add':'manual_face_reference'}[operation_type]
                row=db.execute(f'SELECT body FROM {table} WHERE workspace_id=? AND request_id=?',(workspace,request_id)).fetchone()
                result=json.loads(row['body']) if row else None
                if result is not None:
                    if operation_type in {'create','prepare'}:
                        expected='preparation' if operation_type=='prepare' else 'analysis'
                        result=self.public(result) if result.get('purpose','analysis')==expected else None
                    if result is not None:result.pop('feature',None)
                resource=(result or {}).get('job_id') or (result or {}).get('reference_id')
            else:resource=None
        return {'found':result is not None,'operation_type':operation_type,'request_id':request_id,
            'resource_id':resource,'receipt':result}

    def _recover_interrupted(self,db,item):
        # A queued item without an epoch has never opened its worker gate.
        if not item.get('worker_epoch') and (item['status']=='queued' or item.get('recovery_intent')=='queued'):
            item.update(status='queued',stage='queued',error=None,updated_at=now());return
        if item['status']=='needs_verification' and not item.get('worker_epoch'):
            proof=self._never_started_receipt(db,item) if not item.get('recovery_intent') else None
            if proof:
                self._feature()
                item.update(status='queued',stage='recovered_queue',error=None,recovery_intent='queued',updated_at=now(),
                    recovery_receipt={'never_started':True,'receipt_source':proof,'verified_at':now(),'from_generation':item['generation']})
            return
        previous=item.get('recovery_intent',item['status'])
        if item['status']!='needs_verification' or 'recovery_intent' not in item:
            self._feature()
            item['recovery_intent']=previous
            item.update(status='needs_verification',stage='interrupted',
                error='上次执行中断；原执行终止凭证尚未确认',updated_at=now())
        output=self.root/item['job_id']/f"attempt-{item['generation']}"
        try:
            from dataclasses import fields
            from workbench_platform.resources.gpu import Owner
            from workbench_platform.resources.recovery import recovery_evidence
            path=output/'worker-ownership.json'
            if not path.is_file():return
            record=json.loads(path.read_text('utf-8'))
            if (record.get('worker_epoch')!=item.get('worker_epoch') or record.get('job_id')!=item['job_id']
                    or record.get('generation')!=item['generation']):return
            owner=Owner(**{field.name:record['owner'][field.name] for field in fields(Owner)})
            proof=recovery_evidence(record,owner)
            if not proof.reclaim_confirmed:return
            if item.get('gpu_request_id')==self.coordinator.snapshot().owner_request_id:
                recover_owned(self.coordinator,self.ownership_root)
            if previous=='cancelling':
                self._feature()
                item.update(status='cancelled',stage='cancelled',error=None,
                    termination_receipt={'worker_epoch':record['worker_epoch'],'verified_at':now()});return
            if record.get('terminal_status')=='succeeded' and (output/'result.json').is_file():
                if self.file_hash(output/'result.json')!=record.get('result_file_hash'):return
                identities=record.get('output_files')
                if not isinstance(identities,dict):return
                actual={path.name for path in output.iterdir() if path.is_file() and not path.name.startswith('input-')
                    and (path.suffix in {'.png','.wav','.npy'} or path.name=='asr-original.json')}
                if actual!=set(identities):return
                for name,identity in identities.items():
                    path=output/name
                    if Path(name).name!=name or not path.is_file() or path.stat().st_size!=identity.get('bytes') or self.file_hash(path)!=identity.get('sha256'):return
                self._accept_result(item,output)
                if item.get('purpose')=='preparation' or item.get('prepare_on_success'):self._record_readiness(item,output)
                item['recovery_receipt']={'from_generation':item['generation'],'verified_at':now(),'result_recovered':True}
                return
            self._feature()
            item.update(status='queued',stage='recovered',error=None,generation=item['generation']+1,
                revision=item['revision']+1,worker_epoch=None,gpu_request_id=None,actual_device=None,
                recovery_receipt={'from_generation':item['generation'],'verified_at':now(),'process_tree_exited':True})
        except (OSError,ValueError,KeyError,ReleaseUnconfirmed):
            # Missing or contradictory evidence never authorizes another writer.
            return

    def _never_started_receipt(self,db,item):
        directory=self.root/item['job_id']
        if directory.exists() and any(directory.iterdir()):return None
        if item.get('gpu_request_id') and self.coordinator.snapshot().owner_request_id==item['gpu_request_id']:return None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='projection_events'").fetchone():
            try:
                started=db.execute("SELECT 1 FROM projection_events WHERE platform_job_id=? AND workspace_id=? AND generation=? "
                    "AND json_extract(payload,'$.native_status') IN ('running','cancelling','succeeded','completed','failed','cancelled') LIMIT 1",
                    (item['job_id'],item['workspace_id'],item['generation'])).fetchone()
                if started:return None
            except sqlite3.Error:return None
        rows=db.execute('SELECT body FROM manual_operation WHERE workspace_id=? AND resource_id=?',
            (item['workspace_id'],item['job_id'])).fetchall()
        for row in rows:
            try:receipt=json.loads(row['body'])
            except (ValueError,TypeError):continue
            if not isinstance(receipt,dict):continue
            if (receipt.get('job_id')==item['job_id'] and receipt.get('workspace_id')==item['workspace_id']
                    and receipt.get('status')=='queued' and receipt.get('generation')==item['generation']
                    and receipt.get('revision')==item['revision'] and receipt.get('inputs')==item['inputs']):
                return 'native_operation'
        ledger=self.root.parent/'agent/operations.sqlite3'
        if not ledger.is_file():return None
        try:
            with closing(sqlite3.connect('file:'+ledger.as_posix()+'?mode=ro',uri=True)) as journal:
                rows=journal.execute('SELECT o.tool,o.result FROM operations o JOIN runs r ON r.id=o.run_id '
                    'WHERE o.id=? AND r.workspace_id=?',(item['request_id'],item['workspace_id'])).fetchall()
            tools={'face_analyze':'face','audio_transcribe':'audio','seal_extract':'seal','catnet_analyze':'catnet','document_forensics_analyze':'document_forensics','aigc_image_analyze':'aigc_image','multimodal_prepare':item['kind']}
            for tool,body in rows:
                if tools.get(tool)!=item['kind'] or not body:continue
                receipt=json.loads(body);data=receipt.get('data',{})
                if (receipt.get('status')!='pending' or data.get('status')!='queued' or data.get('job_id')!=item['job_id']
                        or data.get('generation')!=item['generation']):continue
                if any(ref.get('kind')=='job' and ref.get('application')=='multimodal'
                        and ref.get('workspace_id')==item['workspace_id'] and ref.get('object_id')==item['job_id']
                        and ref.get('revision')==item['generation'] for ref in receipt.get('job_refs',[])):
                    return 'agent_submission'
        except (OSError,ValueError,TypeError,AttributeError,KeyError,sqlite3.Error):return None
        return None

    def recover(self,workspace,job,payload=None):
        value=validated(ManualControl,payload or {});digest=fingerprint({'job_id':job,**value})
        with self.transaction() as db:
            previous=self._operation(db,workspace,'recover',value['request_id'],digest)
            if previous is not None:return previous
            item=self.row(db,workspace,job)
            for field in ('revision','generation'):
                if value['expected_'+field] is not None and value['expected_'+field]!=item[field]:
                    raise DataError('任务版本或执行代次已经变化',409)
            if item['status']!='needs_verification' or job in self.inflight or job in self.processes:
                return self.public(item)
            before=encoded(item);self._recover_interrupted(db,item)
            if encoded(item)!=before:self.save(db,item)
            if item['status']!='needs_verification':
                self._receipt(db,workspace,'recover',value['request_id'],digest,job,self.public(item))
        self.wake.set()
        return self.public(item)

    def _termination_verified(self,item):
        if not item.get('worker_epoch'):return item['status']=='cancelled'
        try:
            from workbench_platform.resources.gpu import Owner
            from workbench_platform.resources.recovery import recovery_evidence
            path=self.root/item['job_id']/f"attempt-{item['generation']}"/'worker-ownership.json'
            record=json.loads(path.read_text('utf-8'))
            if (record.get('worker_epoch')!=item.get('worker_epoch') or record.get('job_id')!=item['job_id']
                    or record.get('generation')!=item['generation']):return False
            return recovery_evidence(record,Owner(**record['owner'])).reclaim_confirmed
        except (OSError,ValueError,KeyError,ReleaseUnconfirmed):return False

    @staticmethod
    def public(item):
        public_fields={'schema_version','job_id','workspace_id','request_id','kind','inputs','options','status','stage',
            'revision','generation','created_at','updated_at','result','result_hash','review','error','progress','actual_device','purpose','model_binding'}
        if item.get('kind')=='aigc_image':
            item={**item,'model_binding':aigc_backend.public_identity(item.get('model_binding'))}
            if item.get('result') and item['result'].get('model_binding'):
                item['result']={**item['result'],'model_binding':aigc_backend.public_identity(item['result']['model_binding'])}
        item={key:copy.deepcopy(value) for key,value in item.items() if key in public_fields}
        item.setdefault('progress',{})
        item.setdefault('actual_device',None)
        item.setdefault('purpose','analysis')
        from .revisions import attach_row_ids
        if item.get('result'):attach_row_ids(item,item['result'])
        def redact(value):
            if isinstance(value,dict):return {key:redact(record) for key,record in value.items() if key not in {'feature','embedding'}}
            if isinstance(value,list):return [redact(record) for record in value]
            return value
        item=redact(item)
        return item

    def list(self,workspace,offset=0,limit=50,kind=None):
        condition='workspace_id=?';parameters=[workspace]
        if kind is not None:
            condition+=" AND json_extract(body,'$.kind')=?";parameters.append(kind)
        with closing(self.db()) as db:
            rows=db.execute(f'SELECT body FROM manual_analysis WHERE {condition} ORDER BY rowid DESC LIMIT ? OFFSET ?',
                            (*parameters,limit,offset)).fetchall()
            total=db.execute(f'SELECT COUNT(*) FROM manual_analysis WHERE {condition}',parameters).fetchone()[0]
        return {'items':[self.public(json.loads(row[0])) for row in rows],'total':total}

    def capabilities(self, *, deep_engine=None):
        definitions={
          'face':('runtimes/forensics/python.exe',['face/yunet.onnx','face/sface.onnx']),
          'audio':('runtimes/audio/python.exe',['audio/paraformer-zh/model.pt','audio/fsmn-vad/model.pt','audio/ct-punc/model.pt','audio/campplus/campplus_cn_common.bin']),
          'seal':('bundles/ocr/runtimes/ppocr/python.exe',['seal/PP-OCRv4_server_seal_det/inference.json','seal/PP-OCRv4_server_rec/inference.json','seal/PP-DocLayout-L/inference.json']),
          'catnet':('runtimes/catnet/python.exe',['catnet/CAT_full_v2.pth.tar','catnet/source/lib/models/network_CAT.py']),
          'document_forensics':('runtimes/catnet/python.exe',['catnet/CAT_full_v2.pth.tar','catnet/source/lib/models/network_CAT.py'])}
        receipts={}
        record=self.models/'validation.json'
        if record.exists():receipts=json.loads(record.read_text('utf-8'))
        local_receipts={};local_error=None
        local_record=self.root.parent/'multimodal-cpu-validation.json'
        if local_record.is_file():
            try:
                local_receipts=json.loads(local_record.read_text('utf-8'))
                if not isinstance(local_receipts,dict):raise ValueError('本机 CPU 验证回执结构无效')
            except (OSError,ValueError) as error:local_receipts={};local_error=str(error)
        items=[]
        for kind,(python,files) in definitions.items():
            if kind in deep_backend.DEEP_KINDS and (deep_engine or deep_backend.backend(self.config))=='safire':
                items.append(self.safire_capability(kind));continue
            missing=[file for file in files if not (self.models/file).is_file()]
            if not (self.program/python).is_file():missing.append(python)
            local=kind in local_receipts
            receipt=local_receipts[kind] if local else receipts.get(kind,{})
            if not isinstance(receipt,dict):receipt={}
            identities=receipt.get('resources',{})
            if not isinstance(identities,dict):identities={}
            # New independent adapters do not invalidate an unchanged legacy
            # worker. Every source actually named by its receipt is still hashed.
            source_hashes={name:self.file_hash(SOURCE/name) for name in receipt.get('source_hashes',{})
                           if (SOURCE/name).is_file()}
            try:
                fresh=bool(identities) and bool(source_hashes) and receipt.get('source_hashes')==source_hashes and receipt.get('adapter_sha256')==self.file_hash(SOURCE/'modules/manual_multimodal/worker.py') and (self.program/python).is_file() and receipt.get('runtime_sha256')==self.file_hash(self.program/python) and all(self.resource_matches(self.models/name,identity) for name,identity in identities.items())
            except (OSError,KeyError,TypeError,ValueError):fresh=False
            engineering=receipt.get('record',{})
            if not isinstance(engineering,dict):engineering={}
            outcome=engineering.get('outcome') or {}
            if not isinstance(outcome,dict):outcome={}
            local_success=not local or (engineering.get('actual_device')=='cpu' and engineering.get('cleanup_confirmed') is True
                and outcome.get('status')=='succeeded')
            cpu=not missing and fresh and receipt.get('cpu') is True and local_success
            gpu=False;gpu_reason='需完成本机 GPU 工程推理验证'
            if kind not in GPU_KINDS or kind not in self.config.get('gpu',{}).get('enabled_kinds',[]):
                gpu_reason='本阶段此能力使用 CPU'
            elif not missing and identities and self.qualification.path.is_file():
                try:
                    local=json.loads(self.qualification.path.read_text('utf-8'))
                    if kind in local.get('items',{}):
                        binding=self.gpu_binding(kind,python,identities)
                        gpu=self.qualification.ready(kind,binding,peak_target_mib=self.config.get('gpu',{}).get('peak_target_mib',12288))
                        if not gpu:gpu_reason='本机程序、模型、运行库或驱动设备变化，请重新验证 GPU'
                except (OSError,ValueError,KeyError) as error:gpu_reason=str(error)
            items.append({'kind':kind,'title':LABELS[kind],'state':'resources_missing' if missing else 'gpu_ready' if gpu else 'cpu_ready' if cpu else 'load_failed' if fresh and engineering.get('exit_code',0) else 'resources_present',
                          'cpu_ready':cpu,'cpu_reason':None if cpu else local_error or '缺少与当前程序、模型及运行库身份匹配的真实 CPU 推理回执',
                          'validation_source':'data_local' if local else 'bundled',
                          'gpu_ready':gpu,'gpu_reason':None if gpu else gpu_reason,'missing':missing,'validation':receipt if fresh else {},'runtime':python})
        items.append(self.aigc_capability())
        return {'items':items}

    def aigc_gpu_binding(self, model_binding=None):
        model_binding = model_binding or aigc_backend.bind(self)
        return self.aigc_qualification.binding('aigc_image', Path(model_binding['runtime']),
            [SOURCE/name for name in aigc_backend.PRODUCT_SOURCES] + [SOURCE/'config/multimodal.json'],
            {**model_binding['resources'],**{'runtime:'+name:identity for name,identity in model_binding['runtime_resources'].items()}},
            device_index=self.config.get('gpu', {}).get('device_index', 0))

    def aigc_capability(self, model_binding=None):
        missing=[];gpu=False;binding=None;reason='整图 AIGC 检测需完成本机真实 CUDA 工程验证'
        try:
            if model_binding is None and not self._has_gpu_receipt(self.aigc_qualification,'aigc_image'):
                # An absent qualification cannot become ready by hashing the
                # entire inference runtime. Listing only checks installed
                # prerequisites; actual bindings still verify every byte.
                root=aigc_backend.asset_root(self.config)
                inventory=json.loads(aigc_backend.member(root,'aigc-inventory.json').read_text('utf-8-sig'))
                if not isinstance(inventory,dict) or inventory.get('format')!='clear-aigc-assets-v1':raise ValueError('Unknown AIGC asset inventory format')
                engine=os.environ.get('WORKBENCH_AIGC_ENGINE',self.config.get('aigc_image',{}).get('default_engine',inventory.get('primary','community_forensics384')))
                selected=inventory.get('engines',{}).get(engine)
                if engine not in aigc_backend.ENGINES or not isinstance(selected,dict):raise ValueError('AIGC engine is not installed: '+str(engine))
                runtime=local_path(selected.get('runtime',inventory.get('runtime','')))
                if not runtime.is_file():raise ValueError('AIGC isolated runtime is missing')
                resources=selected.get('resources')
                if not isinstance(resources,dict) or not resources:raise ValueError('AIGC engine must pin its source and weight resources')
                for name,identity in resources.items():
                    path=aigc_backend.member(root,name)
                    if not isinstance(identity,dict) or not path.is_file() or path.stat().st_size!=identity.get('bytes'):
                        raise ValueError('AIGC resource is missing or changed: '+name)
                binding={'engine':engine,'runtime':str(runtime),'calibration':None}
            else:
                binding=model_binding or aigc_backend.bind(self)
                if model_binding:aigc_backend.verify(self,binding)
                identity=self.aigc_gpu_binding(binding)
                gpu=self.aigc_qualification.ready('aigc_image',identity,
                    peak_target_mib=self.config.get('gpu',{}).get('peak_target_mib',12288))
            if not gpu:reason='本机模型、适配器、运行库或 GPU 身份尚无匹配的工程验证回执'
        except (OSError,ValueError,KeyError,TypeError) as error:
            missing.append(str(error));reason=str(error)
        return {'kind':'aigc_image','title':LABELS['aigc_image'],
            'engine':binding['engine'] if binding else self.config.get('aigc_image',{}).get('default_engine','community_forensics384'),
            'state':'resources_missing' if missing else 'gpu_ready' if gpu else 'resources_present',
            'gpu_ready':gpu,'gpu_reason':None if gpu else reason,'cpu_ready':False,
            'cpu_reason':'产品入口只开放经过工程验证的 CUDA 路线；CLI CPU 研究不启用产品资格',
            'missing':missing,'runtime':binding['runtime'] if binding else '',
            'validation_source':'data_local','validation':{},'calibration':bool(binding and binding.get('calibration')),
            'business_accuracy_qualified':False,'qualification':'experimental'}

    def safire_gpu_binding(self,kind,model_binding=None):
        model_binding=model_binding or deep_backend.bind(self,'safire')
        sources=[SOURCE/'modules/manual_multimodal/safire_worker.py',SOURCE/'modules/manual_multimodal/safire_processing.py',
            SOURCE/'modules/manual_multimodal/worker.py',SOURCE/'modules/manual_multimodal/worker_protocol.py',
            SOURCE/'modules/manual_multimodal/image_geometry.py',Path(__file__),
            SOURCE/'src/workbench_platform/multimodal/deep_backend.py',SOURCE/'src/workbench_platform/multimodal/runner.py',
            SOURCE/'src/workbench_platform/multimodal/qualification.py',SOURCE/'src/workbench_platform/resources/recovery.py',
            SOURCE/'config/multimodal.json']
        if kind=='document_forensics':
            sources += sorted((SOURCE/'modules/image_forensics/forensic_toolbox').glob('*.py'))
        return self.safire_qualification.binding(kind,Path(model_binding['runtime']),sources,
            model_binding['resources'],device_index=self.config.get('gpu',{}).get('device_index',0))

    def safire_capability(self,kind,model_binding=None):
        missing=[];gpu=False;binding=None;reason='SAFIRE 需完成本机真实 CUDA 工程验证'
        try:
            qualified=model_binding is not None or self._has_gpu_receipt(self.safire_qualification,kind)
            if not qualified:
                # Presence is sufficient to explain a not-yet-qualified tool.
                # Model hashes and the runtime closure are checked whenever a
                # qualification or a frozen task binding can actually be used.
                deep_backend.assets(self.config,lambda _path:None)
                binding={'runtime':str(deep_backend.runtime(self.config))}
            else:
                binding=model_binding or deep_backend.bind(self,'safire')
            if not Path(binding['runtime']).is_file():missing.append(binding['runtime'])
            if not missing and qualified:
                identity=self.safire_gpu_binding(kind,binding)
                gpu=self.safire_qualification.ready(kind,identity,
                    peak_target_mib=self.config.get('gpu',{}).get('peak_target_mib',12288))
        except (OSError,ValueError,KeyError) as error:
            missing.append(str(error));reason=str(error)
        return {'kind':kind,'title':LABELS[kind],'engine':'SAFIRE',
            'state':'resources_missing' if missing else 'gpu_ready' if gpu else 'resources_present',
            'gpu_ready':gpu,'gpu_reason':None if gpu else reason,'cpu_ready':False,
            'cpu_reason':'SAFIRE 本轮只开放真实验证的 CUDA 路线；不自动替代显式 CPU 请求',
            'missing':missing,'validation_source':'data_local','validation':{},
            'runtime':str(deep_backend.runtime(self.config)),'calibration':False,
            'business_accuracy_qualified':False,'operational_trigger':'any_candidate_region'}

    @staticmethod
    def _has_gpu_receipt(qualification,kind):
        from .qualification import FORMAT
        try:
            record=json.loads(qualification.path.read_text('utf-8'))
            return record.get('format')==FORMAT and isinstance(record.get('items',{}).get(kind),dict)
        except (OSError,ValueError,TypeError,AttributeError):
            return False

    def capability_for_item(self,item):
        if item['kind']=='aigc_image':
            pinned=aigc_backend.item_binding(item)
            aigc_backend.verify(self,pinned)
            return self.aigc_capability(pinned)
        if item['kind'] in deep_backend.DEEP_KINDS:
            pinned=deep_backend.item_binding(self,item)
            deep_backend.verify(self,pinned)
            if pinned['engine']=='safire':return self.safire_capability(item['kind'],pinned)
            return next(row for row in self.capabilities(deep_engine='catnet')['items'] if row['kind']==item['kind'])
        return next(row for row in self.capabilities()['items'] if row['kind']==item['kind'])

    def gpu_binding(self,kind,runtime,identities):
        resources={name:{'bytes':(self.models/name).stat().st_size,'sha256':self.qualification.file_hash(self.models/name)} for name in identities}
        return self.qualification.binding(kind,self.program/runtime,
            [*sorted((SOURCE/'modules/manual_multimodal').glob('*.py')),SOURCE/'config/multimodal.json',Path(__file__),SOURCE/'src/workbench_platform/multimodal/runner.py',
             SOURCE/'src/workbench_platform/multimodal/qualification.py',SOURCE/'src/workbench_platform/resources/recovery.py'],
            resources,device_index=self.config.get('gpu',{}).get('device_index',0))

    def resource_matches(self,path,identity):
        # ZIP extraction changes timestamp precision. Content identity survives
        # relocation; the stat tuple only caches a hash already checked here.
        if not path.is_file() or path.stat().st_size!=identity['bytes']:return False
        stat=path.stat();key=(str(path),stat.st_size,stat.st_mtime_ns)
        actual=self.resource_hash_cache.get(key)
        if actual is None:
            actual=self.file_hash(path);self.resource_hash_cache[key]=actual
        return actual==identity['sha256']

    def create(self,workspace,payload):
        return self._create(workspace,payload,preparation=False)

    def prepare(self,workspace,kind,payload):
        if kind not in LABELS:raise DataError('未知准备能力')
        if kind=='aigc_image':
            raise DataError('整图 AIGC 产品入口只开放 CUDA；请运行独立本机 GPU 工程诊断',409)
        if kind in deep_backend.DEEP_KINDS and deep_backend.backend(self.config)=='safire':
            raise DataError('SAFIRE 只开放 CUDA；请运行本机 GPU 工程诊断，不以 CPU 诊断替代',409)
        if len(payload.get('inputs',[]))!=1:raise DataError('运行准备需要一份明确选择的输入资料')
        options={**payload.get('options',{})}
        if options.get('device','cpu')!='cpu':raise DataError('运行准备只验证 CPU；GPU 资格必须经过独立实测',409)
        options.update(device='cpu',action='detect' if kind=='face' else 'analyze',reference_ids=[])
        return self._create(workspace,{**payload,'kind':kind,'options':options},preparation=True)

    def _create(self,workspace,payload,*,preparation=False):
        payload=copy.deepcopy(payload)
        if payload.get('kind') in deep_backend.DEEP_KINDS | {'aigc_image'} and not preparation:
            options=payload.setdefault('options',{})
            if 'device' not in options:
                # Idempotent resubmission retains the original resolved default,
                # including across an operational backend rollback.
                with closing(self.db()) as db:
                    previous=db.execute('SELECT body FROM manual_analysis WHERE workspace_id=? AND request_id=?',
                        (workspace,payload.get('request_id'))).fetchone()
                options['device']=json.loads(previous['body'])['options']['device'] if previous else ('cuda' if payload.get('kind')=='aigc_image' or deep_backend.backend(self.config)=='safire' else 'cpu')
        value=validated(Create,payload)
        kind=value['kind'];count=len(value['inputs'])
        if count != (2 if kind=='face' and value['options']['action']=='compare' else 1):
            raise DataError('当前操作的输入数量无效')
        if kind!='face' and value['options']['action']!='analyze':raise DataError('分析操作无效')
        if any(type(index) is not int or index<0 for index in value['options']['face_indices']):raise DataError('人脸索引无效')
        digest_value=copy.deepcopy(value)
        if digest_value['options'].get('reference_ids') is None:digest_value['options'].pop('reference_ids',None)
        if preparation:digest_value['purpose']='preparation'
        digest=fingerprint(digest_value)
        with self.transaction() as db:
            existing=db.execute('SELECT body,payload_hash FROM manual_analysis WHERE workspace_id=? AND request_id=?',(workspace,value['request_id'])).fetchone()
            if existing:
                if existing['payload_hash']!=digest:raise DataError('此请求已经绑定其他输入',409)
                return self.public(json.loads(existing['body']))
            capability=next(x for x in self.capabilities()['items'] if x['kind']==kind)
            if capability['missing']:raise DataError('模型或运行环境尚未备齐：'+', '.join(capability['missing']),503)
            if value['options']['device']=='cpu' and kind in deep_backend.DEEP_KINDS and deep_backend.backend(self.config)=='safire':
                raise DataError('此模型仅开放已完成推理验证的 CUDA 路线，不提供 CPU 切换',409)
            prepare_on_success=value['options']['device']=='cpu' and not capability['cpu_ready'] and not preparation
            if value['options']['device']=='cuda' and not capability['gpu_ready']:
                raise DataError('此模型的 GPU 路线尚未通过本机工程实测，请先运行交付诊断',409)
            if kind=='audio':self._check_disk_budget(db,additional_audio=AUDIO_DISK_BUDGET)
            frozen_references=[]
            if kind=='face' and value['options']['action']=='search':
                requested=value['options'].get('reference_ids')
                library={row['reference_id']:row for row in self.references(workspace,private=True)['items']}
                chosen=list(library) if requested is None else requested
                if len(chosen)!=len(set(chosen)):raise DataError('参考范围不能包含重复身份')
                if any(identifier not in library for identifier in chosen):raise DataError('参考身份不存在或不属于当前项目',404)
                frozen_references=[{**copy.deepcopy(library[identifier]),'revision':library[identifier].get('revision',1)} for identifier in chosen]
                if not frozen_references:raise DataError('人脸检索需要明确选择至少一个已授权参考',409)
            elif value['options'].get('reference_ids'):
                raise DataError('参考范围仅适用于人脸检索')
            inputs=[]
            for source in value['inputs']:
                asset=self.registry.get(workspace,source['asset_id'])
                if (asset.revision,asset.blob_hash)!=(source['asset_revision'],source['blob_hash']):raise DataError('资料版本已经变化',409)
                suffix=Path(asset.original_name).suffix.lower()
                allowed={ 'face':{'.jpg','.jpeg','.png','.webp','.bmp'},'catnet':deep_backend.INPUT_SUFFIXES if deep_backend.backend(self.config)=='safire' else {'.jpg','.jpeg'},
                          'document_forensics':deep_backend.INPUT_SUFFIXES,
                          'aigc_image':aigc_backend.INPUT_SUFFIXES,
                          'audio':{'.wav','.mp3','.m4a','.flac','.ogg','.aac','.wma'},'seal':{'.jpg','.jpeg','.png','.webp','.bmp','.pdf'}}[kind]
                if suffix not in allowed:raise DataError('当前模型不支持此格式；请保留原件并选择适用工具')
                inputs.append({**source,'original_name':asset.original_name,'suffix':suffix})
            identifier='m'+uuid.uuid4().hex
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent,'manual-multimodal-v1','source-media-segment-v1','manual-long-audio-v1')
            self._feature()
            if kind=='document_forensics':require_features(self.root.parent,'document-forensics-v1')
            if kind=='aigc_image':require_features(self.root.parent,'aigc-image-v1')
            item={'schema_version':'1.0','job_id':identifier,'workspace_id':workspace,'request_id':value['request_id'],
                  'kind':kind,'inputs':inputs,'options':value['options'],'status':'queued','stage':'queued','revision':1,
                  'created_at':now(),'updated_at':now(),'result':None,'result_hash':None,'review':None,'error':None,'generation':1,
                  'progress':{},'actual_device':None,'frozen_references':frozen_references,'purpose':'preparation' if preparation else 'analysis','prepare_on_success':prepare_on_success}
            if kind in deep_backend.DEEP_KINDS:
                item['model_binding']=deep_backend.bind(self)
                if item['model_binding']['engine']=='safire':require_features(self.root.parent,'safire-forensics-v1')
            elif kind=='aigc_image':
                item['model_binding']=aigc_backend.bind(self)
            db.execute('INSERT INTO manual_analysis VALUES(?,?,?,?,?)',(identifier,workspace,value['request_id'],digest,encoded(item)))
            self.sync_projection(db,item)
            self._receipt(db,workspace,'prepare' if preparation else 'create',value['request_id'],digest,identifier,self.public(item))
        self.wake.set()
        return self.public(item)

    def run(self,workspace,job):
        epoch=uuid.uuid4().hex
        generation=None
        try:
            with self.transaction() as db:
                item=self.row(db,workspace,job)
                if item['status']!='queued':return
                if item['kind']=='audio':self._check_disk_budget(db)
                generation=item['generation']
                item.update(status='running',stage='prepare',worker_epoch=epoch,updated_at=now());self.save(db,item)
            output=f_path(self.root/job/f"attempt-{item['generation']}");output.mkdir(parents=True,exist_ok=True)
            if item.get('purpose')=='preparation' or item.get('prepare_on_success'):self._validate_preparation(item)
            checkpoints=local_path(self.root/job/'audio-checkpoints');checkpoints.mkdir(parents=True,exist_ok=True)
            reduced=item.get('oom_retries',0)>0
            pinned=aigc_backend.item_binding(item) if item['kind']=='aigc_image' else deep_backend.item_binding(self,item) if item['kind'] in deep_backend.DEEP_KINDS else None
            if pinned:
                (aigc_backend.verify if item['kind']=='aigc_image' else deep_backend.verify)(self,pinned)
            value={'kind':item['kind'],'options':item['options'],'models':str(self.models),'program':str(self.program),'inputs':[],
                   'references':copy.deepcopy(item['frozen_references']) if item['kind']=='face' and item['options']['action']=='search' and 'frozen_references' in item
                       else self.references(workspace,private=True)['items'] if item['kind']=='face' and item['options']['action']=='search' else [],
                   'runner':{'protocol':PROTOCOL,'worker_epoch':epoch,'launch_id':self.launch_id,'instance_id':self.instance_id,
                       'job_id':job,'generation':generation,'checkpoint_root':str(checkpoints),
                       'audio_batch_size_s':self.config.get('audio',{}).get('reduced_batch_size_s' if reduced else 'batch_size_s',15 if reduced else 30),
                       'catnet_tile_size':self.config.get('catnet',{}).get('reduced_tile_size' if reduced else 'tile_size',512 if reduced else 1024),
                       'catnet_halo':self.config.get('catnet',{}).get('halo',0)}}
            if pinned:
                value['model_binding']=pinned
                if item['kind']=='aigc_image':
                    for unused in ('audio_batch_size_s','catnet_tile_size','catnet_halo'):
                        value['runner'].pop(unused,None)
                    value['runner']['aigc_feature_batch_size']=self.config.get('aigc_image',{}).get('reduced_feature_batch_size' if reduced else 'feature_batch_size',16 if reduced else 32)
                elif pinned['engine']=='safire':
                    value['runner']['safire_points_per_batch']=32 if reduced else 64
            with ExitStack() as stack:
                for index,source in enumerate(item['inputs']):
                    staged=stack.enter_context(self.registry.stage_verified(workspace,source['asset_id'],revision=source['asset_revision'],blob_hash=source['blob_hash']))
                    destination=output/f"input-{index}{source['suffix']}"
                    with self.transaction() as db:self._stage_input(db,staged,destination,source['blob_hash'])
                    value['inputs'].append({**source,'path':str(destination)})
                (output/'request.json').write_text(encoded(value),encoding='utf-8')
                runtime=pinned['runtime'] if pinned else next(x['runtime'] for x in self.capabilities()['items'] if x['kind']==item['kind'])
                env=worker_environment(output,self.program/runtime,gpu=item['options']['device']=='cuda',device_index=self.config.get('gpu',{}).get('device_index',0))
                if item['kind']=='seal':env.update(OMP_NUM_THREADS='1',MKL_NUM_THREADS='8',OPENBLAS_NUM_THREADS='1')
                def cancelled():
                    if self.stopping.is_set():return True
                    with closing(self.db()) as db:current=self.row(db,workspace,job)
                    return (current['status'] in {'cancelling','needs_verification'} or current['generation']!=generation
                            or current.get('worker_epoch')!=epoch)
                def progress(update):
                    with self.transaction() as db:
                        current=self.row(db,workspace,job)
                        if current['generation']!=generation or current.get('worker_epoch')!=epoch or current['status']!='running':return
                        information={**current.get('progress',{}),**{key:value for key,value in update.items() if key not in {'stage','worker_epoch','event','checkpoint'}}}
                        information.pop('queue_position',None)
                        if 'total_ms' in information:information['duration_ms']=information.pop('total_ms')
                        if 'chunks_total' in information:information['total_chunks']=information.pop('chunks_total')
                        stage=str(update.get('stage',current['stage']))[:128]
                        if stage!=current['stage'] or information!=current.get('progress'):
                            current.update(stage=stage,progress=information,updated_at=now());self.save(db,current)
                def committed(event):
                    progress({**event,'stage':'音频分块检查点已提交','completed_chunks':event['chunk_index']+1})
                def track(process):
                    with self.lock:self.processes[job]=process
                self._feature()
                entry=aigc_backend.worker_entry(item['kind'],pinned)
                command=aigc_backend.worker_command(self.program/runtime,SOURCE/'modules/manual_multimodal'/entry,output/'request.json') if item['kind']=='aigc_image' else [str(self.program/runtime),str(SOURCE/'modules/manual_multimodal'/entry),str(output/'request.json')]
                outcome=self.runner.run(command,
                    output,env,value,request_id=item.get('gpu_request_id'),cancelled=cancelled,progress=progress,committed=committed,
                    process_started=track,timeout=self.config['worker_timeouts'][item['kind']])
                with self.transaction() as db:
                    current=self.row(db,workspace,job)
                    if current['generation']!=generation or current.get('worker_epoch')!=epoch:return
                    if current['status']=='needs_verification':return
                    current['active_seconds']=current.get('active_seconds',0)+outcome.elapsed_seconds
                    if outcome.status=='needs_verification':
                        current.update(recovery_intent=current['status'],status='needs_verification',stage='cleanup_unconfirmed',error=outcome.error)
                    elif current['status']=='cancelling' or outcome.status=='cancelled':
                        current.update(status='cancelled',stage='cancelled',error=None)
                    elif outcome.status in {'yielded','waiting'}:
                        current.update(status='queued',stage='waiting_gpu',error=None)
                    elif outcome.status=='oom' and not current.get('oom_retries'):
                        current.update(status='queued',stage='retry_reduced_load',error=None,oom_retries=1)
                        current['progress']={**current.get('progress',{}),'load_reduced':True}
                    elif outcome.status!='succeeded':
                        current.update(status='failed',stage='failed',error=outcome.error)
                    else:
                        self._accept_result(current,output)
                        if current.get('purpose')=='preparation' or current.get('prepare_on_success'):self._record_readiness(current,output)
                    if current['status']!='queued':self._cancel_pending(current)
                    current.update(updated_at=now());self.save(db,current)
        except Exception as error:
            with self.transaction() as db:
                item=self.row(db,workspace,job)
                if generation is not None and (item['generation']!=generation or item.get('worker_epoch')!=epoch):return
                if item['status']=='needs_verification':return
                self._cancel_pending(item)
                item.update(status='cancelled' if item['status']=='cancelling' else 'failed',error=str(error)[:2400],updated_at=now());self.save(db,item)
        finally:
            with self.lock:
                self.processes.pop(job,None)
                self.inflight.pop(job,None)
            self.wake.set()

    def _accept_result(self,current,output):
        result=json.loads((output/'result.json').read_text('utf-8'))
        if current.get('model_binding') and result.get('model_binding')!=current['model_binding']:
            raise DataError('实际检测器与任务冻结的模型身份不一致',409)
        actual=result.get('device')
        if actual!=current['options']['device']:raise DataError('模型实际执行设备与请求不一致',409)
        if current['kind']=='aigc_image':
            from packages.contracts.multimodal import AigcImageResult
            signal=AigcImageResult.model_validate(result.get('aigc_image')).model_dump(mode='json')
            pinned=current['model_binding']
            if result.get('source_sha256')!=current['inputs'][0]['blob_hash']:
                raise DataError('AIGC 实际读取的原件哈希与固定输入不一致',409)
            if signal['preprocessing_version']!=pinned['preprocessing_version'] or signal['calibration']!=pinned.get('calibration'):
                raise DataError('AIGC 预处理或校准记录与固定模型身份不一致',409)
            calibration=pinned.get('calibration')
            if signal['threshold']!=(calibration.get('threshold') if calibration else None):
                raise DataError('AIGC 阈值与冻结校准记录不一致',409)
            if any(result.get(name) for name in ('regions','heatmap','mask','pixel_probability','pixel_probability_artifact')):
                raise DataError('整图 AIGC 分类不能输出篡改定位或概率热图',409)
            result['aigc_image']=signal
            # Check the full worker binding above, then keep one compact model
            # identity in the canonical result and its public immutable snapshots.
            result['model_binding']=aigc_backend.public_identity(pinned)
        for segment in result.get('segments',[]):
            source=current['inputs'][0]
            segment['source']=SourceLocator.model_validate({'schema_version':'1.0','kind':'media_segment',
                'asset_id':source['asset_id'],'asset_revision':source['asset_revision'],'blob_hash':source['blob_hash'],
                'precision':'approximate','start_ms':segment['start_ms'],'end_ms':segment['end_ms'],
                'channel':current['options'].get('channel')}).model_dump(mode='json')
        self.attach_regions(current,result)
        from .revisions import attach_row_ids
        attach_row_ids(current,result)
        result['output_files']={path.name:{'bytes':path.stat().st_size,'sha256':self.file_hash(path)}
            for path in output.iterdir() if path.is_file() and (path.suffix in {'.png','.wav','.npy'} or path.name=='asr-original.json') and not path.name.startswith('input-')}
        current.update(status='succeeded',stage='completed',result=result,result_hash=fingerprint(result),error=None,actual_device=actual)
        if current['kind']=='audio' and result.get('audio'):
            duration=result['audio']['duration_ms'];chunks=result.get('performance',{}).get('total_chunks',0)
            current['progress']={**current.get('progress',{}),'processed_ms':duration,'duration_ms':duration,
                'completed_chunks':chunks,'total_chunks':chunks}

    def _record_readiness(self,item,output):
        from .cpu_validation import ReadinessRecordError
        if item.get('purpose')=='preparation':
            return self._record_prepared(item,output)
        try:
            self._record_prepared(item,output)
        except ReadinessRecordError as error:
            # Readiness bookkeeping is separate from a completed business result.
            item['preparation_pending']=True
            item['preparation_record_error']=str(error)[:500]
        else:
            item.pop('preparation_pending',None)
            item.pop('preparation_record_error',None)

    def _record_prepared(self,item,output):
        from .cpu_validation import record_cpu_validation
        runtime=next(value['runtime'] for value in self.capabilities()['items'] if value['kind']==item['kind'])
        record_cpu_validation(SOURCE,self.program,self.root.parent,item['kind'],self.program/runtime,output,
            {'kind':item['kind'],'outcome':{'status':'succeeded'},'actual_device':item['actual_device'],
             'cleanup_confirmed':True,'output':str(output),'job_id':item['job_id'],'generation':item['generation'],
             'scope':'Successful offline selected-input CPU inference; not business accuracy qualification'})

    def _validate_preparation(self,item):
        from .cpu_validation import validate_inventory
        runtime=next(value['runtime'] for value in self.capabilities()['items'] if value['kind']==item['kind'])
        validate_inventory(self.program,item['kind'],self.program/runtime)

    def control(self,workspace,job,action,payload=None):
        value=validated(ManualControl,payload or {})
        operation_type='control:'+action;digest=fingerprint({'job_id':job,'action':action,**value})
        with self.transaction() as db:
            previous=self._operation(db,workspace,operation_type,value['request_id'],digest)
            if previous is not None:return previous
            item=self.row(db,workspace,job)
            for field in ('revision','generation'):
                if value['expected_'+field] is not None and item[field]!=value['expected_'+field]:
                    raise DataError('任务版本或执行代次已经变化',409)
            if action=='cancel':
                if item['status']=='queued':
                    self._cancel_pending(item)
                    item.update(status='cancelled',stage='cancelled')
                elif item['status'] in ACTIVE:item['status']='cancelling'
                else:raise DataError('任务已结束',409)
            elif action in {'retry','retry_cpu'}:
                if item['status'] not in {'failed','cancelled','needs_verification'}:raise DataError('当前任务不能重试',409)
                if item.get('gpu_request_id') and self.coordinator.snapshot().owner_request_id==item['gpu_request_id']:
                    raise DataError('原任务 GPU 进程清理尚未核实，请等待可信恢复；不能并发写入音频检查点',409)
                if job in self.processes or job in self.inflight:
                    raise DataError('原执行尚未退出；不能并发重试',409)
                if item['status']=='needs_verification' and not self._termination_verified(item):
                    raise DataError('原执行终止凭证尚未核实，不能重试',409)
                capability=self.capability_for_item(item)
                target='cpu' if action=='retry_cpu' else item['options']['device']
                if target=='cpu':
                    if item['kind']=='aigc_image' or (item.get('model_binding') or {}).get('engine')=='safire':
                        raise DataError('此模型仅开放已完成推理验证的 CUDA 路线，不提供 CPU 切换',409)
                    if capability.get('missing'):raise DataError('模型或运行环境缺失，不能重试',409)
                    item['prepare_on_success']=not capability['cpu_ready']
                elif not capability['gpu_ready']:
                    raise DataError('模型或运行环境已变化，请完成推理验证后重试',409)
                if item['kind']=='audio':self._check_disk_budget(db,additional_audio=AUDIO_DISK_BUDGET)
                self._cancel_pending(item)
                if action=='retry_cpu':
                    item['options']={**item['options'],'device':'cpu'}
                    item['progress']={**item.get('progress',{}),'explicit_cpu_retry':True,'device_change_note':'已确认块可复用；CPU/GPU 浮点结果可能存在差异'}
                item.update(status='queued',stage='queued',error=None,generation=item['generation']+1,
                    revision=item['revision']+1,oom_retries=0,active_seconds=0,gpu_request_id=None,actual_device=None,
                    worker_epoch=None,result=None,result_hash=None,review=None)
            else:raise DataError('任务操作无效')
            item['updated_at']=now();self.save(db,item)
            self._receipt(db,workspace,operation_type,value['request_id'],digest,job,self.public(item))
        self.wake.set()
        return self.public(item)

    def review(self,workspace,job,payload):
        value=validated(Review,payload);digest=fingerprint({'job_id':job,**value})
        if not value['actor'].strip() or not value['note'].strip():raise DataError('请填写复核人和复核说明')
        with self.transaction() as db:
            previous=db.execute('SELECT body,payload_hash FROM manual_review WHERE workspace_id=? AND request_id=?',(workspace,value['request_id'])).fetchone()
            if previous:
                if previous['payload_hash']!=digest:raise DataError('审阅请求已经绑定其他内容',409)
                return json.loads(previous['body'])
            item=self.row(db,workspace,job)
            if item['status']!='succeeded' or item['revision']!=value['expected_revision']:raise DataError('结果未就绪或审阅版本已变化',409)
            edits=value['edits']
            if value['regions'] and item['kind'] not in deep_backend.DEEP_KINDS:raise DataError('当前工具不支持手工区域标注')
            for region in value['regions']:
                x1,y1,x2,y2=region['box']
                if not 0<=x1<x2<=1 or not 0<=y1<y2<=1:raise DataError('标注区域必须位于原图范围内')
                region['source']=self.region_source(item['inputs'][0],1,region['box'],'normalized')
            field='segments' if item['kind']=='audio' else 'seals' if item['kind']=='seal' else None
            rows=(item.get('result') or {}).get(field,[]) if field else []
            if edits and not field:raise DataError('此工具只支持人工结论与备注')
            if edits:
                from .revisions import normalized_edits
                value['edits']=normalized_edits(item,field,edits)
            item.update(revision=item['revision']+1,review={**value,'created_at':now()},updated_at=now())
            self.save(db,item);receipt=self.public(item)
            db.execute('INSERT INTO manual_review VALUES(?,?,?,?)',(workspace,value['request_id'],digest,encoded(receipt)))
        return receipt

    def references(self,workspace,private=False):
        with closing(self.db()) as db:
            rows=db.execute('SELECT body FROM manual_face_reference WHERE workspace_id=? ORDER BY rowid',(workspace,)).fetchall()
        items=[json.loads(row[0]) for row in rows]
        if not private:
            for item in items:item.pop('feature',None)
        return {'items':items}

    def add_reference(self,workspace,payload):
        value=validated(Reference,payload);digest=fingerprint(value)
        if not value['label'].strip():raise DataError('请填写参考名称')
        with self.transaction() as db:
            previous=db.execute('SELECT body,payload_hash FROM manual_face_reference WHERE workspace_id=? AND request_id=?',(workspace,value['request_id'])).fetchone()
            if previous:
                if previous['payload_hash']!=digest:raise DataError('参考库请求冲突',409)
                result=json.loads(previous['body']);result.pop('feature',None);return result
            count=db.execute('SELECT COUNT(*) FROM manual_face_reference WHERE workspace_id=?',(workspace,)).fetchone()[0]
            if count>=self.config['max_reference_faces']:raise DataError('项目参考库已达到首版容量上限',409)
            job=self.row(db,workspace,value['job_id'])
            if job['kind']!='face' or job['status']!='succeeded':raise DataError('请选择已完成的人脸检测任务')
            try:face=next(row for row in job['result']['images'][value['input_index']]['faces'] if row['face_index']==value['face_index'])
            except (KeyError,IndexError,StopIteration):raise DataError('所选人脸不存在') from None
            if not face['quality']['usable']:raise DataError('此人脸质量不足，不能加入参考库')
            item={'reference_id':'f'+uuid.uuid4().hex,'label':value['label'],'feature':face['feature'],
                  'source':job['inputs'][value['input_index']],'box':face['box'],'model_hash':job['result']['model_hashes']['sface'],
                  'created_at':now(),'job_id':job['job_id'],'revision':1}
            self._feature()
            db.execute('INSERT INTO manual_face_reference VALUES(?,?,?,?,?)',(item['reference_id'],workspace,value['request_id'],digest,encoded(item)))
            receipt={key:copy.deepcopy(record) for key,record in item.items() if key!='feature'}
            self._receipt(db,workspace,'reference_add',value['request_id'],digest,item['reference_id'],receipt)
        item.pop('feature',None);return item

    def delete_reference(self,workspace,identifier,payload=None):
        value=validated(ManualReferenceDelete,payload or {});digest=fingerprint({'reference_id':identifier,**value})
        with self.transaction() as db:
            previous=self._operation(db,workspace,'reference_delete',value['request_id'],digest)
            if previous is not None:return previous
            row=db.execute('SELECT body FROM manual_face_reference WHERE workspace_id=? AND reference_id=?',(workspace,identifier)).fetchone()
            if row is None and (value['request_id'] or value['expected_revision'] is not None):
                raise DataError('参考身份不存在或不属于当前项目',404)
            item=json.loads(row['body']) if row else {}
            if value['expected_revision'] is not None and value['expected_revision']!=item.get('revision',1):
                raise DataError('参考版本已经变化',409)
            receipt={'deleted':True,'reference_id':identifier,'revision':item.get('revision',1)} if payload else {'deleted':True}
            self._receipt(db,workspace,'reference_delete',value['request_id'],digest,identifier,receipt)
            db.execute('DELETE FROM manual_face_reference WHERE workspace_id=? AND reference_id=?',(workspace,identifier))
        return receipt

    def output(self,workspace,job,name):
        item=self.get(workspace,job)
        return self._output_item(item,name)

    def _output_item(self,item,name):
        identity=(item.get('result') or {}).get('output_files',{}).get(name)
        if item['status']!='succeeded' or not identity or Path(name).name!=name:raise DataError('结果文件不存在',404)
        path=self.root/item['job_id']/f"attempt-{item['generation']}"/name
        if not path.is_file() or path.stat().st_size!=identity['bytes'] or self.file_hash(path)!=identity['sha256']:
            raise DataError('结果文件校验失败，请保留任务并核查，不能替换固定成果',409)
        return path,mimetypes.guess_type(name)[0] or 'application/octet-stream'

    @staticmethod
    def file_hash(path):
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

    @staticmethod
    def region_source(source,page,box,space='image_pixels'):
        return SourceLocator.model_validate({'schema_version':'1.0','kind':'region',
            'asset_id':source['asset_id'],'asset_revision':source['asset_revision'],'blob_hash':source['blob_hash'],
            'page_number':page,'precision':'approximate','box':box,'coordinate_space':space,
            'image_version_id':'img'+source['blob_hash'][:32]+'p'+str(page),'transform_revision':1}).model_dump(mode='json')

    def attach_regions(self,item,result):
        for index,image in enumerate(result.get('images',[])):
            for face in image['faces']:face['source']=self.region_source(item['inputs'][index],1,face['box'])
        for seal in result.get('seals',[]):
            source=item['inputs'][0];page=seal['page_number']
            seal['source']=self.region_source(source,page,seal['box']) if seal.get('box') else SourceLocator.model_validate({
                'schema_version':'1.0','kind':'page','asset_id':source['asset_id'],'asset_revision':source['asset_revision'],
                'blob_hash':source['blob_hash'],'page_number':page,'precision':'page_only'}).model_dump(mode='json')

    def export(self,workspace,job):
        from .exports import export_files
        with self.lock:
            return export_files(self,workspace,job)

    def collect(self,workspace,job):
        with self.lock:return self._collect(workspace,job)

    def _transfer_key(self,workspace,job,snapshot_id,mode,ctx):
        legacy='d'+fingerprint([workspace,job,snapshot_id,mode])[:40]
        if ctx is None:
            return legacy
        # Data sessions are mutable workspaces. Reading the same immutable
        # analysis in two Agent tasks must not grant either the other's edits.
        session_id=getattr(ctx,'session_id','')
        if not session_id:
            raise DataError('Agent 字段转换缺少任务归属',409)
        data=self.app.state.data_workbench
        original=data.operation(legacy)
        store=getattr(ctx,'store',None)
        if original and store is not None:
            receipts=store.rows('SELECT o.result FROM operations o JOIN runs r ON r.id=o.run_id '
                'WHERE r.session_id=? AND o.result IS NOT NULL AND o.tool IN '
                "('multimodal_to_data_draft','multimodal_to_data_reviewed','results_to_input')",(session_id,))
            for row in receipts:
                value=json.loads(row['result']);body=value.get('data',{})
                refs=value.get('result_refs',[])
                if (value.get('status') in {'success','pending'}
                        and body.get('data_session_id',body.get('session_id'))==original['session_id']
                        and any(ref.get('application')=='multimodal' and ref.get('parent_id')==job
                            and ref.get('object_id')==snapshot_id for ref in refs)):
                    return legacy
        return 'd'+fingerprint([workspace,job,snapshot_id,mode,'agent-task',session_id])[:40]

    def to_data(self,workspace,job,*,ctx=None,snapshot_id=None):
        from dataclasses import replace
        from workbench_platform.agent.tools.results import install
        from workbench_platform.application_services.data import stage_asset_upload
        with self.lock:
            frozen=self.snapshot(workspace,job,snapshot_id);item=frozen['analysis']
            if item['kind']!='seal' or (item['review'] or {}).get('assessment')!='confirmed':
                raise DataError('请先保存当前固定版本的印章字段校正与人工核准',409)
            _,files=self.snapshot_export(workspace,job,frozen['snapshot_id'])
            if 'reviewed.csv' not in files:raise DataError('此结果没有可转入核对的印章字段',409)
            context=ctx or self.app.state.audit_service._source_context(job,workspace)
            key=self._transfer_key(workspace,job,frozen['snapshot_id'],'confirmed-to-data',ctx)
            sources=self._bridge_sources(workspace,frozen)
            provenance={'kind':'manual_seal_fields','analysis_id':job,'review_revision':item['revision'],
                'snapshot_id':frozen['snapshot_id'],'snapshot_hash':frozen['snapshot_hash'],
                'result_hash':item['result_hash'],'review':item['review'],'review_state':'confirmed','draft':False,
                'rows':[{'csv_row':i+2,'row_id':row['row_id'],'source':row['source']} for i,row in enumerate(item['result']['seals'])]}
            result=install(files['reviewed.csv'].read_bytes(),f'印章核准字段-{job}-v{item["revision"]}.csv','text/csv',sources,
                [s['asset_id'] for s in item['inputs']],provenance,replace(context,operation_id=key))['data']
            data=self.app.state.data_workbench
            session=data.create('印章字段核对 · '+item['inputs'][0]['original_name'][:60],request_key=key,workspace_id=workspace)
            upload=stage_asset_upload(data,self.registry,workspace,session['id'],result['asset_id'],result['asset_revision'],result['blob_hash'],key)
            action=data.operation(key)
            if action is None:
                action=data.submit(session['id'],'import',{'side':'left','upload_id':upload['id'],
                    'options':{'header_row':1,'encoding':'utf-8-sig','delimiter':','}},session['version'],request_key=key)
            elif action['status'] in {'failed','interrupted'}:action=data.resume_operation(key)
            return {'session_id':session['id'],'job':action,'asset':result,
                'location':f'/apps/data?workspace={workspace}&session={session["id"]}'}

    def draft_to_data(self,workspace,job,payload=None,*,ctx=None):
        """Deterministic unreviewed fields; this never creates a human review."""
        from dataclasses import replace
        from workbench_platform.agent.tools.results import install
        from workbench_platform.application_services.data import stage_asset_upload
        value=validated(ManualDraftTransfer,payload or {})
        digest=fingerprint({'job_id':job,**value})
        with self.lock:
            with closing(self.db()) as db:
                previous=self._operation(db,workspace,'draft_to_data',value['request_id'],digest)
                if previous is not None:return previous
            frozen=self.snapshot(workspace,job,value['snapshot_id'])
            item=frozen['analysis']
            if item['kind']!='seal':raise DataError('草稿转换仅支持印章候选字段',409)
            if value['expected_revision'] is not None and value['expected_revision']!=item['revision']:
                raise DataError('草稿结果版本已经变化',409)
            rows=(item['result'] or {}).get('seals',[])
            if not rows:raise DataError('没有可转换的印章候选字段',409)
            stream=io.StringIO(newline='');writer=csv.writer(stream)
            writer.writerow(['结果ID','原始文字','候选主体','候选编号','置信信息','来源定位','复核状态'])
            for row in rows:
                writer.writerow([row['row_id'],row.get('text',''),row.get('subject',''),row.get('number',''),
                    encoded({key:record for key,record in row.items() if 'confidence' in key or 'score' in key}),
                    encoded(row['source']),'未复核'])
            key=self._transfer_key(workspace,job,frozen['snapshot_id'],'draft',ctx)
            context=ctx or self.app.state.audit_service._source_context(job,workspace)
            sources=self._bridge_sources(workspace,frozen,draft=True)
            provenance={'kind':'manual_seal_draft','analysis_id':job,'snapshot_id':frozen['snapshot_id'],
                'generation':frozen['generation'],'result_hash':frozen['result_hash'],'snapshot_hash':frozen['snapshot_hash'],
                'review_state':'unreviewed','draft':True,'original_values':copy.deepcopy(rows),
                'rows':[{'csv_row':index+2,'row_id':row['row_id'],'source':row['source'],'review_state':'unreviewed'}
                    for index,row in enumerate(rows)]}
            self._feature()
            result=install(stream.getvalue().encode('utf-8-sig'),f'印章候选草稿-{job}-{frozen["snapshot_id"]}.csv',
                'text/csv',sources,[source['asset_id'] for source in item['inputs']],provenance,
                replace(context,operation_id=key))['data']
            data=self.app.state.data_workbench
            session=data.create('未复核印章草稿 · '+item['inputs'][0]['original_name'][:60],request_key=key,workspace_id=workspace)
            upload=stage_asset_upload(data,self.registry,workspace,session['id'],result['asset_id'],result['asset_revision'],result['blob_hash'],key)
            action=data.operation(key)
            if action is None:
                action=data.submit(session['id'],'import',{'side':'left','upload_id':upload['id'],
                    'options':{'header_row':1,'encoding':'utf-8-sig','delimiter':','}},session['version'],request_key=key)
            elif action['status'] in {'failed','interrupted'}:action=data.resume_operation(key)
            receipt={'session_id':session['id'],'job':action,'asset':result,'snapshot_id':frozen['snapshot_id'],
                'draft':True,'review_state':'unreviewed','location':f'/apps/data?workspace={workspace}&session={session["id"]}'}
            with self.transaction() as db:self._receipt(db,workspace,'draft_to_data',value['request_id'],digest,job,receipt)
            return receipt

    @staticmethod
    def _bridge_sources(workspace,frozen,*,draft=False):
        assets=[{'kind':'asset','workspace_id':workspace,'object_id':source['asset_id'],
            'revision':source['asset_revision'],'sha256':source['blob_hash']} for source in frozen['sources']]
        return [*assets,{'kind':'result','application':'multimodal','workspace_id':workspace,
            'object_id':frozen['snapshot_id'],'parent_id':frozen['job_id'],'native_id':frozen['job_id'],
            'revision':frozen['revision'],'sha256':frozen['snapshot_hash'],'sources':assets,
            'coverage':{'kind':frozen['kind'],'generation':frozen['generation'],
                'reviewed':bool(frozen['review_revision']) and not draft,'draft':draft}}]

    def collect_snapshot(self,workspace,job,snapshot_id):
        with self.lock:return self._collect(workspace,job,snapshot_id)

    def _collect(self,workspace,job,snapshot_id=None):
        frozen=self.snapshot(workspace,job,snapshot_id) if snapshot_id else None
        item=frozen['analysis'] if frozen else self.get(workspace,job)
        approved=(item['review'] or {}).get('assessment')=='confirmed'
        if not frozen and not approved:raise DataError('请先保存当前版本的人工核准再归集正式审计成果',409)
        from .artifacts import get as get_bundle
        from workbench_platform.artifacts.store import ArtifactUnavailable
        center=self.app.state.artifact_center
        identifier='b'+fingerprint(snapshot_id)[:40] if snapshot_id else 'b'+job[1:]+'v'+str(item['revision'])
        try:return get_bundle(center,workspace,identifier)
        except ArtifactUnavailable:pass
        _,files=self.snapshot_export(workspace,job,snapshot_id) if snapshot_id else self.export(workspace,job)
        source_refs=[{'schema_version':'1.0','kind':'file','asset_id':r['asset_id'],'asset_revision':r['asset_revision'],'blob_hash':r['blob_hash'],'precision':'exact'} for r in item['inputs']]
        source_refs += [r['source'] for r in item['result'].get('segments',[])][:9990]
        source_refs += [r['source'] for r in item['result'].get('seals',[])]
        source_refs += [face['source'] for image in item['result'].get('images',[]) for face in image['faces']]
        source_refs += [r['source'] for r in (item['review'] or {}).get('regions',[])]
        source_refs=source_refs[:10000]
        ref={'schema_version':'1.0','platform_job_id':job,'app_id':item['kind'],'instance_id':'localManual',
             'native_job_id':job,'operation_id':item['request_id'],'input_manifest_hash':fingerprint(item['inputs']),
             'native_status':'completed','status':'succeeded','stage':'reviewed_result' if item['review'] else 'unreviewed_draft','counts':{'schema_version':'1.0','total':1,'succeeded':1,'failed':0,'cancelled':0,'pending':0},
             'capabilities':[],'last_event_seq':0,'updated_at':item['updated_at']}
        artifacts=[]
        with center.store.publication_lock(identifier):
            try:return get_bundle(center,workspace,identifier)
            except ArtifactUnavailable:pass
            for name,path in files.items():
                with path.open('rb') as content:
                    artifact=center.store.publish_managed(workspace,content,expected_hash=self.file_hash(path),expected_size=path.stat().st_size,
                        job_ref=ref,source_refs=source_refs,media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream',
                        application_version='clear-manual-multimodal-1',rule_or_model_version=fingerprint(item['result'].get('model_hashes',{})))
                artifacts.append({'name':name,'artifact_id':artifact.artifact_id,'revision':artifact.revision})
        bundle={'schema_version':'1.0','bundle_id':identifier,'kind':'manual_analysis',
            'title':('未核准草稿 · ' if not approved else '')+LABELS[item['kind']]+' · '+item['inputs'][0]['original_name'],
            'created_at':(item['review'] or {}).get('created_at',item['updated_at']),
            'workspace_id':workspace,'platform_job_id':job,'analysis_kind':item['kind'],'review_revision':item['revision'] if item['review'] else 0,
            'input_set_id':job,'input_set_revision':1,'inputs':[{'role':'source','name':s['original_name'],'asset_id':s['asset_id'],'revision':s['asset_revision']} for s in item['inputs']],
            'files':[]}
        if frozen:bundle.update(snapshot_id=snapshot_id,snapshot_hash=frozen['snapshot_hash'],draft=not approved,review_state='confirmed' if approved else 'unreviewed')
        for record in artifacts:
            artifact=center.store.get(workspace,record['artifact_id'])
            bundle['files'].append({**record,'size_bytes':artifact.size_bytes,'blob_hash':artifact.blob_hash,'media_type':artifact.media_type,'availability':artifact.availability})
        with closing(center._connect()) as db:
            db.execute('INSERT OR IGNORE INTO manual_artifact_bundle VALUES(?,?,?,?,?)',
                       (bundle['bundle_id'],workspace,bundle['created_at'],'ready',encoded(bundle)))
        return bundle
