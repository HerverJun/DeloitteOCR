"""Durable platform references to native adaptive image-to-Excel batches.

The native batch, rather than its first OCR tasks, owns execution and recovery.
Only bounded pages and exception summaries cross the Agent boundary.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Literal
import zipfile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from packages.contracts.agent import AgentModel, Identity
from workbench_platform.assets.routes import json_body, library
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.jobs.native_ocr import OcrUnavailable, OcrResultConflict, MAX_EXPORT
from workbench_platform.jobs.ocr_handoff import _json, _utc

router = APIRouter(prefix='/api/platform/v1/workspaces/{workspace_id}/ocr/screen-batches', tags=['screen-batches'])
TERMINAL = {'completed', 'succeeded', 'partial', 'failed', 'cancelled', 'needs_review'}


def _omit_policy_schema_defaults(schema):
    # These are API compatibility values, not the native measured route.
    # Omitted fields are never sent, so advertising them as defaults would
    # encourage a controller to explicitly overwrite frozen native settings.
    for value in schema.get('properties',{}).values():
        value.pop('default',None)


class ScreenPolicy(AgentModel):
    model_config=ConfigDict(json_schema_extra=_omit_policy_schema_defaults)
    primary_engine: Identity = 'auto'
    fallback_engines: list[Identity] | None = Field(default=None, max_length=3)
    max_upgrades: int = Field(default=2, ge=0, le=2)
    mode: Literal['auto', 'accuracy'] = 'auto'
    group_size: int = Field(default=16, ge=1, le=64)
    budget_seconds: int | None = Field(default=None, ge=10, le=1800)
    layout_evidence: bool | None = None


class ScreenSubmit(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)
    request_id: Identity
    policy: ScreenPolicy = Field(default_factory=ScreenPolicy)


class CellEdit(AgentModel):
    table_index: int = Field(ge=0)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    before: str = Field(max_length=16000)
    after: str = Field(max_length=16000)


class ScreenAction(AgentModel):
    action: Literal['pause', 'resume', 'cancel', 'retry_failed', 'review', 'upgrade']
    request_id: Identity
    version_ids: list[Identity] | None = Field(default=None, min_length=1, max_length=1000)
    result_id: Identity | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    candidate_id: Identity | None = None
    state: Literal['accepted', 'retain', 'deferred'] | None = None
    edits: list[CellEdit] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def valid_review(self):
        if self.action == 'review' and (not self.version_ids or len(self.version_ids) != 1
                or not self.result_id or self.expected_revision is None or not self.state):
            raise ValueError('Review requires one pinned image and result revision')
        if self.action != 'review' and (self.result_id or self.expected_revision is not None or self.candidate_id or self.state or self.edits):
            raise ValueError('Review fields are only valid for review')
        if self.action=='upgrade' and not self.version_ids:
            raise ValueError('提高精度必须明确选择图片')
        return self


class ScreenExport(AgentModel):
    include_pending_review: bool = False
    numeric_columns: dict[str, dict[str, list[int]]] | None = None


class ScreenBatches:
    def __init__(self, path: Path, handoff, artifacts):
        self.path, self.handoff, self.artifacts = path, handoff, artifacts
        self.lock = threading.RLock()
        with closing(self._connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS screen_batch_refs(
                workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                handoff_id TEXT NOT NULL,project_id TEXT NOT NULL,batch_id TEXT,
                source_json TEXT NOT NULL,policy_json TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id,request_id),UNIQUE(workspace_id,batch_id));
                CREATE TABLE IF NOT EXISTS screen_batch_actions(
                workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,batch_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,PRIMARY KEY(workspace_id,request_id));
                CREATE TABLE IF NOT EXISTS screen_batch_exports(
                workspace_id TEXT NOT NULL,batch_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                bundle_id TEXT NOT NULL,PRIMARY KEY(workspace_id,batch_id,payload_hash));
                CREATE TABLE IF NOT EXISTS screen_batch_subsets(
                workspace_id TEXT NOT NULL,batch_id TEXT NOT NULL,asset_set_id TEXT NOT NULL,
                asset_set_revision INTEGER NOT NULL,PRIMARY KEY(workspace_id,batch_id));
                CREATE TABLE IF NOT EXISTS screen_batch_policy_receipts(
                workspace_id TEXT NOT NULL,batch_id TEXT NOT NULL,policy_hash TEXT NOT NULL,
                policy_json TEXT NOT NULL,PRIMARY KEY(workspace_id,batch_id));''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _row(self, workspace, batch_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND batch_id=?', (workspace, batch_id)).fetchone()
        if row is None:
            raise KeyError('Screen batch not found')
        return row

    def _adapter(self, workspace, row):
        original = self.handoff._row(workspace, row['handoff_id'])
        context = self.handoff._context(workspace, original['link_id'])
        if context.module_project_id != row['project_id']:
            raise AssetSetConflict('原生项目关联已变化，保留原批次身份')
        fixed = self.fixed(workspace,row)
        for member in fixed.items:
            asset = self.handoff.registry.get(workspace, member.asset_id)
            if asset.revision != member.asset_revision or asset.blob_hash != member.blob_hash:
                raise AssetSetConflict('原图片版本已变化，请建立新的输入集合')
        return self.handoff._adapter(context), original, fixed

    def fixed(self,workspace,row):
        with closing(self._connect()) as db:
            subset=db.execute('SELECT * FROM screen_batch_subsets WHERE workspace_id=? AND batch_id=?',
                              (workspace,row['batch_id'])).fetchone()
        original=self.handoff._row(workspace,row['handoff_id'])
        return self.handoff.sets.get(workspace,subset['asset_set_id'] if subset else original['asset_set_id'],
            subset['asset_set_revision'] if subset else original['asset_set_revision'])

    def submit(self, workspace, args: ScreenSubmit, *, allow_new=True):
        body = args.model_dump(mode='json',exclude_none=True)
        # Native frozen defaults own omitted options. Pydantic display defaults
        # must not silently override a measured native policy on submission.
        body['policy']=args.policy.model_dump(mode='json',exclude_unset=True,exclude_none=True)
        identity = hashlib.sha256(_json(body).encode()).hexdigest()
        with self.lock:
            with closing(self._connect()) as db:
                row = db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND request_id=?', (workspace, args.request_id)).fetchone()
            if row and row['payload_hash'] != identity:
                raise AssetSetConflict('同一批次请求不能替换资料或处理策略')
            if row is None:
                if not allow_new:
                    return None
                imported = self.handoff.begin(workspace, args.link_id, args.asset_set_id, args.asset_set_revision)
                if imported['state'] != 'completed':
                    raise AssetSetConflict('资料导入尚未完成，请继续原交接')
                if any(not item.get('target_version_id') for item in imported['items']):
                    raise ValueError('拍屏批次只接受图片，PDF 使用文档页处理')
                context = self.handoff._context(workspace, args.link_id)
                sources = [{'input_version_id': item['target_version_id'], 'image_id': item['target_asset_id'],
                    'asset_id': item['asset_id'], 'item_id': item['item_id'], 'name': item['name']} for item in imported['items']]
                with closing(self._connect()) as db:
                    db.execute('INSERT INTO screen_batch_refs VALUES(?,?,?,?,?,?,?,?,?)',
                        (workspace, args.request_id, identity, imported['handoff_id'], context.module_project_id,
                         None, _json(sources), _json(body['policy']), _utc()))
                    row = db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND request_id=?', (workspace, args.request_id)).fetchone()
            adapter, _, _ = self._adapter(workspace, row)
            if not row['batch_id']:
                previous = adapter._request('GET', f'/api/projects/{row["project_id"]}/screen-batch-requests/{args.request_id}')
                batch_id = previous.get('batch_id') if previous.get('submitted') else None
                if not batch_id:
                    if not allow_new:
                        return None
                    value = adapter._request('POST', f'/api/projects/{row["project_id"]}/screen-batches',
                        {'version_ids': [item['input_version_id'] for item in json.loads(row['source_json'])],
                         'request_id': args.request_id, 'policy': json.loads(row['policy_json'])},
                        max_body=128000, result_conflict=True)
                    batch_id = value.get('batch_id')
                if not isinstance(batch_id, str) or not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', batch_id):
                    raise OcrUnavailable()
                with closing(self._connect()) as db:
                    db.execute('UPDATE screen_batch_refs SET batch_id=? WHERE workspace_id=? AND request_id=?', (batch_id, workspace, args.request_id))
            else:
                batch_id = row['batch_id']
            return self.status(workspace, batch_id, limit=10)

    def status(self, workspace, batch_id, *, offset=0, limit=30, status=None):
        row = self._row(workspace, batch_id)
        adapter, original, fixed = self._adapter(workspace, row)
        suffix = f'?offset={offset}&limit={limit}' + ('&status=' + status if status else '')
        value = adapter._request('GET', f'/api/projects/{row["project_id"]}/screen-batches/{batch_id}{suffix}')
        if value.get('batch_id') != batch_id or value.get('project_id') != row['project_id']:
            raise OcrUnavailable()
        counts = value.get('counts', {})
        sources = json.loads(row['source_json'])
        if counts.get('total') != len(sources) or any(type(counts.get(key)) is not int or counts[key] < 0 for key in ('total','completed','needs_review','failed','cancelled','pending')):
            raise OcrUnavailable()
        if sum(counts[key] for key in ('completed','needs_review','failed','cancelled','pending')) != counts['total']:
            raise OcrUnavailable()
        source_map = {item['input_version_id']: item for item in sources}
        items = []
        for item in value.get('items', []):
            source = source_map.get(item.get('input_version_id'))
            if not source or item.get('image_id') != source['image_id']:
                raise OcrUnavailable()
            items.append({**item, **source})
        if len(items) > limit:
            raise OcrUnavailable()
        policy=value.get('policy')
        if not isinstance(policy,dict):raise OcrUnavailable()
        policy_json=_json(policy);policy_hash=hashlib.sha256(policy_json.encode()).hexdigest()
        with self.lock,closing(self._connect()) as db:
            recorded=db.execute('SELECT policy_hash FROM screen_batch_policy_receipts WHERE workspace_id=? AND batch_id=?',
                (workspace,batch_id)).fetchone()
            if recorded and recorded['policy_hash']!=policy_hash:
                raise OcrUnavailable()
            if recorded is None:
                db.execute('INSERT INTO screen_batch_policy_receipts VALUES(?,?,?,?)',
                    (workspace,batch_id,policy_hash,policy_json))
        return {**value, 'items': items, 'filtered_total':value.get('total_items',value.get('filtered_total',counts['total'])), 'workspace_id': workspace, 'link_id': original['link_id'],
            'handoff_id': row['handoff_id'], 'asset_set_id': fixed.asset_set_id,
            'asset_set_revision': fixed.revision, 'offset': offset, 'limit': limit,
            'resolved_policy_sha256':policy_hash,
            'capabilities': ['resume','cancel'] if value['status'] == 'paused' else
                ['pause','cancel'] if counts['pending'] else ['retry_failed'] if counts['failed'] else [],
            'stage': value.get('stage', 'screen_batch'), 'progress_done': counts['total']-counts['pending'], 'progress_total': counts['total']}

    def detail(self, workspace, batch_id, version_id):
        row = self._row(workspace, batch_id)
        adapter, _, fixed = self._adapter(workspace, row)
        source = next((item for item in json.loads(row['source_json']) if item['input_version_id'] == version_id), None)
        if not source:
            raise KeyError('Image not in batch')
        value = adapter._request('GET', f'/api/projects/{row["project_id"]}/screen-batches/{batch_id}/items/{version_id}')
        if value.get('input_version_id') != version_id or value.get('image_id') != source['image_id']:
            raise OcrUnavailable()
        member = next(item for item in fixed.items if item.asset_id == source['asset_id'])
        value['original_url'] = f'/api/platform/v1/workspaces/{workspace}/assets/{source["asset_id"]}/content?revision={member.asset_revision}'
        value['tables']=(value.get('result') or {}).get('edited',{}).get('tables',[])
        value['candidates']=[{'result_id':candidate.get('id'),'engine':candidate.get('engine'),
            'tables':(candidate.get('raw') or {}).get('tables',[])} for candidate in value.get('candidate_results',[])][:4]
        value['preparation']=value.get('preparation') or {}
        for image in value['preparation'].get('prepared_images',[]):
            image['url']=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches/{batch_id}/items/{version_id}/files/{image["id"]}'
            image.pop('path',None)
        return {**value, **source}

    def image(self,workspace,batch_id,version_id,file_id):
        row=self._row(workspace,batch_id)
        adapter,_,_=self._adapter(workspace,row)
        detail=self.detail(workspace,batch_id,version_id)
        if file_id not in {image['id'] for image in detail.get('preparation',{}).get('prepared_images',[])}:
            raise KeyError('Prepared image not in this batch')
        return adapter.screen_image(row['project_id'],batch_id,version_id,file_id)

    def action(self, workspace, batch_id, args: ScreenAction):
        row = self._row(workspace, batch_id)
        adapter, _, fixed = self._adapter(workspace, row)
        body = args.model_dump(mode='json', exclude_none=True)
        if args.version_ids and not set(args.version_ids) <= {item['input_version_id'] for item in json.loads(row['source_json'])}:
            raise ValueError('操作图片不属于当前批次')
        identity = hashlib.sha256(_json(body).encode()).hexdigest()
        with self.lock, closing(self._connect()) as db:
            old = db.execute('SELECT * FROM screen_batch_actions WHERE workspace_id=? AND request_id=?', (workspace, args.request_id)).fetchone()
            if old and (old['payload_hash'] != identity or old['batch_id'] != batch_id):
                raise AssetSetConflict('操作请求身份与原参数不同')
            if not old:
                db.execute('INSERT INTO screen_batch_actions VALUES(?,?,?,?)', (workspace,args.request_id,batch_id,identity))
            receipt=adapter._request('POST', f'/api/projects/{row["project_id"]}/screen-batches/{batch_id}/action', body, max_body=128000, result_conflict=True)
            if args.action=='upgrade':
                new_id=receipt.get('batch_id')
                if not isinstance(new_id,str) or not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',new_id) or new_id==batch_id or receipt.get('parent_batch_id')!=batch_id:
                    raise OcrUnavailable()
                sources=[item for item in json.loads(row['source_json']) if item['input_version_id'] in args.version_ids]
                db.execute('INSERT OR IGNORE INTO screen_batch_refs VALUES(?,?,?,?,?,?,?,?,?)',
                    (workspace,args.request_id,identity,row['handoff_id'],row['project_id'],new_id,_json(sources),_json(receipt['policy']),_utc()))
                saved=db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND request_id=?',(workspace,args.request_id)).fetchone()
                if not saved or saved['batch_id']!=new_id or saved['payload_hash']!=identity or saved['source_json']!=_json(sources):
                    raise AssetSetConflict('新尝试回执与原请求不一致，请核实原身份')
                source_ids={source['asset_id'] for source in sources}
                members=[member for member in fixed.items if member.asset_id in source_ids]
                subset_id='s'+hashlib.sha256(_json([fixed.asset_set_id,fixed.revision,[member.item_id for member in members]]).encode()).hexdigest()[:40]
                subset=self.handoff.sets.create(workspace,members,asset_set_id=subset_id,
                    parents={member.item_id:(fixed.asset_set_id,fixed.revision,member.item_id) for member in members})
                db.execute('INSERT OR IGNORE INTO screen_batch_subsets VALUES(?,?,?,?)',
                           (workspace,new_id,subset.asset_set_id,subset.revision))
                batch_id=new_id
        return self.status(workspace, batch_id)

    def export(self, workspace, batch_id, args: ScreenExport):
        row = self._row(workspace, batch_id)
        adapter, original, fixed = self._adapter(workspace, row)
        summary = self.status(workspace, batch_id, limit=1)
        data = adapter.screen_export(row['project_id'], batch_id, args.model_dump(mode='json', exclude_none=True))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            entries = archive.infolist()
            if len(entries) > 1001 or sum(item.file_size for item in entries) > MAX_EXPORT or len({item.filename for item in entries}) != len(entries):
                raise OcrUnavailable()
            items = manifest.get('items', [])
            sources = json.loads(row['source_json'])
            by_version = {item['input_version_id']: item for item in sources}
            if len(items) != len(sources) or {item.get('input_version_id') for item in items} != set(by_version):
                raise OcrUnavailable()
            filenames = []
            for item in items:
                source = by_version[item['input_version_id']]
                if item.get('image_id') != source['image_id']:
                    raise OcrUnavailable()
                name = item.get('file_name')
                if name:
                    if '/' in name or '\\' in name or not name.lower().endswith('.xlsx') or name not in archive.namelist():
                        raise OcrUnavailable()
                    # Each promised spreadsheet must be a real OOXML archive.
                    content = archive.read(name)
                    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
                        if '[Content_Types].xml' not in workbook.namelist() or 'xl/workbook.xml' not in workbook.namelist():
                            raise OcrUnavailable()
                    filenames.append(name)
            if not filenames or len(set(filenames)) != len(filenames) or manifest.get('success_files') != len(filenames):
                raise OcrUnavailable()
        counts = summary['counts']
        # Export timestamps and ZIP container metadata change between native
        # reads. Pinned result identities/revisions and export options own the
        # semantic snapshot; a retry must recover its existing managed bundle.
        export_key=hashlib.sha256(_json({'items':items,'policy':manifest.get('policy'),
            'options':args.model_dump(mode='json',exclude_none=True),'input_manifest_hash':fixed.manifest_hash}).encode()).hexdigest()
        with self.lock,closing(self._connect()) as db:
            previous=db.execute('SELECT bundle_id FROM screen_batch_exports WHERE workspace_id=? AND batch_id=? AND payload_hash=?',
                (workspace,batch_id,export_key)).fetchone()
            if previous:
                return self.artifacts.get(workspace,previous['bundle_id'])
        succeeded = counts['completed']+counts['needs_review']
        state = 'running' if counts['pending'] else 'partial' if counts['failed'] or counts['cancelled'] else 'succeeded'
        snapshot = {'workspace_id':workspace,'handoff_id':batch_id,'project_id':row['project_id'],
            'project_name':'逐图 Excel 批次','platform_job_id':'j'+hashlib.sha256(batch_id.encode()).hexdigest()[:32],
            'native_task_ids':[batch_id], 'engines':[summary.get('policy',{}).get('primary_engine','auto')],
            'input_set_id':fixed.asset_set_id,'input_set_revision':fixed.revision,'input_manifest_hash':fixed.manifest_hash,
            'inputs':[{'name':source['name'],'asset_id':source['asset_id'],'source_locator':next(member.source_locator.model_dump(mode='json') for member in fixed.items if member.asset_id==source['asset_id'])} for source in sources],
            'items':items,'export':data,'media_type':'application/zip',
            'platform_job':{'instance_id':adapter.context.instance_id,'operation_id':row['request_id'],
                'input_manifest_hash':fixed.manifest_hash,'native_status':summary['status'],'status':state,'stage':'screen_batch',
                'counts':{'total':counts['total'],'succeeded':succeeded,'failed':counts['failed'],'cancelled':counts['cancelled'],'pending':counts['pending']},'last_event_seq':0,'updated_at':_utc()}}
        with self.lock,closing(self._connect()) as db:
            previous=db.execute('SELECT bundle_id FROM screen_batch_exports WHERE workspace_id=? AND batch_id=? AND payload_hash=?',
                (workspace,batch_id,export_key)).fetchone()
            if previous:
                return self.artifacts.get(workspace,previous['bundle_id'])
            bundle=self.artifacts.ocr.publish(workspace,batch_id,snapshot=snapshot)
            db.execute('INSERT INTO screen_batch_exports VALUES(?,?,?,?)',(workspace,batch_id,export_key,bundle['bundle_id']))
            return bundle


def configure_screen_batches(app):
    root = os.environ.get('WORKBENCH_DATA_DIR')
    app.state.screen_batches = ScreenBatches(Path(root)/'screen-batches.sqlite3',app.state.ocr_handoff,app.state.artifact_center) if root and app.state.ocr_handoff and app.state.artifact_center else None


def service(request, workspace, write=False):
    library(request, workspace, write=write)
    value = getattr(request.app.state, 'screen_batches', None)
    if value is None:
        raise HTTPException(503,'批量表格处理暂不可用')
    return value


async def call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn,*args,**kwargs)
    except KeyError:
        raise HTTPException(404,'批次或图片不存在') from None
    except (AssetSetConflict,OcrResultConflict):
        raise HTTPException(409,'资料、结果修订或原请求已变化，请核对后继续') from None
    except OcrUnavailable:
        raise HTTPException(503,'OCR 服务响应待核实，保留原请求身份后重试') from None
    except (ValueError,zipfile.BadZipFile):
        raise HTTPException(400,'批次参数或导出内容无效') from None


async def parse(request, model):
    try:
        return model.model_validate(await json_body(request,128000))
    except ValueError:
        raise HTTPException(400,'批次参数无效') from None


@router.post('',status_code=201)
async def submit(workspace_id:str,request:Request):
    value=service(request,workspace_id,True)
    return await call(value.submit,workspace_id,await parse(request,ScreenSubmit))


@router.get('/{batch_id}')
async def status(workspace_id:str,batch_id:Identity,request:Request,offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=50),status:Literal['completed','needs_review','failed','cancelled','pending']|None=None):
    return await call(service(request,workspace_id).status,workspace_id,batch_id,offset=offset,limit=limit,status=status)


@router.get('/{batch_id}/items/{version_id}')
async def detail(workspace_id:str,batch_id:Identity,version_id:Identity,request:Request):
    return await call(service(request,workspace_id).detail,workspace_id,batch_id,version_id)


@router.post('/{batch_id}/action')
async def action(workspace_id:str,batch_id:Identity,request:Request):
    return await call(service(request,workspace_id,True).action,workspace_id,batch_id,await parse(request,ScreenAction))


@router.get('/{batch_id}/items/{version_id}/files/{file_id}')
async def image(workspace_id:str,batch_id:Identity,version_id:Identity,file_id:Identity,request:Request):
    data,media=await call(service(request,workspace_id).image,workspace_id,batch_id,version_id,file_id)
    return Response(content=data,media_type=media,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.post('/{batch_id}/export',status_code=201)
async def export(workspace_id:str,batch_id:Identity,request:Request):
    return await call(service(request,workspace_id,True).export,workspace_id,batch_id,await parse(request,ScreenExport))
