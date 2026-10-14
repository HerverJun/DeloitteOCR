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

from packages.contracts.multimodal import Create, Review, Reference
from packages.contracts.models import SourceLocator
from workbench_platform.audit.service import validated
from workbench_platform.batches.store import encoded, fingerprint, now
from workbench_platform.data_api.service import DataError
from workbench_platform.forensics.service import f_path
from workbench_platform.resources.gpu import GpuCoordinator, ResourceBusy, ReleaseUnconfirmed, StaleOwner
from workbench_platform.resources.recovery import local_path, recover_owned
from .runner import WorkerRunner, worker_environment, PROTOCOL
from .qualification import Qualification, GPU_KINDS

SOURCE = Path(__file__).resolve().parents[3]
ACTIVE = {'queued','running','cancelling'}
LABELS = {'face':'人脸比对','audio':'音频分析','seal':'印章识别','catnet':'深度图像取证'}
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
                payload_hash TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(workspace_id,request_id));''')
            for row in db.execute('SELECT body FROM manual_analysis').fetchall():
                item=json.loads(row[0])
                if item['status'] in ACTIVE:
                    if item.get('gpu_request_id') in self.coordinator.snapshot().pending:
                        self.coordinator.cancel_pending(item['gpu_request_id'],'manual')
                    item.update(status='needs_verification',error='上次执行中断；保留原任务，请明确重试',updated_at=now())
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
                    item.update(status='needs_verification',stage='interrupted',updated_at=now());self.save(db,item)
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
                    for item in rows:
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

    @staticmethod
    def public(item):
        public_fields={'schema_version','job_id','workspace_id','request_id','kind','inputs','options','status','stage',
            'revision','generation','created_at','updated_at','result','result_hash','review','error','progress','actual_device'}
        item={key:copy.deepcopy(value) for key,value in item.items() if key in public_fields}
        item.setdefault('progress',{})
        item.setdefault('actual_device',None)
        from .revisions import attach_row_ids
        if item.get('result'):attach_row_ids(item,item['result'])
        for image in (item.get('result') or {}).get('images',[]):
            for face in image.get('faces',[]):face.pop('feature',None)
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

    def capabilities(self):
        definitions={
          'face':('runtimes/forensics/python.exe',['face/yunet.onnx','face/sface.onnx']),
          'audio':('runtimes/audio/python.exe',['audio/paraformer-zh/model.pt','audio/fsmn-vad/model.pt','audio/ct-punc/model.pt','audio/campplus/campplus_cn_common.bin']),
          'seal':('bundles/ocr/runtimes/ppocr/python.exe',['seal/PP-OCRv4_server_seal_det/inference.json','seal/PP-OCRv4_server_rec/inference.json','seal/PP-DocLayout-L/inference.json']),
          'catnet':('runtimes/catnet/python.exe',['catnet/CAT_full_v2.pth.tar','catnet/source/lib/models/network_CAT.py'])}
        receipts={}
        record=self.models/'validation.json'
        if record.exists():receipts=json.loads(record.read_text('utf-8'))
        items=[]
        for kind,(python,files) in definitions.items():
            missing=[file for file in files if not (self.models/file).is_file()]
            if not (self.program/python).is_file():missing.append(python)
            receipt=receipts.get(kind,{})
            identities=receipt.get('resources',{})
            source_hashes={path.relative_to(SOURCE).as_posix():self.file_hash(path) for path in sorted((SOURCE/'modules/manual_multimodal').glob('*.py'))}
            fresh=bool(identities) and receipt.get('source_hashes')==source_hashes and receipt.get('adapter_sha256')==self.file_hash(SOURCE/'modules/manual_multimodal/worker.py') and (self.program/python).is_file() and receipt.get('runtime_sha256')==self.file_hash(self.program/python) and all(self.resource_matches(self.models/name,identity) for name,identity in identities.items())
            cpu=not missing and fresh and receipt.get('cpu') is True
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
            items.append({'kind':kind,'title':LABELS[kind],'state':'resources_missing' if missing else 'gpu_ready' if gpu else 'cpu_ready' if cpu else 'load_failed' if fresh and receipt.get('record',{}).get('exit_code',0) else 'resources_present',
                          'cpu_ready':cpu,'gpu_ready':gpu,'gpu_reason':None if gpu else gpu_reason,'missing':missing,'validation':receipt if fresh else {},'runtime':python})
        return {'items':items}

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
        value=validated(Create,payload)
        kind=value['kind'];count=len(value['inputs'])
        if count != (2 if kind=='face' and value['options']['action']=='compare' else 1):
            raise DataError('当前操作的输入数量无效')
        if kind!='face' and value['options']['action']!='analyze':raise DataError('分析操作无效')
        if any(type(index) is not int or index<0 for index in value['options']['face_indices']):raise DataError('人脸索引无效')
        digest=fingerprint(value)
        with self.transaction() as db:
            existing=db.execute('SELECT body,payload_hash FROM manual_analysis WHERE workspace_id=? AND request_id=?',(workspace,value['request_id'])).fetchone()
            if existing:
                if existing['payload_hash']!=digest:raise DataError('此请求已经绑定其他输入',409)
                return self.public(json.loads(existing['body']))
            capability=next(x for x in self.capabilities()['items'] if x['kind']==kind)
            if capability['missing']:raise DataError('模型或运行环境尚未备齐：'+', '.join(capability['missing']),503)
            if value['options']['device']=='cpu' and not capability['cpu_ready']:
                raise DataError('此模型尚未完成真实 CPU 推理验证，请先运行交付诊断',409)
            if value['options']['device']=='cuda' and not capability['gpu_ready']:
                raise DataError('此模型的 GPU 路线尚未通过实测，请使用 CPU',409)
            if kind=='audio':self._check_disk_budget(db,additional_audio=AUDIO_DISK_BUDGET)
            inputs=[]
            for source in value['inputs']:
                asset=self.registry.get(workspace,source['asset_id'])
                if (asset.revision,asset.blob_hash)!=(source['asset_revision'],source['blob_hash']):raise DataError('资料版本已经变化',409)
                suffix=Path(asset.original_name).suffix.lower()
                allowed={ 'face':{'.jpg','.jpeg','.png','.webp','.bmp'},'catnet':{'.jpg','.jpeg'},
                          'audio':{'.wav','.mp3','.m4a','.flac','.ogg','.aac','.wma'},'seal':{'.jpg','.jpeg','.png','.webp','.bmp','.pdf'}}[kind]
                if suffix not in allowed:raise DataError('当前模型不支持此格式；请保留原件并选择适用工具')
                inputs.append({**source,'original_name':asset.original_name,'suffix':suffix})
            identifier='m'+uuid.uuid4().hex
            from workbench_platform.resources.reader_features import require_features
            require_features(self.root.parent,'manual-multimodal-v1','source-media-segment-v1','manual-long-audio-v1')
            item={'schema_version':'1.0','job_id':identifier,'workspace_id':workspace,'request_id':value['request_id'],
                  'kind':kind,'inputs':inputs,'options':value['options'],'status':'queued','stage':'queued','revision':1,
                  'created_at':now(),'updated_at':now(),'result':None,'result_hash':None,'review':None,'error':None,'generation':1,
                  'progress':{},'actual_device':None}
            db.execute('INSERT INTO manual_analysis VALUES(?,?,?,?,?)',(identifier,workspace,value['request_id'],digest,encoded(item)))
            self.sync_projection(db,item)
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
            checkpoints=local_path(self.root/job/'audio-checkpoints');checkpoints.mkdir(parents=True,exist_ok=True)
            reduced=item.get('oom_retries',0)>0
            value={'kind':item['kind'],'options':item['options'],'models':str(self.models),'program':str(self.program),'inputs':[],
                   'references':self.references(workspace,private=True)['items'] if item['kind']=='face' and item['options']['action']=='search' else [],
                   'runner':{'protocol':PROTOCOL,'worker_epoch':epoch,'launch_id':self.launch_id,'instance_id':self.instance_id,
                       'job_id':job,'generation':generation,'checkpoint_root':str(checkpoints),
                       'audio_batch_size_s':self.config.get('audio',{}).get('reduced_batch_size_s' if reduced else 'batch_size_s',15 if reduced else 30),
                       'catnet_tile_size':self.config.get('catnet',{}).get('reduced_tile_size' if reduced else 'tile_size',512 if reduced else 1024)}}
            with ExitStack() as stack:
                for index,source in enumerate(item['inputs']):
                    staged=stack.enter_context(self.registry.stage_verified(workspace,source['asset_id'],revision=source['asset_revision'],blob_hash=source['blob_hash']))
                    destination=output/f"input-{index}{source['suffix']}"
                    with self.transaction() as db:self._stage_input(db,staged,destination,source['blob_hash'])
                    value['inputs'].append({**source,'path':str(destination)})
                (output/'request.json').write_text(encoded(value),encoding='utf-8')
                runtime=next(x['runtime'] for x in self.capabilities()['items'] if x['kind']==item['kind'])
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
                outcome=self.runner.run([str(self.program/runtime),str(SOURCE/'modules/manual_multimodal/worker.py'),str(output/'request.json')],
                    output,env,value,request_id=item.get('gpu_request_id'),cancelled=cancelled,progress=progress,committed=committed,
                    process_started=track,timeout=self.config['worker_timeouts'][item['kind']])
                with self.transaction() as db:
                    current=self.row(db,workspace,job)
                    if current['generation']!=generation or current.get('worker_epoch')!=epoch:return
                    if current['status']=='needs_verification':return
                    current['active_seconds']=current.get('active_seconds',0)+outcome.elapsed_seconds
                    if outcome.status=='needs_verification':
                        current.update(status='needs_verification',stage='cleanup_unconfirmed',error=outcome.error)
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
                        result=json.loads((output/'result.json').read_text('utf-8'))
                        actual=result.get('device')
                        if actual!=current['options']['device']:raise DataError('模型实际执行设备与请求不一致',409)
                        for segment in result.get('segments',[]):
                            source=current['inputs'][0]
                            segment['source']=SourceLocator.model_validate({'schema_version':'1.0','kind':'media_segment',
                                'asset_id':source['asset_id'],'asset_revision':source['asset_revision'],'blob_hash':source['blob_hash'],
                                'precision':'approximate','start_ms':segment['start_ms'],'end_ms':segment['end_ms'],'channel':current['options'].get('channel')}).model_dump(mode='json')
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

    def control(self,workspace,job,action):
        with self.transaction() as db:
            item=self.row(db,workspace,job)
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
                capability=next(value for value in self.capabilities()['items'] if value['kind']==item['kind'])
                target='cpu' if action=='retry_cpu' else item['options']['device']
                if not capability['cpu_ready' if target=='cpu' else 'gpu_ready']:
                    raise DataError('模型或运行环境已变化，请完成推理验证后重试',409)
                if item['kind']=='audio':self._check_disk_budget(db,additional_audio=AUDIO_DISK_BUDGET)
                self._cancel_pending(item)
                if action=='retry_cpu':
                    item['options']={**item['options'],'device':'cpu'}
                    item['progress']={**item.get('progress',{}),'explicit_cpu_retry':True,'device_change_note':'已确认块可复用；CPU/GPU 浮点结果可能存在差异'}
                item.update(status='queued',stage='queued',error=None,generation=item['generation']+1,oom_retries=0,active_seconds=0,gpu_request_id=None,actual_device=None)
            else:raise DataError('任务操作无效')
            item['updated_at']=now();self.save(db,item)
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
            if value['regions'] and item['kind']!='catnet':raise DataError('当前工具不支持手工区域标注')
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
                  'created_at':now(),'job_id':job['job_id']}
            db.execute('INSERT INTO manual_face_reference VALUES(?,?,?,?,?)',(item['reference_id'],workspace,value['request_id'],digest,encoded(item)))
        item.pop('feature',None);return item

    def delete_reference(self,workspace,identifier):
        with self.transaction() as db:db.execute('DELETE FROM manual_face_reference WHERE workspace_id=? AND reference_id=?',(workspace,identifier))
        return {'deleted':True}

    def output(self,workspace,job,name):
        item=self.get(workspace,job)
        identity=(item.get('result') or {}).get('output_files',{}).get(name)
        if item['status']!='succeeded' or not identity or Path(name).name!=name:raise DataError('结果文件不存在',404)
        path=self.root/job/f"attempt-{item['generation']}"/name
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

    def to_data(self,workspace,job):
        from dataclasses import replace
        from workbench_platform.agent.tools.results import install
        from workbench_platform.application_services.data import stage_asset_upload
        with self.lock:
            item=self.get(workspace,job)
            if item['kind']!='seal' or not item['review']:raise DataError('请先保存印章字段校正与人工复核',409)
            _,files=self.export(workspace,job)
            if 'reviewed.csv' not in files:raise DataError('此结果没有可转入核对的印章字段',409)
            ctx=self.app.state.audit_service._source_context(job,workspace)
            key='d'+fingerprint([job,item['revision']])[:40]
            sources=[{'kind':'asset','workspace_id':workspace,'object_id':s['asset_id'],
                'revision':s['asset_revision'],'sha256':s['blob_hash']} for s in item['inputs']]
            provenance={'kind':'manual_seal_fields','analysis_id':job,'review_revision':item['revision'],
                'result_hash':item['result_hash'],'review':item['review'],
                'rows':[{'csv_row':i+2,'row_id':row['row_id'],'source':row['source']} for i,row in enumerate(item['result']['seals'])]}
            result=install(files['reviewed.csv'].read_bytes(),f'印章核准字段-{job}-v{item["revision"]}.csv','text/csv',sources,
                [s['asset_id'] for s in item['inputs']],provenance,replace(ctx,operation_id=key))['data']
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

    def _collect(self,workspace,job):
        item=self.get(workspace,job)
        if not item['review']:raise DataError('请先保存人工复核再归集审计成果',409)
        from .artifacts import get as get_bundle
        from workbench_platform.artifacts.store import ArtifactUnavailable
        center=self.app.state.artifact_center
        identifier='b'+job[1:]+'v'+str(item['revision'])
        try:return get_bundle(center,workspace,identifier)
        except ArtifactUnavailable:pass
        _,files=self.export(workspace,job);center=self.app.state.artifact_center
        source_refs=[{'schema_version':'1.0','kind':'file','asset_id':r['asset_id'],'asset_revision':r['asset_revision'],'blob_hash':r['blob_hash'],'precision':'exact'} for r in item['inputs']]
        source_refs += [r['source'] for r in item['result'].get('segments',[])][:9990]
        source_refs += [r['source'] for r in item['result'].get('seals',[])]
        source_refs += [face['source'] for image in item['result'].get('images',[]) for face in image['faces']]
        source_refs += [r['source'] for r in item['review'].get('regions',[])]
        source_refs=source_refs[:10000]
        ref={'schema_version':'1.0','platform_job_id':job,'app_id':item['kind'],'instance_id':'localManual',
             'native_job_id':job,'operation_id':item['request_id'],'input_manifest_hash':fingerprint(item['inputs']),
             'native_status':'completed','status':'succeeded','stage':'reviewed_result','counts':{'schema_version':'1.0','total':1,'succeeded':1,'failed':0,'cancelled':0,'pending':0},
             'capabilities':[],'last_event_seq':0,'updated_at':item['updated_at']}
        artifacts=[]
        with center.store.publication_lock(job+'v'+str(item['revision'])):
            try:return get_bundle(center,workspace,identifier)
            except ArtifactUnavailable:pass
            for name,path in files.items():
                with path.open('rb') as content:
                    artifact=center.store.publish_managed(workspace,content,expected_hash=self.file_hash(path),expected_size=path.stat().st_size,
                        job_ref=ref,source_refs=source_refs,media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream',
                        application_version='clear-manual-multimodal-1',rule_or_model_version=fingerprint(item['result'].get('model_hashes',{})))
                artifacts.append({'name':name,'artifact_id':artifact.artifact_id,'revision':artifact.revision})
        bundle={'schema_version':'1.0','bundle_id':'b'+job[1:]+'v'+str(item['revision']),'kind':'manual_analysis',
            'title':LABELS[item['kind']]+' · '+item['inputs'][0]['original_name'],'created_at':item['review']['created_at'],
            'workspace_id':workspace,'platform_job_id':job,'analysis_kind':item['kind'],'review_revision':item['revision'],
            'input_set_id':job,'input_set_revision':1,'inputs':[{'role':'source','name':s['original_name'],'asset_id':s['asset_id'],'revision':s['asset_revision']} for s in item['inputs']],
            'files':[]}
        for record in artifacts:
            artifact=center.store.get(workspace,record['artifact_id'])
            bundle['files'].append({**record,'size_bytes':artifact.size_bytes,'blob_hash':artifact.blob_hash,'media_type':artifact.media_type,'availability':artifact.availability})
        with closing(center._connect()) as db:
            db.execute('INSERT OR IGNORE INTO manual_artifact_bundle VALUES(?,?,?,?,?)',
                       (bundle['bundle_id'],workspace,bundle['created_at'],'ready',encoded(bundle)))
        return bundle
