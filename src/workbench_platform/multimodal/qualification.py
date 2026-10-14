"""Local engineering GPU qualification; bundled CPU receipts cannot enable CUDA."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from workbench_platform.resources.recovery import atomic_json, local_path

GPU_KINDS = frozenset({'audio', 'catnet'})
FORMAT = 'clear-local-gpu-qualification-v1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def gpu_inventory(*, run=subprocess.run):
    executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/nvidia-smi.exe'
    if not executable.is_file():
        raise ValueError('本机 NVIDIA 驱动查询工具不可用；GPU 资格待验证')
    result = run([str(executable), '--query-gpu=index,uuid,driver_version,memory.total', '--format=csv,noheader,nounits'],
                 capture_output=True, text=True, encoding='utf-8', timeout=15,
                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('本机 NVIDIA 驱动查询失败；GPU 资格待验证')
    rows = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(',')]
        if len(fields) != 4:
            raise ValueError('NVIDIA 驱动查询返回无效数据')
        index, identifier, driver, memory = fields
        rows.append({'index': int(index), 'uuid': identifier, 'driver': driver, 'memory_mib': int(memory)})
    return rows


class Qualification:
    def __init__(self, path, *, inventory=gpu_inventory):
        self.path = local_path(path)
        self.inventory = inventory
        self.cache = {}
        self.lock = threading.RLock()
        self.inventory_cache = None
        self.inventory_at = 0

    def file_hash(self, path):
        path = Path(path)
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        with self.lock:
            if key not in self.cache:
                with path.open('rb') as stream:
                    self.cache[key] = hashlib.file_digest(stream, 'sha256').hexdigest()
            return self.cache[key]

    def binding(self, kind, runtime, source_files, resources, *, device_index=0):
        runtime = local_path(runtime)
        if kind not in GPU_KINDS:
            raise ValueError('本阶段仅为音频与 CAT-Net 开放 GPU 资格')
        # Hash installed inference libraries, including Python glue and DLLs.
        # Stat-keyed content cache avoids re-reading large binaries on every UI poll.
        libraries = {}
        for path in sorted(runtime.parent.rglob('*')):
            if (path.is_file() and path.suffix.lower() in {'.py', '.pyd', '.dll', '.zip', '.json', '._pth'}
                    and '__pycache__' not in path.parts):
                libraries[path.relative_to(runtime.parent).as_posix()] = self.file_hash(path)
        with self.lock:
            if self.inventory_cache is None or time.monotonic() - self.inventory_at > 10:
                self.inventory_cache, self.inventory_at = self.inventory(), time.monotonic()
            device = next((value for value in self.inventory_cache if value['index'] == device_index), None)
        if device is None:
            raise ValueError('已选择的 NVIDIA GPU 不存在')
        return {'kind': kind, 'runtime_executable': self.file_hash(runtime),
                'runtime_libraries': fingerprint(libraries),
                'program': {Path(path).name: self.file_hash(path) for path in source_files},
                'resources': resources, 'device': device}

    def ready(self, kind, binding, *, peak_target_mib=12288):
        try:
            record = json.loads(self.path.read_text('utf-8'))
            receipt = record['items'][kind]
            evidence = receipt['evidence']
            if record.get('format') != FORMAT or receipt.get('binding') != binding:
                return False
            return self.valid_evidence(evidence, binding, peak_target_mib)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def valid_evidence(evidence, binding, peak_target_mib):
        peak = evidence.get('peak_memory_mib')
        if binding.get('kind') == 'audio':
            execution=evidence.get('execution',{})
            devices=execution.get('chunk_devices',{})
            if (type(execution.get('fresh_inference_chunks')) is not int or execution['fresh_inference_chunks']<=0
                    or execution.get('device_execution')!='fresh_inference' or not devices
                    or any(set(value)!={'asr','vad','punctuation','speaker'}
                           or any(not str(device).startswith('cuda') for device in value.values()) for value in devices.values())):
                return False
        return (evidence.get('succeeded') is True and evidence.get('actual_device') == 'cuda'
                and evidence.get('cold_cache') is True and evidence.get('network_blocked') is True
                and evidence.get('cleanup_confirmed') is True
                and evidence.get('memory_method') in {'process', 'device_total', 'torch_reserved'}
                and type(peak) in {int, float} and math.isfinite(peak) and 0 < peak <= peak_target_mib
                and binding.get('device', {}).get('memory_mib', 0) >= peak + 2048
                and isinstance(evidence.get('result_sha256'), str) and len(evidence['result_sha256']) == 64)

    def record(self, kind, binding, evidence, *, peak_target_mib=12288):
        """Called after a real local cold-cache run; no synthetic auto-qualification."""
        if kind not in GPU_KINDS or binding.get('kind') != kind or not self.valid_evidence(evidence, binding, peak_target_mib):
            raise ValueError('GPU 实测、显存或清理证据不完整，保留关闭状态')
        with self.lock:
            try:
                value = json.loads(self.path.read_text('utf-8'))
                if value.get('format') != FORMAT:
                    raise ValueError('Unknown GPU qualification receipt format')
            except FileNotFoundError:
                value = {'format': FORMAT, 'items': {}}
            value['items'][kind] = {'binding': binding, 'evidence': evidence, 'qualified_at': time.time()}
            atomic_json(self.path, value)
