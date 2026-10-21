"""Native GPU admission and restart recovery with durable named-Job evidence.

No old PID is killed and no timeout proves quiescence. An unrecorded legacy
reservation becomes an explicit verification blocker, retaining the ledger.
"""
from __future__ import annotations

from dataclasses import asdict
import ctypes
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from ctypes import wintypes

from .gpu import GpuCoordinator, Owner, ResourceBusy, ReleaseUnconfirmed, StaleOwner
from .recovery import (atomic_json, checked, kernel, local_path, owner_active,
                       persist_owner, process_identity, recovery_evidence)
from .reader_features import require_features

NATIVE = {'ocr', 'classification'}
BLOCKED = '[GPU_NEEDS_VERIFICATION] '


class NativeResourceBlocked(ReleaseUnconfirmed):
    pass


def launch_record(directory, *, requester, launch_id, instance_id, job_name, broker_pid, broker_creation_time):
    directory = local_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if requester not in NATIVE or not job_name.startswith('Local\\ClearGpu-'):
        raise ValueError('Invalid native named Job declaration')
    record = {'format': 'clear-native-launch-job-v1', 'requester': requester,
              'launch_id': launch_id, 'instance_id': instance_id, 'job_name': job_name,
              'broker_pid': broker_pid, 'broker_creation_time': broker_creation_time,
              'kill_on_close': True, 'breakaway': False}
    require_features(directory.parent, 'agent-multimodal-v1')
    path = directory / (launch_id + '.json')
    atomic_json(path, record)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _launch(owner, path, expected_hash, *, membership_probe=None):
    path = local_path(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ReleaseUnconfirmed('Native launch Job declaration identity changed')
    record = json.loads(raw)
    if (record.get('format') != 'clear-native-launch-job-v1'
            or (record.get('requester'), record.get('launch_id'), record.get('instance_id'))
            != (owner.requester, owner.launch_id, owner.instance_id)
            or record.get('kill_on_close') is not True or record.get('breakaway') is not False
            or not str(record.get('job_name', '')).startswith('Local\\ClearGpu-')):
        raise ReleaseUnconfirmed('Native launch Job identity differs')
    if membership_probe is None:
        api = kernel()
        job = checked(api.OpenJobObjectW(4, False, record['job_name']))
        process = None
        try:
            process = checked(api.OpenProcess(0x1000, False, owner.pid))
            answer = wintypes.BOOL()
            checked(api.IsProcessInJob(process, job, ctypes.byref(answer)))
            member = bool(answer.value) and process_identity(owner.pid) == owner.creation_time
        finally:
            if process:
                api.CloseHandle(process)
            api.CloseHandle(job)
    else:
        member = membership_probe(owner, record['job_name'])
    if member is not True:
        raise ReleaseUnconfirmed('Native requester is outside its recorded owned Job')
    return record


def persist_request(data, request_id, owner, path, expected_hash, *, membership_probe=None):
    """Record ownership before the request can enter the durable FIFO queue."""
    data = local_path(data)
    GpuCoordinator._validate_request(request_id, owner.requester)
    record = _launch(owner, path, expected_hash, membership_probe=membership_probe)
    directory = local_path(data / 'gpu-requests')
    directory.mkdir(parents=True, exist_ok=True)
    require_features(data, 'agent-multimodal-v1')
    value = {'format': 'clear-native-gpu-request-v1', 'request_id': request_id,
             'owner': asdict(owner), 'job_name': record['job_name'], 'kill_on_close': True,
             'breakaway': False, 'launch_record': str(local_path(path)), 'launch_sha256': expected_hash}
    atomic_json(directory / (request_id + '.json'), value)
    return record


def _request_record(data, request_id, requester):
    path = local_path(data / 'gpu-requests' / (request_id + '.json'))
    if not path.is_file():
        raise NativeResourceBlocked(BLOCKED + '旧 GPU 排队请求缺少受管作业归属记录；原队列已保留，请核实旧实例。')
    record = json.loads(path.read_text('utf-8'))
    owner = Owner(**record['owner'])
    if (record.get('format') != 'clear-native-gpu-request-v1' or record.get('request_id') != request_id
            or owner.requester != requester or record.get('kill_on_close') is not True
            or record.get('breakaway') is not False):
        raise NativeResourceBlocked(BLOCKED + 'GPU 排队归属记录不匹配；原队列已保留。')
    launch = local_path(record['launch_record'])
    if hashlib.sha256(launch.read_bytes()).hexdigest() != record.get('launch_sha256'):
        raise NativeResourceBlocked(BLOCKED + 'GPU 原始启动记录已变化；原队列已保留。')
    declared = json.loads(launch.read_text('utf-8'))
    if (declared.get('format') != 'clear-native-launch-job-v1'
            or (declared.get('requester'), declared.get('launch_id'), declared.get('instance_id'), declared.get('job_name'))
            != (owner.requester, owner.launch_id, owner.instance_id, record.get('job_name'))
            or declared.get('kill_on_close') is not True or declared.get('breakaway') is not False):
        raise NativeResourceBlocked(BLOCKED + 'GPU 原始启动归属不匹配；原队列已保留。')
    return record, owner


def _evidence_record(record):
    return {**record, 'format': 'clear-gpu-owner-v1'}


def recover_native_resources(coordinator, data, **probes):
    """Reclaim only expired, witnessed native ownership; cancel only dead queues."""
    data = local_path(data)
    report = {'reclaimed': None, 'cancelled_pending': [], 'blockers': []}
    snapshot = coordinator.snapshot()
    if snapshot.owner and snapshot.owner.requester in NATIVE and snapshot.expires_at <= coordinator.clock():
        try:
            record, owner = _request_record(data, snapshot.owner_request_id, snapshot.owner.requester)
            if owner != snapshot.owner:
                raise NativeResourceBlocked(BLOCKED + 'GPU 租约与原始请求身份不匹配。')
            # The pre-enqueue record also covers a crash immediately after
            # grant, before persist_owner could save its generation receipt.
            evidence = recovery_evidence(_evidence_record(record), owner, **probes)
            coordinator.reclaim(snapshot.generation, witness=lambda actual: evidence if actual == owner else None)
            report['reclaimed'] = snapshot.owner_request_id
        except (OSError, ValueError, KeyError) as error:
            report['blockers'].append(str(error) if str(error).startswith(BLOCKED)
                                      else BLOCKED + '旧 GPU 持有者或子进程尚未核实：' + str(error))
    for pending in coordinator.pending_requests():
        if pending['requester'] not in NATIVE:
            continue
        try:
            record, owner = _request_record(data, pending['request_id'], pending['requester'])
            evidence = recovery_evidence(_evidence_record(record), owner, **probes)
            if evidence.reclaim_confirmed:
                coordinator.cancel_pending(pending['request_id'], pending['requester'])
                report['cancelled_pending'].append(pending['request_id'])
        except StaleOwner:
            pass  # It may have acquired the grant since the read-only snapshot.
        except (OSError, ValueError, KeyError) as error:
            report['blockers'].append(str(error) if str(error).startswith(BLOCKED)
                                      else BLOCKED + 'GPU 排队归属无法核实：' + str(error))
    if report['reclaimed'] or report['cancelled_pending']:
        directory = local_path(data / 'gpu-recovery-receipts')
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / (uuid.uuid4().hex + '.json'), {'format': 'clear-native-gpu-recovery-v1', **report})
    return report


def install(native_resources, *, app_id, phase=None):
    """Replace admission only; use the sealed Reservation and algorithm workers."""
    if app_id not in NATIVE:
        raise ValueError('Unsupported native resource adapter')
    original = native_resources.reserve

    def reserve(kind=None, cancelled=None, control=None):
        if not native_resources.platform_mode():
            return original(kind, cancelled) if app_id == 'ocr' else original(cancelled, control)
        cancelled = cancelled or threading.Event()
        identity = native_resources._identity()
        owner = Owner(app_id, os.environ['WORKBENCH_LAUNCH_ID'], os.environ['WORKBENCH_INSTANCE_ID'],
                      os.getpid(), identity)
        data = local_path(Path(os.environ['WORKBENCH_GPU_LEDGER']).parent)
        coordinator = GpuCoordinator(data / 'gpu.sqlite3')
        request_id = app_id + '-' + uuid.uuid4().hex
        launch = persist_request(data, request_id, owner, os.environ['WORKBENCH_NATIVE_LAUNCH_RECORD'],
                                 os.environ['WORKBENCH_NATIVE_LAUNCH_SHA256'])
        granted = False
        try:
            coordinator.request(request_id, app_id)
            while True:
                if cancelled.is_set():
                    raise native_resources.ResourceUnavailable('等待 GPU 时已取消')
                if control is not None:
                    control()
                try:
                    lease = coordinator.grant(request_id, owner,
                        owned=lambda actual: actual == owner and native_resources._identity() == identity)
                    granted = True
                    persist_owner(data / 'gpu-owners', lease, type('NativeJob', (), {'name': launch['job_name']})())
                    if phase:
                        phase('加载引擎')
                    return native_resources.Reservation(coordinator, lease, owner, identity)
                except ResourceBusy:
                    recovered = recover_native_resources(coordinator, data)
                    if recovered['blockers']:
                        raise NativeResourceBlocked(recovered['blockers'][0])
                    if phase:
                        phase('等待 GPU 资源')
                    cancelled.wait(.1)
        finally:
            if not granted:
                try:
                    coordinator.cancel_pending(request_id, app_id)
                except StaleOwner:
                    pass

    if app_id == 'ocr':
        native_resources.reserve = lambda kind, cancelled=None: reserve(kind, cancelled)
    else:
        native_resources.reserve = lambda cancelled=None, control=None: reserve(None, cancelled, control)


def install_ocr_service(service):
    """Expose real GPU waiting phases without changing native task identities."""
    original = service.create_app

    def create_app(*args, **kwargs):
        app = original(*args, **kwargs)
        from ocr_workbench import platform_resources
        queue = app.state.queue
        previous = None
        def phase(value):
            nonlocal previous
            task_id = queue.current
            if task_id and previous != (task_id, value):
                with queue.store.transaction() as db:
                    changed = db.execute("UPDATE tasks SET phase=? WHERE id=? AND status='running'", (value, task_id)).rowcount
                if changed:
                    previous = (task_id, value)
        install(platform_resources, app_id='ocr', phase=phase)
        return app
    service.create_app = create_app
