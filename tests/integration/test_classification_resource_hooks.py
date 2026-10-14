"""Classification GPU entry and process-ownership simulation; no real CUDA claim."""

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

CLASSIFICATION_SRC = Path(__file__).resolve().parents[3] / 'classification' / 'src'
sys.path.insert(0, str(CLASSIFICATION_SRC))

from image_platform import cli, platform_resources as resources  # noqa: E402
from workbench_platform.resources.gpu import (  # noqa: E402
    GpuCoordinator, Owner, ResourceBusy, ResourceEvidence,
)


@pytest.fixture
def platform_env(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    ledger = tmp_path / "gpu.sqlite"
    for key, value in {
        "WORKBENCH_APP_ID": "classification", "WORKBENCH_INSTANCE_ID": "instance-classification",
        "WORKBENCH_LAUNCH_ID": "launch-classification", "WORKBENCH_GPU_LEDGER": str(ledger),
        "LOCALAPPDATA": str(local),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(resources, "_identity", lambda: "123456789012345678")
    return ledger


def test_competing_ocr_cannot_grant_while_classification_owns(platform_env):
    reservation = resources.reserve()
    coordinator = GpuCoordinator(platform_env)
    assert coordinator.snapshot().owner == Owner("classification", "launch-classification",
                                                  "instance-classification", os.getpid(), "123456789012345678")
    coordinator.request("ocr-recognition", "ocr")
    ocr = Owner("ocr", "launch-ocr", "instance-ocr", os.getpid(), "123456789012345678")
    with pytest.raises(ResourceBusy):
        coordinator.grant("ocr-recognition", ocr, owned=lambda _: True)
    reservation.close()
    lease = coordinator.grant("ocr-recognition", ocr, owned=lambda _: True)
    coordinator.release(lease, witness=lambda _: ResourceEvidence(True, True, False, True))


def test_child_cache_and_job_must_be_proven_drained(platform_env):
    reservation = resources.reserve()

    class Child:
        alive = True

        def poll(self):
            return None if self.alive else 0

    job = SimpleNamespace(handle=123, drained=False)
    child = Child()
    reservation.track(child, job)
    with pytest.raises(resources.ResourceUnavailable):
        reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id
    child.alive, job.handle, job.drained = False, None, True
    reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is None


def test_queue_cancel_and_control_do_not_release_other_owner(platform_env):
    coordinator = GpuCoordinator(platform_env)
    ocr = Owner("ocr", "launch-ocr", "instance-ocr", os.getpid(), "123456789012345678")
    coordinator.request("ocr-recognition", "ocr")
    lease = coordinator.grant("ocr-recognition", ocr, owned=lambda _: True)
    cancelled, calls, errors = threading.Event(), [], []

    def wait_for_gpu():
        try:
            resources.reserve(cancelled=cancelled, control=lambda: calls.append("native-lease-checked"))
        except resources.ResourceUnavailable as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wait_for_gpu)
    thread.start()
    deadline = time.monotonic() + 5
    while not coordinator.snapshot().pending and time.monotonic() < deadline:
        time.sleep(.01)
    assert coordinator.snapshot().pending and calls
    cancelled.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors
    assert coordinator.snapshot().pending == () and coordinator.snapshot().owner == ocr
    coordinator.release(lease, witness=lambda _: ResourceEvidence(True, True, False, True))


def test_partial_configuration_and_direct_cuda_loader_fail_closed(tmp_path, monkeypatch):
    for key in resources._KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(resources.ResourceUnavailable):
        resources.require_gpu_context()
    monkeypatch.setenv("WORKBENCH_APP_ID", "classification")
    with pytest.raises(resources.ResourceUnavailable):
        resources.reserve()


def test_cli_cuda_predict_and_doctor_are_isolated(monkeypatch):
    monkeypatch.setattr(cli.Settings, "from_env", lambda: SimpleNamespace(prepare=lambda: None))
    calls = []

    def isolated(payload):
        calls.append(payload)
        return subprocess.CompletedProcess([], 0, b'{"ok": true}\n', b"")

    monkeypatch.setattr(resources, "run_isolated", isolated)
    monkeypatch.setattr(sys, "argv", ["image-platform", "doctor"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["image-platform", "predict", "package", "image"])
    cli.main()
    assert [item["mode"] for item in calls] == ["doctor", "predict"]


def test_native_classification_entries_in_isolated_runtime(tmp_path):
    python = CLASSIFICATION_SRC.parents[2] / 'program/bundles/classification/runtimes/control/python.exe'
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(CLASSIFICATION_SRC)
    environment['PYTHONDONTWRITEBYTECODE']='1'
    for key in resources._KEYS:
        environment.pop(key, None)
    environment['PYTEST_ADDOPTS']='-p no:cacheprovider'
    result = subprocess.run([str(python), "-m", "pytest", "tests/test_resource_hooks.py", "-q", '-p', 'no:cacheprovider',
                             '--basetemp',str(tmp_path/'classification-pytest')],
                            cwd=CLASSIFICATION_SRC.parent, env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership")
def test_owned_job_drains_residual_descendant_before_release(platform_env, tmp_path):
    gate, pidfile = tmp_path / "go", tmp_path / "child.pid"
    script = ("import subprocess,sys,time;from pathlib import Path;"
              "gate=Path(sys.argv[1]);pidfile=Path(sys.argv[2]);"
              "\nwhile not gate.exists(): time.sleep(.01)\n"
              "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
              "pidfile.write_text(str(child.pid));time.sleep(60)")
    reservation = resources.reserve()
    with resources.OwnedJob() as job:
        parent = subprocess.Popen([sys.executable, "-c", script, str(gate), str(pidfile)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            reservation.track(parent, job)
            job.assign(parent)
            gate.touch()
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert pidfile.exists() and job.active_processes() >= 2
            with pytest.raises(resources.ResourceUnavailable):
                reservation.close()
            job.drain()
            parent.wait(timeout=5)
            assert job.active_processes() == 0
        finally:
            if parent.poll() is None:
                job.close()
                parent.wait(timeout=5)
    reservation.close()
    assert GpuCoordinator(platform_env).snapshot().owner_request_id is None
