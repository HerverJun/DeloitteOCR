"""An isolated resident model process, with atomically published file IPC."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import time
import traceback
import faulthandler
from types import SimpleNamespace
from ocr_workbench.paths import require_path


def publish(path, data):
    from ocr_workbench.atomic_files import write_json

    write_json(path, data)


class LoadProgress:
    """Observe startup boundaries without changing the engine's startup budget.

    The clock starts after argument parsing, not at process creation. A Paddle
    pipeline stage includes any dependency imports inside load_paddle; its
    internals are deliberately not reported as independently observed stages.
    """

    def __init__(self, ipc, engine, *, interval=2):
        resolved = require_path(ipc, 'Engine loading receipts')
        self.path = resolved / 'loading.json'
        self.engine = engine
        self.interval = interval
        self.started = self.stage_started = time.perf_counter()
        self.stage = 'worker_start'
        self.detail = 'Worker arguments parsed; Python process startup is not included'
        self.status = 'loading'
        self.stages = []
        self.error = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, name='engine-load-heartbeat', daemon=True)
        with self._lock:
            self._write_locked()
        self._thread.start()

    def _write_locked(self):
        from ocr_workbench.atomic_files import write_json

        now = time.perf_counter()
        receipt = {
            'schema_version': 1,
            'engine': self.engine,
            'status': self.status,
            'stage': self.stage,
            'detail': self.detail,
            'clock_origin': 'after_argument_parsing',
            'elapsed_seconds': max(0, now - self.started),
            'stage_elapsed_seconds': max(0, now - self.stage_started),
            'heartbeat_unix_seconds': time.time(),
            'stages': list(self.stages),
        }
        if self.error is not None:
            receipt['message'] = self.error
        try:
            write_json(self.path, receipt, durable=True)
        except OSError as error:
            # Diagnostics must not turn a healthy model load into a retry or
            # conceal the exception that actually caused startup to fail.
            print(f'Engine loading receipt write failed: {error}', flush=True)

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                if self.status != 'loading':
                    return
                self._write_locked()

    def enter(self, stage, detail):
        with self._lock:
            if self.status != 'loading':
                return
            now = time.perf_counter()
            self.stages.append({'stage': self.stage, 'seconds': max(0, now - self.stage_started)})
            self.stage, self.detail, self.stage_started = stage, detail, now
            self._write_locked()

    @property
    def elapsed_seconds(self):
        return max(0, time.perf_counter() - self.started)

    def finish(self, status, error=None):
        self._stop.set()
        with self._lock:
            if self.status == 'loading':
                now = time.perf_counter()
                self.stages.append({'stage': self.stage, 'seconds': max(0, now - self.stage_started)})
                self.status = status
                self.error = str(error) if error is not None else None
                if status == 'ready':
                    self.stage, self.detail = 'ready', 'Engine readiness was published to status.json'
                    self.stage_started = now
                self._write_locked()
        self._thread.join()


def consume_request(path, stop_path):
    """Claim one request before inference, tolerating brief Windows read locks."""
    data = json.loads(path.read_text(encoding='utf-8'))
    deadline = time.monotonic() + 1
    while True:
        if stop_path.exists():
            return None
        try:
            path.unlink()
            return data
        except PermissionError as error:
            if getattr(error, 'winerror', None) not in {32, 33} or time.monotonic() >= deadline:
                raise
            # No model call occurs until ownership was consumed. Persistent
            # sharing errors still fail within the bounded one-second window.
            time.sleep(.025)


class Resident:
    def __init__(self, bundle, engine, ipc, geometry_provider='paddle', progress=None):
        self.bundle, self.engine = bundle, engine
        self.stack = ExitStack()
        self.first = True
        self.load_timings = []
        load_started = time.perf_counter()
        recorder = progress
        def progress(phase):
            self.load_timings.append({'phase': phase, 'elapsed_seconds': time.perf_counter() - load_started})
            publish(ipc / 'load-progress.json', {'version': 1, 'engine': engine,
                                                'stages': self.load_timings})
        def stage(name, detail):
            if recorder is not None:
                recorder.enter(name, detail)

        try:
            if engine == 'geometry' and geometry_provider in ('tableformer-raw', 'rapidtable'):
                stage('dependency_import', 'Importing geometry candidate worker')
                from ocr_workbench.geometry_candidate_worker import CandidateSession
                stage('model_initialization', 'Constructing geometry candidate session')
                self.session = CandidateSession(geometry_provider)
                self.loaded = self.session.loaded
            elif engine in {"ppocr", "paddlevl", "dewarp", "geometry"}:
                stage('dependency_import', 'Importing Paddle worker and model path helpers')
                from ocr_workbench.windows_paths import ascii_model_directory
                from ocr_workbench.worker import load_paddle

                stage('model_path_preparation', 'Preparing the existing local model directory')
                self.models = self.stack.enter_context(
                    ascii_model_directory(bundle / "models")
                )
                if engine == "geometry":
                    stage('dependency_import', 'Importing table geometry sessions')
                    from ocr_workbench.table_geometry_worker import GeometrySession, GeometryContextSession
                    stage('model_initialization', 'Constructing the table geometry session')
                    self.session = (GeometryContextSession if geometry_provider == 'context' else GeometrySession)(self.models)
                    self.loaded = self.session.loaded
                elif engine == "dewarp":
                    stage('dependency_import', 'Importing PaddleX and Paddle')
                    from paddlex import create_model
                    import paddle

                    if (
                        not paddle.is_compiled_with_cuda()
                        or paddle.device.cuda.device_count() < 1
                    ):
                        raise RuntimeError("UVDoc requires CUDA")
                    stage('model_initialization', 'Constructing the existing UVDoc model')
                    started = time.perf_counter()
                    self.session = create_model(
                        "UVDoc",
                        model_dir=str(self.models / "UVDoc"),
                        device="gpu:0",
                        enable_mkldnn=False,
                    )
                    self.loaded = time.perf_counter() - started
                else:
                    stage('paddle_import', 'Importing Paddle runtime before the existing loader')
                    import paddle
                    stage('paddle_pipeline_initialization', 'Calling the existing Paddle loader')
                    self.session, self.loaded = load_paddle(engine, self.models, progress=progress)
            elif engine == "glm":
                stage('dependency_import', 'Importing GLM session loader')
                from ocr_workbench.glm import load_session

                stage('model_initialization', 'Calling the existing GLM session loader')
                self.session = load_session(bundle / "models", stage=stage)
                self.loaded = self.session[5]
            elif engine == 'mineru_pro':
                from ocr_workbench.mineru_pro import Session
                self.session = Session(bundle / 'models', progress=progress)
                self.stack.callback(self.session.close)
                self.loaded = self.session.loaded
            else:
                stage('dependency_import', 'Importing Hunyuan session')
                from ocr_workbench.hunyuan import Session

                stage('model_initialization', 'Constructing Hunyuan session; internal loading stages are not instrumented')
                self.session = Session(bundle, ipc)
                self.stack.callback(self.session.close)
                self.loaded = self.session.loaded
        except BaseException:
            self.stack.close()
            raise

    def recognize(self, image, output, *, recognition_mode='page'):
        from ocr_workbench.recognition_modes import validate_recognition_mode
        validate_recognition_mode(self.engine, recognition_mode)
        if recognition_mode == 'local_text':
            from ocr_workbench.glm import recognize_local_text
            raw, blocks, _ = recognize_local_text(self.bundle / 'models', image, self.session)
        elif self.engine in {"ppocr", "paddlevl"}:
            from ocr_workbench.worker import paddle_engine

            raw, blocks, _ = paddle_engine(
                self.engine, self.models, image, self.session, self.loaded
            )
            if self.engine == "paddlevl":
                # Variable page shapes otherwise retain gigabytes of unused
                # allocator blocks between tasks. Outputs are already on CPU;
                # keep live model tensors, precision and decoding unchanged.
                import gc
                import paddle

                gc.collect()
                paddle.device.cuda.empty_cache()
        elif self.engine == "glm":
            from ocr_workbench.glm import recognize

            raw, blocks, _ = recognize(self.bundle / "models", image, self.session)
        else:
            raw, blocks, _ = self.session.recognize(image, output)
        loaded = self.loaded if self.first else 0
        self.first = False
        return raw, blocks, loaded

    def close(self):
        self.stack.close()

    def dewarp(self, image, output):
        import numpy as np
        from PIL import Image

        with Image.open(image) as source:
            pixels = np.ascontiguousarray(np.asarray(source.convert("RGB"))[:, :, ::-1])
        result = next(iter(self.session.predict(pixels)))
        # PaddleX DocTrResult's default Pillow writer consumes this RGB array directly.
        pixels = np.asarray(result["doctr_img"])
        if pixels.ndim != 3 or pixels.shape[2] != 3:
            raise RuntimeError("UVDoc returned an invalid image")
        target = output / "dewarped.png"
        Image.fromarray(np.ascontiguousarray(pixels).astype("uint8")).save(target)
        revision = json.loads(
            (self.bundle / "models/UVDoc/source-manifest.json").read_text(
                encoding="utf-8"
            )
        )["revision"]
        publish(
            output / "dewarp.json",
            {
                "status": "success",
                "image": str(target),
                "model": "UVDoc",
                "revision": revision,
            },
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument(
        "--engine",
        required=True,
        choices=["ppocr", "paddlevl", "glm", "hunyuan", "mineru_pro", "dewarp", "geometry"],
    )
    p.add_argument("--ipc", type=Path, required=True)
    p.add_argument('--geometry-provider', choices=['paddle', 'tableformer-raw', 'rapidtable', 'context'], default='paddle')
    args = p.parse_args()
    args.bundle = require_path(args.bundle, 'OCR engine bundle', role='read')
    args.ipc = require_path(args.ipc, 'OCR engine IPC')
    progress = LoadProgress(args.ipc, args.engine)
    session = None
    try:
        faulthandler.enable()
        faulthandler.dump_traceback_later(60, repeat=True)
        progress.enter('offline_import', 'Importing the existing offline configuration helpers')
        from ocr_workbench.offline import configure, install_guard
        progress.enter('offline_configuration', 'Applying the existing local cache and network guard configuration')
        configure(args.ipc)
        install_guard(args.ipc / "network-blocked.log")
        session = Resident(args.bundle, args.engine, args.ipc, args.geometry_provider, progress=progress)
        faulthandler.cancel_dump_traceback_later()
        initialization_seconds = progress.elapsed_seconds
        progress.enter('readiness_publish', 'Publishing engine readiness after successful session construction')
        publish(
            args.ipc / "status.json",
            {"status": "ready", "load_seconds": session.loaded, 'load_timings': getattr(session, 'load_timings', []),
             "initialization_seconds": initialization_seconds},
        )
        progress.finish('ready')
        while not (args.ipc / "stop.json").exists():
            request = args.ipc / "request.json"
            if not request.exists():
                time.sleep(0.05)
                continue
            data = consume_request(request, args.ipc / 'stop.json')
            if data is None:
                break
            try:
                from ocr_workbench.worker import run_image

                from ocr_workbench.recognition_modes import validate_recognition_mode
                mode = validate_recognition_mode(args.engine, data.get('recognition_mode', 'page'))
                if mode == 'local_text' and data.get('geometry_request') is not None:
                    raise ValueError('Local text cannot use a geometry request')

                if args.engine == "geometry":
                    session.session.write(require_path(data['image'], 'Geometry input', role='read'),
                        require_path(data['output'], 'Geometry output'), data['geometry_request'])
                elif args.engine == "dewarp":
                    session.dewarp(require_path(data['image'], 'Dewarp input', role='read'),
                        require_path(data['output'], 'Dewarp output'))
                else:
                    run_image(
                        SimpleNamespace(
                            bundle=args.bundle,
                            engine=args.engine,
                            image=Path(data["image"]),
                            output=Path(data["output"]),
                            recognition_mode=mode,
                        ),
                        session,
                    )
                publish(
                    args.ipc / "response.json", {"id": data["id"], "status": "success"}
                )
            except Exception as error:
                publish(
                    args.ipc / "response.json",
                    {"id": data["id"], "status": "failed", "message": str(error)},
                )
                return 1
        return 0
    except BaseException as error:
        if progress.status == 'loading':
            progress.finish('failed', error)
        publish(
            args.ipc / "status.json",
            {
                "status": "failed",
                "message": str(error),
                "traceback": traceback.format_exc(),
            },
        )
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()
        if progress.status == 'loading':
            progress.finish('failed', 'Worker startup ended before readiness')
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
