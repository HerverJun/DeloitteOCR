"""Narrow, explicit folder ingress; ordinary runtime reads stay inside DATA.

Single-user selection authorizes only one real local directory. The worker
copies its inputs before the normal image/model readers see them. Multi-user
requests can only select this project's already staged upload subtree.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid

from .paths import require_path
from .store import now

MAX_BYTES = 100 * 1024 * 1024


def _install():
    try:
        from packages.runtime import current_install
    except ModuleNotFoundError as error:
        if error.name not in {'packages', 'packages.runtime'} or os.environ.get('CLEAR_INSTALL_CONFIG'):
            raise
        return None
    return current_install()


def _no_links(path):
    for member in (path, *path.parents):
        if member.is_symlink() or (hasattr(member, 'is_junction') and member.is_junction()):
            raise ValueError('导入目录和图片不能经过符号链接或目录联接')


def _directory(value, config, settings, project_id):
    # Check the requested spelling before resolve can hide a junction target.
    requested = Path(value).expanduser().absolute()
    if '..' in requested.parts:
        raise ValueError('请输入不含上级跳转的文件夹完整路径')
    _no_links(requested)
    if config is None:
        path = require_path(requested, 'Folder import source', role='read')
    else:
        from packages.runtime.config import _local
        _local(requested, 'Folder import source')
        try:
            path = requested.resolve(strict=True)
        except OSError as error:
            raise ValueError('目录不存在或无法读取，请填写此服务器上的完整路径') from error
        _local(path, 'Folder import source')
        drives = config.policy.get('allowed_drives')
        if drives and path.drive.upper() not in drives:
            raise ValueError('导入目录超出本机登记的盘符范围')
        protected = [config.check(config.roots[key], 'Protected import boundary', role='read')
            for key in ('program', 'assets', 'runtimes', 'tools', 'secrets', 'postgres', 'backup')]
        if any(path.is_relative_to(root) or root.is_relative_to(path) for root in protected):
            raise ValueError('不能将程序、模型、凭据、数据库、备份目录或其上级目录作为图片来源')
        if config.mode == 'multi':
            owned = require_path(settings.data_dir / 'uploads' / project_id, 'Project upload scope', role='read')
            if not path.is_relative_to(owned):
                raise ValueError('多人模式不能按服务器路径读取其他目录；请使用“上传图片”，或选择当前项目已上传的目录')
    if not path.is_dir():
        raise ValueError('目录不存在，请填写此服务器上的完整路径')
    _no_links(path)
    return path


def authorize_directory(value, settings, project_id):
    config = _install()
    path = _directory(value, config, settings, project_id)
    if config is None:
        return path, None
    info = path.stat()
    return path, {'schema_version': 1, 'mode': 'copy-to-data', 'installation_id': config.installation_id,
        'project_id': project_id, 'root': str(path), 'directory_id': [info.st_dev, info.st_ino]}


def validate_job_directory(store, job):
    config = _install()
    path = _directory(job['params']['path'], config, store.settings, job['project_id'])
    if config is not None:
        authorized = job['params'].get('ingress') or {}
        info = path.stat()
        expected = {'schema_version': 1, 'mode': 'copy-to-data', 'installation_id': config.installation_id,
            'project_id': job['project_id'], 'root': str(path), 'directory_id': [info.st_dev, info.st_ino]}
        if authorized != expected:
            raise ValueError('文件夹导入授权已变化或属于旧安装；请重新选择文件夹提交，不会自动读取新目录')
    return path, config is not None


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_selected_input(store, job, source, root, control):
    """The sole managed external-read boundary for explicit folder selection."""
    current_root, managed = validate_job_directory(store, job)
    source = Path(source).absolute()
    _no_links(source)
    if not managed or current_root != root or not source.is_relative_to(root):
        raise ValueError('文件不属于本次明确选择的导入目录')
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError('仅能导入不超过 100 MiB 的普通图片文件')
    staging = require_path(store.settings.data_dir / 'uploads/imports/.staging', 'Folder ingress staging')
    staging.mkdir(parents=True, exist_ok=True)
    temporary = require_path(staging / (uuid.uuid4().hex + '.new'), 'Folder ingress temporary file')
    total, hasher = 0, hashlib.sha256()
    try:
        # Bypass the generic read-root helper only for this validated selection.
        # Both passes use one handle; replacement of the named original is also
        # detected before the copied bytes become an accepted business asset.
        with source.open('rb') as incoming, temporary.open('xb') as outgoing:
            before = os.fstat(incoming.fileno())
            if _signature(before) != _signature(info):
                raise ValueError('原图在开始复制前已变化，请重新提交')
            while chunk := incoming.read(1024 * 1024):
                control()
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError('图片文件超过 100 MiB')
                outgoing.write(chunk)
                hasher.update(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            incoming.seek(0)
            if (hashlib.file_digest(incoming, 'sha256').hexdigest() != hasher.hexdigest()
                    or _signature(os.fstat(incoming.fileno())) != _signature(before)):
                raise ValueError('原图在复制期间已变化，当前副本未采用')
        _no_links(source)
        if _signature(source.stat()) != _signature(before) or total != before.st_size:
            raise ValueError('原图在复制期间已变化，当前副本未采用')
        digest = hasher.hexdigest()
        target = require_path(store.settings.data_dir / 'uploads/imports' / digest / source.name, 'Imported image copy')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != total or _digest(target) != digest:
                raise ValueError('既有导入副本已变化，未覆盖原文件')
        else:
            os.replace(temporary, target)
        return target, {'source_path': str(source), 'source_root': str(root),
            'source_relative': source.relative_to(root).as_posix(), 'original_name': source.name,
            'sha256': digest, 'size_bytes': total}
    finally:
        temporary.unlink(missing_ok=True)


def record_source(store, job, asset_id, source_id, provenance):
    # Keep original source spelling and hash as immutable provenance. Relocation
    # changes the managed assets.path pointer, not this original-source record.
    with store.connect(write=True) as db:
        store.assert_owner(db, job['id'], job['lease_token'])
        db.execute('''CREATE TABLE IF NOT EXISTS folder_import_sources(
            asset_id TEXT NOT NULL REFERENCES assets(id),source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,source_root TEXT NOT NULL,source_relative TEXT NOT NULL,
            original_name TEXT NOT NULL,source_id TEXT NOT NULL,size_bytes INTEGER NOT NULL,
            first_job_id TEXT NOT NULL REFERENCES jobs(id),created_at TEXT NOT NULL,
            PRIMARY KEY(asset_id,source_path,source_sha256))''')
        db.execute('INSERT OR IGNORE INTO folder_import_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
            (asset_id, provenance['source_path'], provenance['sha256'], provenance['source_root'],
             provenance['source_relative'], provenance['original_name'], source_id,
             provenance['size_bytes'], job['id'], now()))
