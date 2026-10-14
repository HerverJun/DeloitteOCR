"""GPU routing and CPU opt-out without a physical GPU."""

import sys
from types import SimpleNamespace

import pytest

from image_platform import cli, platform_resources as resources, runtime, training, worker


def test_direct_gpu_loader_and_training_reject_unmanaged_process(tmp_path):
    with pytest.raises(resources.ResourceUnavailable):
        runtime.session_for(tmp_path / "missing.onnx", require_gpu=True)
    with pytest.raises(resources.ResourceUnavailable):
        training.train(None, {"params": {"device": "cuda"}}, None, None)


def test_cpu_session_never_enters_gpu_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(resources, "require_gpu_context", lambda: calls.append("gpu"))

    class FakeOrt:
        class SessionOptions:
            enable_profiling = False
            intra_op_num_threads = 0

        @staticmethod
        def InferenceSession(*_, **__):
            return SimpleNamespace(disable_fallback=lambda: None,
                                   get_inputs=lambda: [SimpleNamespace(name="images", type="tensor(float)")],
                                   get_outputs=lambda: [SimpleNamespace(name="logits")])

    monkeypatch.setitem(sys.modules, "onnxruntime", FakeOrt)
    runtime.session_for("model.onnx", require_gpu=False)
    assert calls == []


def test_worker_routes_only_cuda_jobs_to_short_lived_child(monkeypatch):
    jobs = iter([{"kind": "train", "params": {"device": "cuda"}},
                 {"kind": "inference", "params": {"device": "cpu"}},
                 {"kind": "import", "params": {}}])
    calls = []
    monkeypatch.setattr(worker, "Store", lambda _: SimpleNamespace(claim=lambda _: next(jobs)))
    monkeypatch.setattr(worker, "run_cuda_job", lambda _, job: calls.append(("gpu", job["kind"])))
    monkeypatch.setattr(worker, "run_job", lambda _, job: calls.append(("cpu", job["kind"])))
    for _ in range(3):
        worker.serve_worker(SimpleNamespace(), once=True)
    assert calls == [("gpu", "train"), ("cpu", "inference"), ("cpu", "import")]


def test_worker_child_failure_does_not_retry_or_claim_success(monkeypatch):
    job = {"id": "job", "kind": "inference", "params": {"device": "cuda"}, "lease_token": "token"}
    calls = []

    class Store:
        settings = SimpleNamespace(data_dir="data")

        def heartbeat(self, *_):
            return True

        def job(self, *_):
            return {**job, "state": "running"}

        def finish(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(resources, "run_isolated", lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b"failed"))
    worker.run_cuda_job(Store(), job)
    assert len(calls) == 1 and calls[0][0][2] == "failed"


def test_worker_queue_pause_marks_paused_without_child(monkeypatch):
    job = {"id": "job", "kind": "inference", "params": {"device": "cuda"}, "lease_token": "token"}
    calls = []

    class Store:
        settings = SimpleNamespace(data_dir="data")

        def heartbeat(self, *_):
            return True

        def job(self, *_):
            return {**job, "state": "pausing", "lease_until": 10**12}

        def finish(self, *args, **kwargs):
            calls.append(args)

    def isolated(_, *, control, **kwargs):
        control()
        raise AssertionError("unreachable")

    monkeypatch.setattr(resources, "run_isolated", isolated)
    worker.run_cuda_job(Store(), job)
    assert len(calls) == 1 and calls[0][2] == "paused"


def test_explicit_cpu_cli_predict_does_not_reserve(monkeypatch):
    from image_platform import predict

    calls = []
    monkeypatch.setattr(cli.Settings, "from_env", lambda: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(resources, "run_isolated", lambda *a, **kw: calls.append("gpu"))
    monkeypatch.setattr(predict, "predict_images", lambda *args: (calls.append(args), {"cpu": True})[1])
    monkeypatch.setattr(sys, "argv", ["image-platform", "predict", "package", "image", "--device", "cpu"])
    cli.main()
    assert len(calls) == 1 and calls[0][2] == "cpu"
