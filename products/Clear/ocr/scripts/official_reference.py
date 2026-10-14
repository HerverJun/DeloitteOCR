"""Direct upstream API calls for fidelity audit; no application inference wrappers.

Uses the same pinned files, fixed precision, prompt and generation parameters.
Offline guards are shared infrastructure, not recognition/normalization code.
"""

import argparse
import base64
import io
import json
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.request
from ocr_workbench.offline import configure, install_guard


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument(
        "--engine", choices=["ppocr", "paddlevl", "glm", "hunyuan"], required=True
    )
    p.add_argument("--images", type=Path, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    configure(a.output)
    install_guard(a.output / "network-blocked.log")
    models = a.bundle / "models"
    from PIL import Image

    if a.engine in {"ppocr", "paddlevl"}:
        import numpy as np
        import paddle

        assert paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
        common = dict(
            device="gpu:0",
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
        )
        if a.engine == "ppocr":
            from paddleocr import PaddleOCR

            model = PaddleOCR(
                **common,
                text_detection_model_name="PP-OCRv6_medium_det",
                text_detection_model_dir=str(models / "PP-OCRv6_medium_det"),
                text_recognition_model_name="PP-OCRv6_medium_rec",
                text_recognition_model_dir=str(models / "PP-OCRv6_medium_rec"),
                textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
                textline_orientation_model_dir=str(
                    models / "PP-LCNet_x1_0_textline_ori"
                ),
                use_textline_orientation=True,
            )
        else:
            from paddleocr import PaddleOCRVL

            model = PaddleOCRVL(
                **common,
                pipeline_version="v1.6",
                layout_detection_model_dir=str(models / "PP-DocLayoutV3"),
                vl_rec_model_dir=str(models / "PaddleOCR-VL-1.6"),
                use_layout_detection=True,
                vl_rec_backend="native",
                use_queues=False,
            )
        for i, path in enumerate(a.images):
            with Image.open(path) as image:
                pixels = np.ascontiguousarray(
                    np.asarray(image.convert("RGB"))[:, :, ::-1]
                )
            value = [result.json for result in model.predict(pixels)]
            (a.output / f"{i}.json").write_text(
                json.dumps(value, ensure_ascii=False, default=lambda x: x.tolist()),
                "utf-8",
            )
    elif a.engine == "glm":
        import torch
        from transformers import AutoProcessor, AutoModelForImageTextToText
        from glmocr.config import load_config
        from glmocr.layout import PPDocLayoutDetector
        from glmocr.dataloader import PageLoader
        from glmocr.postprocess import ResultFormatter
        from glmocr.utils.image_utils import crop_image_region

        config = load_config()
        config.pipeline.maas.enabled = False
        config.pipeline.max_workers = 1
        config.pipeline.layout.model_dir = str(models / "PP-DocLayoutV3_safetensors")
        config.pipeline.layout.device = "cpu"
        detector = PPDocLayoutDetector(config.pipeline.layout)
        detector.start()
        loader = PageLoader(config.pipeline.page_loader)
        formatter = ResultFormatter(config.pipeline.result_formatter)
        model = (
            AutoModelForImageTextToText.from_pretrained(
                str(models / "GLM-OCR"),
                dtype=torch.bfloat16,
                local_files_only=True,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )
        processor = AutoProcessor.from_pretrained(
            str(models / "GLM-OCR"), local_files_only=True
        )
        for i, path in enumerate(a.images):
            with Image.open(path) as source:
                image = source.convert("RGB")
            pages, _ = detector.process(
                [image],
                save_visualization=False,
                global_start_idx=0,
                use_polygon=config.pipeline.layout.use_polygon,
            )
            regions = pages[0]
            generated = []
            for region in regions:
                task = region.get("task_type", "text")
                if task in {"skip", "abandon"}:
                    region["content"] = ""
                    continue
                crop = crop_image_region(
                    image,
                    region["bbox_2d"],
                    (
                        region.get("polygon")
                        if config.pipeline.layout.use_polygon
                        else None
                    ),
                )
                payload = loader.build_request_from_image(crop, task_type=task)
                for item in payload["messages"][0]["content"]:
                    if item["type"] == "image_url":
                        raw = base64.b64decode(
                            item["image_url"]["url"].split(",", 1)[1]
                        )
                        item.clear()
                        item.update(
                            type="image",
                            image=Image.open(io.BytesIO(raw)).convert("RGB"),
                        )
                inputs = processor.apply_chat_template(
                    payload["messages"],
                    tokenize=True,
                    add_generation_prompt=True,
                    return_dict=True,
                    return_tensors="pt",
                ).to(model.device)
                with torch.inference_mode():
                    output = model.generate(
                        **inputs,
                        max_new_tokens=payload["max_tokens"],
                        do_sample=False,
                        repetition_penalty=payload["repetition_penalty"],
                    )
                ids = output[0][inputs["input_ids"].shape[-1] :]
                if len(ids) >= payload["max_tokens"]:
                    raise RuntimeError("Reference generation truncated")
                text = processor.decode(ids, skip_special_tokens=True)
                region["content"] = text
                generated.append(
                    {"bbox_2d": region["bbox_2d"], "task": task, "content": text}
                )
            formatted, markdown, _ = formatter.process([regions])
            value = {
                "regions": regions,
                "generated": generated,
                "official_formatted_json": json.loads(formatted),
                "official_markdown": markdown,
            }
            (a.output / f"{i}.json").write_text(
                json.dumps(value, ensure_ascii=False), "utf-8"
            )
    else:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        key = secrets.token_hex(24)
        base = f"http://127.0.0.1:{port}"
        command = [
            str(a.bundle / "runtimes/llama/llama-server.exe"),
            "--model",
            str(models / "HunyuanOCR-GGUF/hyocr-f16.gguf"),
            "--mmproj",
            str(models / "HunyuanOCR-GGUF/mmproj-hyocr-f16.gguf"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--alias",
            "HYVL",
            "--ctx-size",
            "10240",
            "--n-predict",
            "4096",
            "--n-gpu-layers",
            "99",
            "--parallel",
            "1",
            "--api-key",
            key,
            "--offline",
            "--log-verbosity",
            "4",
            "--fit",
            "off",
            "--device",
            "CUDA0",
            "--mmproj-device",
            "CUDA0",
        ]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        with (a.output / "llama-server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = time.monotonic() + 180
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("Reference server exited")
                    try:
                        with opener.open(
                            urllib.request.Request(base + "/health", headers=headers),
                            timeout=2,
                        ) as r:
                            if r.status == 200:
                                break
                    except OSError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError("Reference server startup")
                    time.sleep(0.2)
                prompt = json.loads(
                    (a.bundle / "config/hunyuan-prompts.json").read_text("utf-8")
                )["doc_parse"]
                for i, path in enumerate(a.images):
                    body = {
                        "model": "HYVL",
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": "data:image/png;base64,"
                                            + base64.b64encode(
                                                path.read_bytes()
                                            ).decode()
                                        },
                                    },
                                    {"type": "text", "text": prompt},
                                ],
                            }
                        ],
                        "max_tokens": 4096,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "top_k": -1,
                        "repeat_penalty": 1.08,
                        "seed": 0,
                    }
                    with opener.open(
                        urllib.request.Request(
                            base + "/v1/chat/completions",
                            data=json.dumps(body).encode(),
                            headers=headers,
                        ),
                        timeout=600,
                    ) as r:
                        value = json.load(r)
                    if value["choices"][0]["finish_reason"] != "stop":
                        raise RuntimeError("Reference generation truncated")
                    (a.output / f"{i}.json").write_text(
                        json.dumps(value, ensure_ascii=False), "utf-8"
                    )
            finally:
                server.terminate()
                server.wait(20)


if __name__ == "__main__":
    main()
