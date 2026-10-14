"""Bounded real ASGI uploads, durable offsets and atomic asset receipts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from packages.contracts.uploads import (
    AUDIO_UPLOAD_CHUNK_BYTES as CHUNK, AUDIO_UPLOAD_MAX_BYTES as MAXIMUM,
    AudioUploadBinding, AudioUploadStatus, UPLOAD_MAX_BYTES,
)
from workbench_platform.assets.audio_uploads import AudioUploadStore
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.routes import router
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.store import WorkspaceStore

LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}
BINARY = {**LOCAL, 'content-type': 'application/octet-stream'}


@pytest.fixture
def audio(tmp_path):
    uploads, blobs = tmp_path / 'uploads', tmp_path / 'blobs'
    uploads.mkdir(); blobs.mkdir()
    registry = AssetRegistry(tmp_path / 'assets.sqlite3', blobs, {'uploads': uploads})
    spaces = WorkspaceStore(tmp_path / 'workspaces.sqlite3')
    spaces.create_workspace('音频', workspace_id='workspaceA')
    spaces.create_workspace('其他', workspace_id='workspaceB')
    app = FastAPI()
    app.include_router(router)
    app.state.asset_library = (registry, AssetSetStore(registry), uploads)
    app.state.workspace_store = spaces
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        yield client, registry, uploads, blobs, app


def binding(body=b'fixed audio bytes', request_id='audioA', name='录音 甲.wav'):
    return AudioUploadBinding(schema_version='1.0', workspace_id='workspaceA', request_id=request_id,
        name=name, size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def url(value, suffix=''):
    return f'/api/platform/v1/workspaces/{value.workspace_id}/assets/audio-uploads/{value.request_id}{suffix}'


def begin(client, value):
    return client.post(url(value), headers=LOCAL, json=value.model_dump(mode='json'))


def append(client, value, body=b'fixed audio bytes', offset=0, chunk_hash=None):
    return client.put(url(value, '/chunks'), headers=BINARY,
        params={'offset': offset, 'sha256': chunk_hash or hashlib.sha256(body).hexdigest()}, content=body)


def control(client, value, action):
    return client.post(url(value, '/' + action), headers=LOCAL, json={})


def test_real_sequential_chunks_lost_reply_replay_restart_and_fixed_receipt(audio):
    client, registry, uploads, blobs, app = audio
    first, tail = b'a' * CHUNK, b'last chunk'
    value = binding(first + tail)
    assert client.get(url(value)).status_code == 404
    assert begin(client, value).json()['next_offset'] == 0
    accepted = append(client, value, first)
    assert accepted.status_code == 200 and accepted.json()['next_offset'] == CHUNK
    # A caller that did not receive accepted's response reads the durable offset.
    reloaded = AssetRegistry(registry.db_path, blobs, {'uploads': uploads})
    app.state.asset_library = (reloaded, AssetSetStore(reloaded), uploads)
    assert client.get(url(value)).json() == accepted.json()
    assert append(client, value, first).json() == accepted.json()
    assert append(client, value, tail, CHUNK).json()['next_offset'] == len(first + tail)
    final = control(client, value, 'complete')
    assert final.status_code == 200, final.text
    status = AudioUploadStatus.model_validate(final.json())
    assert status.status == 'committed' and status.receipt.asset.size_bytes == len(first + tail)
    assert control(client, value, 'complete').json() == final.json()
    assert control(client, value, 'cancel').json() == final.json()
    assert begin(client, value).json() == final.json()
    assert reloaded.list('workspaceA')[1] == 1
    content = client.get(f'/api/platform/v1/workspaces/workspaceA/assets/{status.receipt.asset.asset_id}/content?revision=1')
    assert content.content == first + tail
    source = reloaded.source('workspaceA', status.receipt.asset.asset_id)
    assert (uploads / source['relative_path']).stat().st_ino == reloaded._blob(value.sha256).stat().st_ino
    assert not list(uploads.glob('.audio-*'))


def test_wrong_binding_chunk_hash_gaps_and_duplicate_change_preserve_confirmed_bytes(audio):
    client, registry, _, _, _ = audio
    first, tail = b'b' * CHUNK, b'tail'
    value = binding(first + tail)
    assert begin(client, value).status_code == 201
    altered = value.model_copy(update={'sha256': 'f' * 64})
    assert begin(client, altered).status_code == 409
    for data, offset, digest in [(first, 0, 'f' * 64), (b'', 0, None), (tail, CHUNK, None), (tail, 0, None)]:
        assert append(client, value, data, offset, digest).status_code == 409
        assert client.get(url(value)).json()['next_offset'] == 0
    assert append(client, value, first).status_code == 200
    assert append(client, value, b'x' * CHUNK).status_code == 409
    assert client.get(url(value)).json()['next_offset'] == CHUNK
    assert control(client, value, 'complete').status_code == 409
    assert registry.list('workspaceA')[1] == 0


def test_chunk_size_limit_and_legacy_limit_remain_independent(audio):
    client, registry, _, _, _ = audio
    value = binding(b'a' * (CHUNK + 1))
    begin(client, value)
    assert append(client, value, b'x' * (CHUNK + 1)).status_code == 413
    legacy = '/api/platform/v1/workspaces/workspaceA/assets/uploads/legacyA'
    response = client.post(legacy, headers=BINARY, params={'name': '旧音频.wav', 'sha256': 'a' * 64,
                           'size_bytes': UPLOAD_MAX_BYTES + 1}, content=b'')
    assert response.status_code == 413
    assert registry.list('workspaceA')[1] == 0


@pytest.mark.parametrize('change', [{'size_bytes': 0}, {'size_bytes': MAXIMUM + 1}, {'name': '大文件.pdf'},
    {'name': '../录音.wav'}, {'name': 'NUL.wav'}, {'size_bytes': True}, {'extra': 'field'}])
def test_empty_oversize_non_audio_and_unsafe_metadata_have_no_upload(audio, change):
    client, registry, uploads, _, _ = audio
    value = binding()
    result = client.post(url(value), headers=LOCAL, json={**value.model_dump(), **change})
    assert result.status_code in {400, 413}
    assert client.get(url(value)).status_code == 404 and registry.list('workspaceA')[1] == 0
    assert not list(uploads.glob('.audio-*'))


def test_four_gib_metadata_boundary_without_allocating_four_gib(audio):
    client, _, uploads, _, _ = audio
    value = binding().model_copy(update={'size_bytes': MAXIMUM})
    assert begin(client, value).status_code == 201
    assert sum(path.stat().st_size for path in uploads.glob('.audio-*/body')) == 0
    assert control(client, value, 'cancel').json()['status'] == 'cancelled'
    assert not list(uploads.glob('.audio-*'))


def test_insufficient_space_on_reserve_and_mid_upload_preserves_identity(audio, monkeypatch):
    client, registry, _, _, _ = audio
    value = binding()
    import workbench_platform.assets.audio_uploads as module
    real_disk_usage = module.shutil.disk_usage
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    assert begin(client, value).status_code == 507
    assert client.get(url(value)).status_code == 404
    monkeypatch.setattr(module.shutil, 'disk_usage', real_disk_usage)
    assert begin(client, value).status_code == 201
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    assert append(client, value).status_code == 507
    assert client.get(url(value)).json()['next_offset'] == 0
    assert registry.list('workspaceA')[1] == 0
    monkeypatch.setattr(module.shutil, 'disk_usage', real_disk_usage)
    assert append(client, value).status_code == 200
    assert control(client, value, 'complete').json()['status'] == 'committed'


def test_restart_discards_unacknowledged_tail_and_rejects_missing_acknowledged_bytes(audio):
    client, registry, _, _, _ = audio
    value = binding()
    begin(client, value)
    store = AudioUploadStore(registry)
    _, staged, _, _ = store._paths(value)
    staged.write_bytes(b'crashed write before SQLite commit')
    assert store.status('workspaceA', value.request_id).next_offset == 0
    assert staged.stat().st_size == 0
    append(client, value)
    staged.write_bytes(b'')
    status = store.status('workspaceA', value.request_id)
    assert status.status == 'rejected' and status.error.details['effect'] == 'no_registration'
    assert registry.list('workspaceA')[1] == 0
    assert control(client, value, 'cancel').json()['status'] == 'cancelled'


def test_final_full_hash_detects_persisted_corruption_before_asset_registration(audio):
    client, registry, _, _, _ = audio
    value = binding()
    begin(client, value); append(client, value)
    _, staged, _, _ = AudioUploadStore(registry)._paths(value)
    staged.write_bytes(b'x' * value.size_bytes)
    result = control(client, value, 'complete')
    assert result.json()['status'] == 'rejected'
    assert result.json()['error']['details']['effect'] == 'no_registration'
    assert registry.list('workspaceA')[1] == 0


def test_concurrent_exact_chunk_and_completion_replay_register_only_one_asset(audio):
    _, registry, _, _, _ = audio
    store = AudioUploadStore(registry)
    value = binding()
    store.begin(value)
    data = b'fixed audio bytes'
    with ThreadPoolExecutor(max_workers=4) as pool:
        offsets = list(pool.map(lambda _: store.append('workspaceA', value.request_id, 0, value.sha256, data).next_offset, range(4)))
        completed = list(pool.map(lambda _: store.complete('workspaceA', value.request_id), range(4)))
    assert offsets == [len(data)] * 4
    assert all(row.status == 'committed' and row.receipt == completed[0].receipt for row in completed)
    assert registry.list('workspaceA')[1] == 1


def test_cancel_keeps_other_uploads_and_committed_assets_and_requires_loopback_origin(audio):
    client, registry, uploads, _, _ = audio
    first, second = binding(request_id='first'), binding(request_id='second')
    for value in (first, second):
        begin(client, value); append(client, value)
    rejected = client.post(url(first, '/cancel'), headers={'host': LOCAL['host'], 'origin': 'https://external.invalid'}, json={})
    assert rejected.status_code == 403
    assert control(client, first, 'cancel').json()['status'] == 'cancelled'
    assert append(client, first).json()['status'] == 'cancelled'
    assert control(client, first, 'complete').json()['status'] == 'cancelled'
    assert client.get(url(second)).json()['status'] == 'uploading'
    assert len(list(uploads.glob('.audio-*'))) == 1
    assert control(client, second, 'complete').json()['status'] == 'committed'
    assert registry.list('workspaceA')[1] == 1
    other = client.get(url(second).replace('workspaceA', 'workspaceB'))
    assert other.status_code == 404


def test_commit_failure_cannot_leave_registration_without_receipt(audio, monkeypatch):
    client, registry, uploads, _, _ = audio
    value = binding()
    begin(client, value); append(client, value)
    store = registry._audio_upload_store
    save = store._save
    def fail_receipt(db, status):
        if status.status == 'committed':
            raise sqlite3.OperationalError('simulated failure before commit')
        save(db, status)
    monkeypatch.setattr(store, '_save', fail_receipt)
    assert control(client, value, 'complete').status_code == 503
    assert registry.list('workspaceA')[1] == 0
    assert not registry._blob(value.sha256).exists() and not list(uploads.glob('ua*'))
    assert client.get(url(value)).json()['status'] == 'uploading'
    monkeypatch.setattr(store, '_save', save)
    assert control(client, value, 'complete').json()['status'] == 'committed'
    assert registry.list('workspaceA')[1] == 1


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows open-handle delete sharing semantics')
def test_windows_cancel_during_real_open_hash_is_durable_and_prevents_registration(audio, monkeypatch):
    client, registry, uploads, _, _ = audio
    data = b'h' * (2 * CHUNK + 17)
    value = binding(data, request_id='windowsHashCancel')
    assert begin(client, value).status_code == 201
    for offset in range(0, len(data), CHUNK):
        assert append(client, value, data[offset:offset + CHUNK], offset).status_code == 200
    store = registry._audio_upload_store
    _, staged, _, _ = store._paths(value)
    entered, release = threading.Event(), threading.Event()
    original_open = Path.open
    consumed = []

    class HeldReader:
        """Keep a real, non-DELETE-shared Windows reader open while cancelling."""
        def __init__(self, stream): self.stream = stream
        def __enter__(self): self.stream.__enter__(); return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            content = self.stream.read(size)
            consumed.append(len(content))
            if not entered.is_set():
                entered.set()
                assert release.wait(15), 'Cancellation did not release the controlled hash reader'
            return content

    def open_with_real_held_reader(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return HeldReader(stream) if path == staged and mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', open_with_real_held_reader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.complete, 'workspaceA', value.request_id)
        try:
            assert entered.wait(15), 'Complete did not start reading the real file'
            # This is the OS failure that used to roll back cancellation. No
            # unlink mocking is involved: the hash reader holds the actual file.
            with pytest.raises(PermissionError):
                staged.unlink()
            cancelled = control(client, value, 'cancel')
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()['status'] == 'cancelled'
            assert staged.is_file(), 'Windows must defer cleanup until the hash handle closes'
            reloaded = AudioUploadStore(registry)
            assert reloaded.status('workspaceA', value.request_id).status == 'cancelled'
            assert registry.list('workspaceA')[1] == 0
        finally:
            release.set()
        assert pending.result(timeout=15).status == 'cancelled'
    assert sum(consumed) <= CHUNK < len(data)
    assert not staged.exists() and not list(uploads.glob('.audio-*'))
    assert registry.list('workspaceA')[1] == 0 and not registry._blob(value.sha256).exists()
    assert control(client, value, 'complete').json()['status'] == 'cancelled'


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows open-handle delete sharing semantics')
def test_windows_cancel_cleanup_retries_after_registry_restart_and_reader_release(audio):
    client, registry, uploads, blobs, _ = audio
    value = binding(request_id='windowsCleanup')
    begin(client, value); append(client, value)
    _, staged, _, _ = registry._audio_upload_store._paths(value)
    with staged.open('rb'):
        with pytest.raises(PermissionError):
            staged.unlink()
        cancelled = control(client, value, 'cancel')
        assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
        assert staged.exists()
        restored_registry = AssetRegistry(registry.db_path, blobs, {'uploads': uploads})
        restored = AudioUploadStore(restored_registry)
        assert restored.status('workspaceA', value.request_id).status == 'cancelled'
        assert staged.exists()
    assert restored.status('workspaceA', value.request_id).status == 'cancelled'
    assert not staged.exists() and not list(uploads.glob('.audio-*'))
    assert registry.list('workspaceA')[1] == 0
