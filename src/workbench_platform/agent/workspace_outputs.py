"""Publish actual general-workspace files through the existing artifact store."""
from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path

from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from packages.contracts.models import SourceLocator
from workbench_platform.supervisor.processes import require_f_path
from workbench_platform.artifacts.native_exports import publish, lookup_result
from .general import enabled
from .registry import ToolSpec
from .store import canonical, digest

SKIP = {'inputs', '.clear', '.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache'}


class PublishOutputs(AgentModel):
    paths: list[str] = Field(default_factory=list, max_length=512,
        description='Optional relative output paths. Empty publishes the task files except inputs and caches.')


def output_files(root, paths=()):
    root = require_f_path(root)
    found = []
    if paths:
        candidates = [root / name for name in paths]
    else:
        candidates = []
        for current, directories, files in os.walk(root, followlinks=False):
            parent = Path(current)
            directories[:] = sorted(name for name in directories if name not in SKIP
                and not (parent / name).is_symlink() and not (parent / name).is_junction())
            candidates.extend(parent / name for name in sorted(files))
    total = 0
    for path in candidates:
        if path.is_symlink() or path.is_junction():
            raise ValueError('Output links must be resolved to an authorized ordinary file')
        path = require_f_path(path)
        if not path.is_relative_to(root) or any(part in SKIP for part in path.relative_to(root).parts):
            raise ValueError('Only files in the current task workspace can be published')
        if not path.is_file():
            raise ValueError('The requested output file does not exist')
        total += path.stat().st_size
        found.append(path)
        if len(found) > 512 or total > 512 * 1024 * 1024:
            raise ValueError('Package large output collections into an archive before publishing')
    return found


def execute(args, ctx):
    state = ctx.services.agent.graph.load(ctx.run_id)
    if not enabled(state) or not state.get('working_directory'):
        return AgentToolResult(status='unsupported', summary='当前任务没有通用文件工作目录').model_dump()
    root = require_f_path(Path(state['working_directory']))
    paths = output_files(root, args.paths)
    if not paths:
        return AgentToolResult(status='success', summary='当前没有新增的任务输出文件').model_dump()
    files, manifest = {}, []
    for path in paths:
        ctx.current()
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('An output changed during publication; wait for its writer and retry')
        relative = path.relative_to(root).as_posix()
        name = relative.replace('/', '__')
        if len(name) > 220 or name in files or name == 'clear-output-files.json':
            name = digest(relative)[:12] + '-' + path.name[-190:]
        media_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        files[name] = content, media_type
        manifest.append({'path': relative, 'filename': name, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
    files['clear-output-files.json'] = canonical(manifest).encode('utf-8'), 'application/json'
    inputs, sources = [], []
    for item in state.get('workspace_inputs', []):
        if item['asset_id'] not in ctx.selected_assets:
            continue
        inputs.append({'asset_id': item['asset_id'], 'revision': item['revision'], 'blob_hash': item['sha256']})
        sources.append(SourceLocator(schema_version='1.0', kind='file', asset_id=item['asset_id'],
            asset_revision=item['revision'], blob_hash=item['sha256'], precision='exact').model_dump(mode='json'))
    bundle = publish(ctx, '任务文件', files, inputs, sources, 'general-workspace-outputs-v1')
    return AgentToolResult(status='success', summary=f'已保存并固定{len(manifest)}个实际任务文件，可读取和下载',
        data={'bundle': bundle, 'files': manifest}, artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id,
            object_id=bundle['bundle_id'], revision=1)]).model_dump(mode='json')


async def automatic(runtime, state, request_id):
    if not state.get('working_directory'):
        return None
    root = Path(state['working_directory'])
    baseline = state.get('workspace_baseline', {})
    changed = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in output_files(root)}
    changed = {path: sha for path, sha in changed.items() if baseline.get(path) != sha}
    if not changed:
        return None
    # Repeated finalization reuses the same operation and fixed output bundle.
    # A real file change creates a new identity, even within the same run.
    return await runtime.registry.dispatch({'call_id': 'auto_files_' + digest(changed)[:32],
        'tool': 'workspace_publish_outputs', 'arguments': {'paths': sorted(changed)}}, runtime.context(state))


def register(registry):
    registry.register(ToolSpec('workspace_publish_outputs',
        '保存任务工作目录中的实际输出文件并提供下载；自动收尾也会保存。可指定相对paths；原件、缓存和依赖不作为输出。',
        PublishOutputs, execute, effect='derive', lookup_operation=lookup_result, idempotent=True,
        input_kinds=('file',), output_kinds=('artifact',)))
