import numpy as np
import pytest
import sys
from types import SimpleNamespace

from image_platform.runtime import decide, probabilities


def test_insufficient_calibration_rejects_even_saturated_confidence():
    metadata = {"labels": [{"class_index": 1, "label_id": "b", "display_name": "B"}, {"class_index": 0, "label_id": "a", "display_name": "A"}],
                "decision_policy": {"thresholds_by_label_id": {"a": 1.0, "b": 0.8}}}
    logits = np.array([[1000.0, -1000.0], [-1000.0, 1000.0]], dtype=np.float32)
    rows = decide(logits, metadata, auto_accept=True)
    assert rows[0]["score"] == 1.0
    assert [(row["label_id"], row["status"]) for row in rows] == [("a", "needs_review"), ("b", "accepted")]
    assert all(row["status"] == "needs_review" for row in decide(logits, metadata))


def test_invalid_logits_are_rejected_before_decisions():
    with pytest.raises(ValueError):
        probabilities(np.array([[float("nan"), 0.0]]))
    with pytest.raises(ValueError):
        probabilities(np.array([1.0, 2.0]))


def test_session_cache_has_bounded_capacity_and_keys_content_and_device(tmp_path):
    from image_platform.runtime import SessionCache
    calls = []
    def factory(path, require_gpu):
        result = object()
        calls.append(result)
        return result
    cache = SessionCache(1)
    first, hit = cache.get(tmp_path / "model.onnx", "sha-one", factory=factory)
    assert not hit
    assert cache.get(tmp_path / "model.onnx", "sha-one", factory=factory) == (first, True)
    second, hit = cache.get(tmp_path / "model.onnx", "sha-two", factory=factory)
    assert not hit and second is not first and len(cache.entries) == 1
    third, hit = cache.get(tmp_path / "model.onnx", "sha-two", require_gpu=False, factory=factory)
    assert not hit and third is not second and len(calls) == 3
    cache.clear()
    assert not cache.entries


def test_device_probe_separates_frameworks_and_does_not_treat_driver_as_cuda(monkeypatch):
    from image_platform.runtime import probe_devices
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(get_available_providers=lambda: ['CPUExecutionProvider']))
    devices = probe_devices()
    assert devices['training']['cpu']['available'] is True
    assert devices['training']['cuda']['available'] is False
    assert devices['inference']['cpu']['available'] is True
    assert devices['inference']['cuda']['available'] is False


def test_declared_ort_cuda_does_not_claim_device_or_dll_initialization_passed(monkeypatch):
    from image_platform.runtime import probe_devices
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(get_available_providers=lambda: ['CUDAExecutionProvider', 'CPUExecutionProvider']))
    devices = probe_devices()
    assert devices['training']['cuda']['available'] is True
    assert devices['inference']['cuda']['available'] is None
    assert '模型初始化时核实' in devices['inference']['cuda']['reason']


def test_missing_training_framework_does_not_hide_available_cpu_inference(monkeypatch):
    from image_platform.runtime import probe_devices
    monkeypatch.setitem(sys.modules, 'torch', None)
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(get_available_providers=lambda: ['CPUExecutionProvider']))
    devices = probe_devices()
    assert devices['training']['cpu']['available'] is False
    assert devices['inference']['cpu']['available'] is True


def test_environment_probe_timeout_is_unknown_and_does_not_switch_device(monkeypatch):
    import subprocess
    from image_platform import runtime
    monkeypatch.setattr(runtime, '_DEVICE_PROBE', None)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(subprocess, 'run', timeout)
    devices = runtime.device_readiness(refresh=True)
    assert all(state['available'] is None for framework in devices.values() for device, state in framework.items() if device in {'cpu', 'cuda'})
    assert '按所选设备执行' in devices['training']['cuda']['reason']
