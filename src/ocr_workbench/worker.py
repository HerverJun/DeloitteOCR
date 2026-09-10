"""One isolated process per engine invocation; results travel via UTF-8 files."""

import argparse
import hashlib
import json
from pathlib import Path
import time
import traceback
import faulthandler

from ocr_workbench.offline import configure, install_guard
from ocr_workbench.tables import parse_tables, export_xlsx


def json_safe(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value)}")


def load_paddle(engine, models):
    import paddle

    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
        raise RuntimeError("Paddle CUDA GPU is required; CPU fallback is disabled")
    common = {
        "device": "gpu:0",
        "enable_mkldnn": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
    }
    start = time.perf_counter()
    if engine == "ppocr":
        from paddleocr import PaddleOCR

        pipe = PaddleOCR(
            **common,
            text_detection_model_name="PP-OCRv6_medium_det",
            text_detection_model_dir=str(models / "PP-OCRv6_medium_det"),
            text_recognition_model_name="PP-OCRv6_medium_rec",
            text_recognition_model_dir=str(models / "PP-OCRv6_medium_rec"),
            textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
            textline_orientation_model_dir=str(models / "PP-LCNet_x1_0_textline_ori"),
            use_textline_orientation=True,
        )
    else:
        from paddleocr import PaddleOCRVL

        pipe = PaddleOCRVL(
            **common,
            pipeline_version="v1.6",
            layout_detection_model_dir=str(models / "PP-DocLayoutV3"),
            vl_rec_model_dir=str(models / "PaddleOCR-VL-1.6"),
            use_layout_detection=True,
            vl_rec_backend="native",
            use_queues=False,
        )
    return pipe, time.perf_counter() - start


def paddle_engine(engine, models, image, pipe=None, loaded=None):
    if pipe is None:
        pipe, loaded = load_paddle(engine, models)
    # Decode through Python's Unicode-aware I/O; Paddle expects BGR arrays.
    import numpy as np
    from PIL import Image

    with Image.open(image) as source:
        pixels = np.ascontiguousarray(np.asarray(source.convert("RGB"))[:, :, ::-1])
    raw = [res.json for res in pipe.predict(pixels)]
    blocks = []
    for page in raw:
        data = page.get("res", page)
        if engine == "ppocr":
            for text, score, polygon in zip(
                data["rec_texts"], data["rec_scores"], data["rec_polys"]
            ):
                blocks.append(
                    {
                        "text": text,
                        "confidence": float(score),
                        "polygon": polygon,
                        "kind": "text",
                    }
                )
        else:
            for block in data["parsing_res_list"]:
                box = block.get("block_bbox")
                polygon = (
                    [
                        [box[0], box[1]],
                        [box[2], box[1]],
                        [box[2], box[3]],
                        [box[0], box[3]],
                    ]
                    if box
                    else None
                )
                blocks.append(
                    {
                        "text": block["block_content"],
                        "confidence": None,
                        "polygon": polygon,
                        "kind": block["block_label"],
                    }
                )
    return raw, blocks, loaded


def run_image(args, session=None):
    """Write one complete result, with optional resident model reuse."""
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, repeat=True)
    args.output.mkdir(parents=True, exist_ok=True)
    configure(args.output)
    started = time.perf_counter()
    try:
        process_image(args, session, started)
    except Exception as error:
        (args.output / "error.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "engine": args.engine,
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument(
        "--engine", choices=["ppocr", "paddlevl", "glm", "hunyuan"], required=True
    )
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    install_guard(args.output / "network-blocked.log")
    run_image(args)


def process_image(args, session, started):
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener()
    original_hash = hashlib.file_digest(args.image.open("rb"), "sha256").hexdigest()
    prepared = args.output / "input.png"
    with Image.open(args.image) as original:
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError("Multi-frame images require explicit frame selection")
        from ocr_workbench.image_utils import normalized_rgb

        img = normalized_rgb(original)
        img.save(prepared)
        width, height = img.size
    models = args.bundle / "models"
    engine_info = json.loads(
        (args.bundle / "config/engines.json").read_text(encoding="utf-8")
    )[args.engine]
    revisions = {}
    for name in engine_info["models"]:
        manifest = models / name / "source-manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError(f"Missing model provenance manifest: {manifest}")
        revisions[name] = json.loads(manifest.read_text(encoding="utf-8"))["revision"]
    if session is not None:
        raw, blocks, loaded = session.recognize(prepared, args.output)
    elif args.engine in {"ppocr", "paddlevl"}:
        from ocr_workbench.windows_paths import ascii_model_directory

        with ascii_model_directory(models) as compatible_models:
            raw, blocks, loaded = paddle_engine(
                args.engine, compatible_models, prepared
            )
    elif args.engine == "glm":
        from ocr_workbench.glm import recognize

        raw, blocks, loaded = recognize(models, prepared)
    else:
        from ocr_workbench.hunyuan import recognize

        raw, blocks, loaded = recognize(args.bundle, prepared, args.output)
    text = "\n".join(b["text"] for b in blocks)
    (args.output / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, default=json_safe),
        encoding="utf-8",
    )
    tables = parse_tables(text)
    result = {
        "schema_version": 1,
        "status": "success",
        "engine": args.engine,
        "engine_info": engine_info,
        "model_revisions": revisions,
        "image": {
            "source": str(args.image.resolve()),
            "sha256": original_hash,
            "version": hashlib.file_digest(prepared.open("rb"), "sha256").hexdigest(),
            "width": width,
            "height": height,
            "coordinates": "prepared_image_pixels",
        },
        "elapsed_seconds": time.perf_counter() - started,
        "load_seconds": loaded,
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "raw": raw,
    }
    (args.output / "result.txt").write_text(text, encoding="utf-8")
    (args.output / "result.md").write_text(text, encoding="utf-8")
    if tables:
        export_xlsx(tables, args.output / "result.xlsx")
    from ocr_workbench.runtime_audit import inspect_runtime

    result["runtime_audit"] = inspect_runtime(args.bundle)
    pending = args.output / "result.json.tmp"
    pending.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=json_safe),
        encoding="utf-8",
    )
    pending.replace(args.output / "result.json")


if __name__ == "__main__":
    main()
