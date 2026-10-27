"""Sequential, resumable audio uploads using the asset registry's transaction.

Only an acknowledged offset is durable. A restart truncates an unacknowledged
tail before accepting another chunk. Asset registration and the final receipt
commit together, and publication uses hard links rather than a second 4 GiB copy.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import mimetypes
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from packages.contracts.models import Error
from packages.contracts.uploads import (
    AUDIO_UPLOAD_CHUNK_BYTES, AudioUploadBinding, AudioUploadReceipt,
    AudioUploadStatus, upload_payload_hash,
)
from workbench_platform.assets.registry import (
    AssetChanged, AssetRegistry, AssetUnavailable, UploadConflict, UploadMissing, _digest, _utc,
)

SPACE_RESERVE_BYTES = 256 * 1024 * 1024
_INITIALIZE = threading.Lock()
router = APIRouter(prefix='/audio-uploads', tags=['audio-uploads'])


class AudioUploadError(ValueError):
    def __init__(self, message: str, *, status: int = 409, code: str = 'INTEGRITY_MISMATCH'):
        super().__init__(message)
        self.status, self.code = status, code


class _UploadStopped(Exception):
    def __init__(self, status: AudioUploadStatus):
        self.status = status


def _checked(path: Path) -> Path:
    from packages.runtime import require_path
    try:
        path = require_path(path, 'Audio upload storage', role='write')
    except ValueError as exc:
        raise AssetUnavailable(str(exc)) from exc
    if any(parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction())
           for parent in (path, *path.parents)):
        raise AssetUnavailable('Linked audio upload storage is not allowed')
    return path


class AudioUploadStore:
    def __init__(self, registry: AssetRegistry):
        self.registry = registry
        self.root = _checked(registry.roots['uploads'])
        _checked(registry.db_path)
        _checked(registry.blob_root)
        with closing(registry._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS asset_audio_upload (
                    workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, request_id));
                CREATE TABLE IF NOT EXISTS asset_audio_chunk (
                    workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    offset_bytes INTEGER NOT NULL, size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, request_id, offset_bytes));
            ''')

    def _paths(self, binding):
        token = hashlib.sha256((binding.workspace_id + '\0' + binding.request_id).encode()).hexdigest()[:32]
        relative = 'ua' + token + '/' + binding.name
        source_id = 's' + hashlib.sha256(('uploads\0' + os.path.normcase(relative)).encode()).hexdigest()[:32]
        incoming = _checked(self.root / ('.audio-' + token))
        return incoming, _checked(incoming / 'body'), relative, source_id

    @staticmethod
    def _read(db, workspace_id, request_id, binding=None):
        row = db.execute('SELECT * FROM asset_audio_upload WHERE workspace_id=? AND request_id=?',
                         (workspace_id, request_id)).fetchone()
        if row is None:
            raise UploadMissing('Audio upload request is not registered')
        status = AudioUploadStatus.model_validate_json(row['body'])
        if binding is not None and (status.binding != binding or row['payload_hash'] != upload_payload_hash(binding)):
            raise UploadConflict('Audio upload request belongs to another file')
        return status

    @staticmethod
    def _save(db, status):
        db.execute('UPDATE asset_audio_upload SET body=? WHERE workspace_id=? AND request_id=?',
                   (status.model_dump_json(), status.binding.workspace_id, status.binding.request_id))

    def _reject(self, db, status, message):
        _, _, _, source_id = self._paths(status.binding)
        if db.execute('SELECT 1 FROM registered_asset WHERE workspace_id=? AND source_id=?',
                      (status.binding.workspace_id, source_id)).fetchone():
            raise AssetChanged('Registration exists without a matching upload receipt')
        error = Error(schema_version='1.0', code='INTEGRITY_MISMATCH', message=message,
                      request_id=status.binding.request_id, retryable=False,
                      details={'effect': 'no_registration'})
        rejected = AudioUploadStatus(**{**status.model_dump(), 'status': 'rejected',
                                       'error': error, 'updated_at': _utc()})
        self._save(db, rejected)
        return rejected

    def _reconcile(self, db, status):
        if status.status != 'uploading':
            return status
        _, body, _, _ = self._paths(status.binding)
        if not body.is_file() or body.stat().st_size < status.next_offset:
            return self._reject(db, status, '已接收的分块缺失，请取消后用原文件重新上传')
        if body.stat().st_size > status.next_offset:
            # fsync may have finished before the SQLite transaction committed.
            with body.open('r+b') as stream:
                stream.truncate(status.next_offset)
                stream.flush()
                os.fsync(stream.fileno())
        return status

    def _cleanup_terminal(self, status):
        """Cancellation is durable even while a Windows reader denies deletion.

        Cleanup is best effort and retryable from GET, cancel replay, and the
        hash reader's exit. Only this request's temporary link is removed.
        """
        if status.status not in {'committed', 'cancelled'}:
            return
        directory, body, _, _ = self._paths(status.binding)
        try:
            body.unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()
        except OSError:
            pass

    def _hash_upload(self, status, body):
        """Hash with bounded cancellation latency and no registry writer lock."""
        binding = status.binding
        with body.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AssetUnavailable('Audio upload is not a regular file')
            digest, size, checked = hashlib.sha256(), 0, -AUDIO_UPLOAD_CHUNK_BYTES
            while True:
                if size - checked >= AUDIO_UPLOAD_CHUNK_BYTES:
                    with closing(self.registry._connect()) as db:
                        current = self._read(db, binding.workspace_id, binding.request_id)
                    if current.status != 'uploading':
                        raise _UploadStopped(current)
                    checked = size
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != after.st_size:
                raise AssetChanged('Audio upload changed during verification')
            return (digest.hexdigest(), size), after

    def _space(self, db, additional=0):
        outstanding = sum(
            row[0] for row in db.execute("SELECT json_extract(body,'$.binding.size_bytes') - "
                "json_extract(body,'$.next_offset') FROM asset_audio_upload "
                "WHERE json_extract(body,'$.status')='uploading'")
        )
        if shutil.disk_usage(self.root).free < outstanding + additional + SPACE_RESERVE_BYTES:
            raise AudioUploadError('F 盘空间不足，请保留原文件与上传记录，释放空间后恢复',
                                   status=507, code='RESOURCE_BUSY')

    def begin(self, binding: AudioUploadBinding):
        binding = AudioUploadBinding.model_validate(binding)
        with closing(self.registry._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                try:
                    status = self._read(db, binding.workspace_id, binding.request_id, binding)
                    status = self._reconcile(db, status)
                except UploadMissing:
                    self._space(db, binding.size_bytes)
                    from workbench_platform.resources.reader_features import require_features
                    require_features(self.registry.db_path.parent,'manual-long-audio-v1')
                    directory, body, _, _ = self._paths(binding)
                    directory.mkdir(exist_ok=True)
                    # An interrupted initial reservation has no acknowledged bytes.
                    with body.open('wb') as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                    timestamp = _utc()
                    status = AudioUploadStatus(schema_version='1.0', binding=binding,
                        payload_hash=upload_payload_hash(binding), status='uploading', next_offset=0,
                        created_at=timestamp, updated_at=timestamp)
                    db.execute('INSERT INTO asset_audio_upload VALUES(?,?,?,?)',
                        (binding.workspace_id, binding.request_id, status.payload_hash, status.model_dump_json()))
                db.execute('COMMIT')
                return status
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def status(self, workspace_id, request_id):
        with closing(self.registry._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                status = self._reconcile(db, self._read(db, workspace_id, request_id))
                db.execute('COMMIT')
                self._cleanup_terminal(status)
                return status
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def append(self, workspace_id, request_id, offset: int, chunk_hash: str, content: bytes):
        if not content or len(content) > AUDIO_UPLOAD_CHUNK_BYTES or hashlib.sha256(content).hexdigest() != chunk_hash:
            raise AudioUploadError('分块大小或 SHA256 不一致；先查询已确认位置再重试原分块')
        with closing(self.registry._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                status = self._reconcile(db, self._read(db, workspace_id, request_id))
                if status.status in {'cancelled', 'rejected'}:
                    db.execute('COMMIT')
                    return status
                existing = db.execute('SELECT size_bytes,sha256 FROM asset_audio_chunk WHERE '
                    'workspace_id=? AND request_id=? AND offset_bytes=?', (workspace_id, request_id, offset)).fetchone()
                if existing is not None:
                    if (existing['size_bytes'], existing['sha256']) != (len(content), chunk_hash):
                        raise AudioUploadError('此位置已绑定其他分块，不能覆盖已确认内容')
                    db.execute('COMMIT')
                    return status
                if status.status != 'uploading' or offset != status.next_offset:
                    raise AudioUploadError('分块位置不连续；请查询已确认位置后继续', code='REVISION_CONFLICT')
                if len(content) != min(AUDIO_UPLOAD_CHUNK_BYTES, status.binding.size_bytes - offset):
                    raise AudioUploadError('每块必须为 8 MiB，只有最后一块可以更短')
                self._space(db)
                _, body, _, _ = self._paths(status.binding)
                with body.open('r+b') as stream:
                    stream.seek(offset)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                db.execute('INSERT INTO asset_audio_chunk VALUES(?,?,?,?,?)',
                           (workspace_id, request_id, offset, len(content), chunk_hash))
                status = AudioUploadStatus(**{**status.model_dump(), 'next_offset': offset + len(content),
                                              'updated_at': _utc()})
                self._save(db, status)
                db.execute('COMMIT')
                return status
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def complete(self, workspace_id, request_id):
        # A long hash pass must not keep the SQLite writer lock, so cancellations
        # and unrelated uploads remain responsive. Re-read state before publish.
        status = self.status(workspace_id, request_id)
        if status.status != 'uploading':
            return status
        if status.next_offset != status.binding.size_bytes:
            raise AudioUploadError('尚有分块未上传；请从已确认位置继续', code='REVISION_CONFLICT')
        directory, body, relative, source_id = self._paths(status.binding)
        try:
            identity, verified_stat = self._hash_upload(status, body)
        except _UploadStopped as stopped:
            # _hash_upload has already closed its real Windows file handle.
            self._cleanup_terminal(stopped.status)
            return stopped.status
        except (OSError, AssetUnavailable):
            reconciled = self.status(workspace_id, request_id)
            if reconciled.status != 'uploading':
                return reconciled
            raise
        with closing(self.registry._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            created_source = created_blob = False
            blob_was_referenced = True
            source = None
            try:
                status = self._reconcile(db, self._read(db, workspace_id, request_id))
                if status.status != 'uploading':
                    db.execute('COMMIT')
                    self._cleanup_terminal(status)
                    return status
                current_stat = body.stat()
                unchanged = (current_stat.st_dev, current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns) == (
                    verified_stat.st_dev, verified_stat.st_ino, verified_stat.st_size, verified_stat.st_mtime_ns)
                if not unchanged or identity != (status.binding.sha256, status.binding.size_bytes):
                    status = self._reject(db, status, '完整文件 SHA256 校验失败；请取消后重新上传原文件')
                    db.execute('COMMIT')
                    return status
                binding = status.binding
                blob = _checked(self.registry._blob(binding.sha256))
                created_blob = not blob.exists()
                blob_was_referenced = db.execute('SELECT 1 FROM registered_asset WHERE blob_hash=? LIMIT 1',
                                                (binding.sha256,)).fetchone() is not None
                self.registry._install_blob(body, binding.sha256, binding.size_bytes)
                source = _checked(self.root / relative)
                source.parent.mkdir(exist_ok=True)
                if source.exists():
                    if _digest(source) != identity:
                        raise AssetChanged('Fixed audio upload source changed')
                else:
                    os.link(blob, source)
                    created_source = True
                media_type = mimetypes.guess_type(binding.name)[0] or 'audio/octet-stream'
                row = self.registry._register_in(db, workspace_id, source_id, 'uploads', relative, binding.name,
                    media_type, 'managed', binding.sha256, binding.size_bytes, allow_update=False)
                receipt = AudioUploadReceipt(schema_version='1.0', binding=binding, payload_hash=status.payload_hash,
                    asset=self.registry._asset(row), created_at=_utc())
                status = AudioUploadStatus(**{**status.model_dump(), 'status': 'committed', 'receipt': receipt,
                                              'error': None, 'updated_at': receipt.created_at})
                self._save(db, status)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    if created_source and source is not None:
                        source.unlink(missing_ok=True)
                        try:
                            source.parent.rmdir()
                        except OSError:
                            pass
                    if created_blob and not blob_was_referenced:
                        # Remove only this transaction's new link while the
                        # registry writer lock still excludes another commit.
                        self.registry._blob(status.binding.sha256).unlink(missing_ok=True)
                    db.execute('ROLLBACK')
                raise
        self._cleanup_terminal(status)
        return status

    def cancel(self, workspace_id, request_id):
        with closing(self.registry._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                status = self._read(db, workspace_id, request_id)
                if status.status != 'committed':
                    status = AudioUploadStatus(**{**status.model_dump(), 'status': 'cancelled', 'error': None,
                                                  'updated_at': _utc()})
                    self._save(db, status)
                db.execute('COMMIT')
                self._cleanup_terminal(status)
                return status
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise


def _store(request, workspace_id, request_id, *, write=False):
    from workbench_platform.assets.routes import library
    from workbench_platform.workspaces.routes import ID
    if not ID.fullmatch(request_id):
        raise AudioUploadError('上传请求身份无效', status=400, code='VALIDATION_FAILED')
    registry, _, _ = library(request, workspace_id, write=write)
    with _INITIALIZE:
        if not hasattr(registry, '_audio_upload_store'):
            registry._audio_upload_store = AudioUploadStore(registry)
    return registry._audio_upload_store


def _failure(error, request_id):
    from fastapi import HTTPException
    from workbench_platform.assets.routes import upload_error
    if isinstance(error, HTTPException):
        return upload_error(error.status_code, 'NOT_FOUND' if error.status_code == 404 else 'APP_UNAVAILABLE', str(error.detail), request_id)
    if isinstance(error, UploadMissing):
        return upload_error(404, 'NOT_FOUND', '尚无此音频上传记录；请保留原请求身份', request_id)
    if isinstance(error, UploadConflict):
        return upload_error(409, 'IDEMPOTENCY_CONFLICT', '此请求已经绑定其他文件，不能替换内容', request_id)
    if isinstance(error, AudioUploadError):
        return upload_error(error.status, error.code, str(error), request_id, retryable=error.status == 507)
    if isinstance(error, (ValidationError, ValueError)) and not isinstance(error, AssetUnavailable):
        return upload_error(400, 'VALIDATION_FAILED', '音频上传参数、文件名或大小无效', request_id)
    return upload_error(503, 'APP_UNAVAILABLE', '上传状态尚需核实；请保留原文件，先查询原请求后恢复', request_id, retryable=True)


@router.post('/{request_id}', response_model=AudioUploadStatus, status_code=201)
async def begin_audio_upload(workspace_id: str, request_id: str, request: Request):
    from workbench_platform.assets.routes import json_body
    try:
        store = _store(request, workspace_id, request_id, write=True)
        if request.query_params:
            raise AudioUploadError('不支持重复或额外查询参数', status=400, code='VALIDATION_FAILED')
        binding = AudioUploadBinding.model_validate(await json_body(request))
        if binding.workspace_id != workspace_id or binding.request_id != request_id:
            raise AudioUploadError('路径与上传身份不一致', status=400, code='VALIDATION_FAILED')
        status = await run_in_threadpool(store.begin, binding)
        return JSONResponse(status_code=200 if status.status == 'committed' else 201, content=status.model_dump(mode='json'))
    except Exception as error:
        return _failure(error, request_id)


@router.get('/{request_id}', response_model=AudioUploadStatus)
async def get_audio_upload(workspace_id: str, request_id: str, request: Request):
    try:
        store = _store(request, workspace_id, request_id)
        if request.query_params:
            raise AudioUploadError('查询参数无效', status=400, code='VALIDATION_FAILED')
        return await run_in_threadpool(store.status, workspace_id, request_id)
    except Exception as error:
        return _failure(error, request_id)


@router.put('/{request_id}/chunks', response_model=AudioUploadStatus)
async def append_audio_chunk(workspace_id: str, request_id: str, request: Request):
    try:
        store = _store(request, workspace_id, request_id, write=True)
        query = request.query_params
        if (set(query) != {'offset', 'sha256'} or any(len(query.getlist(key)) != 1 for key in query)
                or not query['offset'].isascii() or not query['offset'].isdigit()
                or str(int(query['offset'])) != query['offset']
                or len(query['sha256']) != 64 or any(char not in '0123456789abcdef' for char in query['sha256'])):
            raise AudioUploadError('分块位置与哈希参数无效', status=400, code='VALIDATION_FAILED')
        if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/octet-stream':
            raise AudioUploadError('请提交原音频分块二进制', status=400, code='VALIDATION_FAILED')
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > AUDIO_UPLOAD_CHUNK_BYTES:
                raise AudioUploadError('单个分块超过 8 MiB', status=413, code='VALIDATION_FAILED')
            content.extend(chunk)
        return await run_in_threadpool(store.append, workspace_id, request_id,
                                       int(query['offset']), query['sha256'], bytes(content))
    except Exception as error:
        return _failure(error, request_id)


async def _control(workspace_id, request_id, request, action):
    from workbench_platform.assets.routes import json_body
    try:
        store = _store(request, workspace_id, request_id, write=True)
        if request.query_params or await json_body(request) != {}:
            raise AudioUploadError('请提交空 JSON 控制参数', status=400, code='VALIDATION_FAILED')
        return await run_in_threadpool(getattr(store, action), workspace_id, request_id)
    except Exception as error:
        return _failure(error, request_id)


@router.post('/{request_id}/complete', response_model=AudioUploadStatus)
async def complete_audio_upload(workspace_id: str, request_id: str, request: Request):
    return await _control(workspace_id, request_id, request, 'complete')


@router.post('/{request_id}/cancel', response_model=AudioUploadStatus)
async def cancel_audio_upload(workspace_id: str, request_id: str, request: Request):
    return await _control(workspace_id, request_id, request, 'cancel')
