"""An isolated resident model process, with atomically published file IPC."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time
import traceback
from types import SimpleNamespace


def publish(path, data):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    pending.replace(path)


class Resident:
    def __init__(self, bundle, engine, ipc):
        self.bundle, self.engine = bundle, engine
        self.stack = ExitStack()
        self.first = True
        try:
            if engine in {"ppocr", "paddlevl", "dewarp"}:
                from ocr_workbench.windows_paths import ascii_model_directory
                from ocr_workbench.worker import load_paddle

                self.models = self.stack.enter_context(
                    ascii_model_directory(bundle / "models")
                )
                if engine == "dewarp":
                    from paddlex import create_model
                    import paddle

                    if (
                        not paddle.is_compiled_with_cuda()
                        or paddle.device.cuda.device_count() < 1
                    ):
                        raise RuntimeError("UVDoc requires CUDA")
                    started = time.perf_counter()
                    self.session = create_model(
                        "UVDoc",
                        model_dir=str(self.models / "UVDoc"),
                        device="gpu:0",
                        enable_mkldnn=False,
                    )
                    self.loaded = time.perf_counter() - started
                else:
                    self.session, self.loaded = load_paddle(engine, self.models)
            elif engine == "glm":
                from ocr_workbench.glm import load_session

                self.session = load_session(bundle / "models")
                self.loaded = self.session[5]
            else:
                from ocr_workbench.hunyuan import Session

                self.session = Session(bundle, ipc)
                self.stack.callback(self.session.close)
                self.loaded = self.session.loaded
        except BaseException:
            self.stack.close()
            raise

    def recognize(self, image, output):
        if self.engine in {"ppocr", "paddlevl"}:
            from ocr_workbench.worker import paddle_engine

            raw, blocks, _ = paddle_engine(
                self.engine, self.models, image, self.session, self.loaded
            )
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
        choices=["ppocr", "paddlevl", "glm", "hunyuan", "dewarp"],
    )
    p.add_argument("--ipc", type=Path, required=True)
    args = p.parse_args()
    from ocr_workbench.offline import configure, install_guard

    configure(args.ipc)
    install_guard(args.ipc / "network-blocked.log")
    session = None
    try:
        session = Resident(args.bundle, args.engine, args.ipc)
        publish(
            args.ipc / "status.json",
            {"status": "ready", "load_seconds": session.loaded},
        )
        while not (args.ipc / "stop.json").exists():
            request = args.ipc / "request.json"
            if not request.exists():
                time.sleep(0.05)
                continue
            data = json.loads(request.read_text(encoding="utf-8"))
            request.unlink()
            try:
                from ocr_workbench.worker import run_image

                if args.engine == "dewarp":
                    session.dewarp(Path(data["image"]), Path(data["output"]))
                else:
                    run_image(
                        SimpleNamespace(
                            bundle=args.bundle,
                            engine=args.engine,
                            image=Path(data["image"]),
                            output=Path(data["output"]),
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
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
