"""Verified, request-idempotent asset staging shared by portal and Agent."""
from contextlib import closing
import hashlib
import os
from pathlib import Path

from workbench_platform.data_api.service import DataError, identity
from workbench_platform.assets.registry import AssetUnavailable


def register_manual_source(registry, workspace_id, name, path, sha256, size):
    """Keep uploaded bytes in the project library, reusing identical sources."""
    with closing(registry._connect()) as db:
        matches = db.execute('SELECT asset_id FROM registered_asset WHERE workspace_id=? '
            'AND original_name=? AND blob_hash=? AND size_bytes=? ORDER BY created_at,asset_id',
            (workspace_id, name, sha256, size)).fetchall()
    asset = None
    for match in matches:
        candidate = registry.get(workspace_id, match['asset_id'])
        try:
            with registry.stage_verified(workspace_id, candidate.asset_id,
                    revision=candidate.revision, blob_hash=sha256):
                pass
        except AssetUnavailable:
            continue
        asset = candidate
        break
    if asset is None:
        root = registry.roots.get('data')
        if root is None:
            raise DataError('数据上传的项目资料目录尚未配置', 503)
        try:
            relative = Path(path).resolve().relative_to(root).as_posix()
        except ValueError:
            raise DataError('上传来源不属于数据资料目录', 409) from None
        media = {'.csv': 'text/csv', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                 '.parquet': 'application/vnd.apache.parquet'}[Path(name).suffix.lower()]
        asset = registry.register(workspace_id, 'data', relative, original_name=name,
                                  media_type=media, storage_mode='managed')
    if (asset.blob_hash, asset.size_bytes) != (sha256, size):
        raise DataError('上传内容与资料来源不一致', 409)
    return {'workspace_id': workspace_id, 'asset_id': asset.asset_id,
            'revision': asset.revision, 'blob_hash': asset.blob_hash}


def stage_asset(data, registry, workspace_id, asset_id, revision, blob_hash, request_key):
    identity(request_key)
    asset = registry.get(workspace_id, asset_id)
    if (asset.revision, asset.blob_hash) != (revision, blob_hash):
        raise DataError('资料版本发生变化', 409)
    suffix = Path(asset.original_name).suffix.lower()
    if suffix not in {'.csv', '.xlsx', '.parquet'} or asset.size_bytes > 64 * 1024 * 1024:
        raise DataError('资料格式或大小不受数据工作台支持')
    return asset, suffix


def stage_asset_upload(data, registry, workspace_id, session_id, asset_id, revision, blob_hash, request_key):
    session = data.get(session_id)
    if session['workspace_id'] is not None and session['workspace_id'] != workspace_id:
        raise DataError('作业属于其他项目', 409)
    asset, suffix = stage_asset(data, registry, workspace_id, asset_id, revision, blob_hash, request_key)
    upload_id = 'u' + hashlib.sha256(request_key.encode()).hexdigest()[:40]
    path = data.session_dir(session_id) / 'uploads' / (upload_id + suffix)
    # The deterministic native upload ID is recoverable even if the platform
    # process dies before its Agent operation receipt is committed.
    with data.lock:
        with closing(data.db()) as db:
            old = db.execute('SELECT * FROM uploads WHERE id=?', (upload_id,)).fetchone()
        if old:
            expected = (session_id, asset.original_name, asset.blob_hash, asset.size_bytes,
                        workspace_id, asset_id, revision, blob_hash)
            actual = tuple(old[key] for key in ('session_id', 'name', 'sha256', 'size',
                           'source_workspace_id', 'source_asset_id', 'source_asset_revision', 'source_blob_hash'))
            if actual != expected:
                raise DataError('上传请求身份与原资料不一致', 409)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != blob_hash:
                raise DataError('已上传的资料内容不可核实', 409)
            return {'id': upload_id, 'name': asset.original_name, 'format': suffix.lstrip('.'),
                    'sha256': blob_hash, 'size': asset.size_bytes, 'reused': True}
        temporary = path.with_suffix(suffix + '.tmp')
        try:
            with registry.stage_verified(workspace_id, asset_id, revision=revision, blob_hash=blob_hash) as source:
                digest, size = hashlib.sha256(), 0
                with source.open('rb') as original, temporary.open('wb') as target:
                    while chunk := original.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if (digest.hexdigest(), size) != (blob_hash, asset.size_bytes):
                    raise AssetUnavailable('Asset content changed')
            os.replace(temporary, path)
            return data.upload(session_id, upload_id, asset.original_name, suffix, path, blob_hash, size,
                    source_asset={'workspace_id': workspace_id, 'asset_id': asset_id,
                                  'revision': revision, 'blob_hash': blob_hash}) | {'reused': False}
        finally:
            temporary.unlink(missing_ok=True)


def stage_set_member(data, registry, sets, workspace_id, session_id, set_id, set_revision, item_id, request_key):
    asset_set = sets.get(workspace_id, set_id, set_revision)
    item = next((entry for entry in asset_set.items if entry.item_id == item_id), None)
    if item is None:
        raise DataError('资料集成员不存在', 404)
    return stage_asset_upload(data, registry, workspace_id, session_id, item.asset_id,
                             item.asset_revision, item.blob_hash, request_key)
