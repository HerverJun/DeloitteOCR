"""Official GLM layout/crop/format components with local Transformers BF16 recognition.

Sequential orchestration deliberately propagates failures; SDK async workers can
otherwise turn failed layout/recognition into apparently successful empty output.
"""

import base64
import io
import json
import time


class LocalTextTruncatedOutput(RuntimeError):
    def __init__(self, evidence):
        self.recognition_evidence = evidence
        super().__init__('GLM local text generation reached token limit; output may be truncated')


def recognize_local_text(models, image_path, session=None):
    """Decode one supplied crop with the official text prompt, without layout.

    This returns one whole-crop text block. It creates no table, guessed cell
    structure, formatting, confidence, source position or acceptance decision.
    """
    import hashlib
    import torch
    from PIL import Image

    _, model, processor, loader, _, loaded, _ = session or load_session(models)
    with Image.open(image_path) as opened:
        image = opened.convert('RGB')
    payload = loader.build_request_from_image(image, task_type='text')
    # Preserve the SDK's resize, JPEG, text prompt and decoding parameters.
    prompt = [{ 'role':message['role'], 'content':[dict(item) for item in message['content']
              if item.get('type') != 'image_url']} for message in payload['messages']]
    image_bindings = []
    for message in payload['messages']:
        for item in message['content']:
            if item['type'] == 'image_url':
                encoded = item['image_url']['url'].split(',', 1)[1]
                encoded_bytes = base64.b64decode(encoded)
                with Image.open(io.BytesIO(encoded_bytes)) as opened:
                    decoder_image = opened.convert('RGB')
                image_bindings.append({'encoded_image_sha256':hashlib.sha256(encoded_bytes).hexdigest(),
                    'decoder_image_size':list(decoder_image.size),
                    'decoder_rgb_pixels_sha256':hashlib.sha256(decoder_image.tobytes()).hexdigest()})
                item.clear()
                item.update(type='image', image=decoder_image)
    if len(image_bindings) != 1:
        raise ValueError('Official local text loader must produce exactly one crop image')
    inputs = processor.apply_chat_template(payload['messages'], tokenize=True,
        add_generation_prompt=True, return_dict=True, return_tensors='pt').to(model.device)
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=payload['max_tokens'],
            do_sample=False, repetition_penalty=payload['repetition_penalty'])
    ids = output[0][inputs['input_ids'].shape[-1]:]
    text = processor.decode(ids, skip_special_tokens=True)
    with open(image_path, 'rb') as handle:
        source_sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    raw = {'recognition_mode':'local_text', 'layout_applied':False, 'formatter_applied':False,
        'input_image_sha256':source_sha,
        'input_image_size':list(image.size), 'request':{'task_type':'text','prompt':prompt,
            'max_new_tokens':payload['max_tokens'],'do_sample':False,
            'repetition_penalty':payload['repetition_penalty'],'image_bindings':image_bindings},
        'generated':[{'bbox_2d':[0,0,1000,1000], 'task':'text', 'content':text,
            'token_count':len(ids),'token_limit':payload['max_tokens']}],
        'truncated':len(ids)>=payload['max_tokens']}
    if raw['truncated']:
        raise LocalTextTruncatedOutput(raw)
    blocks = [{'kind':'text', 'text':text, 'confidence':None,
        'polygon':[[0,0],[image.width,0],[image.width,image.height],[0,image.height]]}]
    return raw, blocks, loaded


def load_session(models):
    import torch
    from PIL import Image
    from transformers import AutoProcessor, AutoModelForImageTextToText
    from glmocr.config import load_config
    from glmocr.layout import PPDocLayoutDetector
    from glmocr.dataloader import PageLoader
    from glmocr.postprocess import ResultFormatter
    from glmocr.utils.image_utils import crop_image_region

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("GLM requires CUDA with BF16 support")
    start = time.perf_counter()
    config = load_config()
    config.pipeline.maas.enabled = False
    config.pipeline.max_workers = 1
    config.pipeline.layout.model_dir = str(models / "PP-DocLayoutV3_safetensors")
    config.pipeline.layout.device = "cpu"
    detector = PPDocLayoutDetector(config.pipeline.layout)
    detector.start()
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
    loader = PageLoader(config.pipeline.page_loader)
    formatter = ResultFormatter(config.pipeline.result_formatter)
    loaded = time.perf_counter() - start
    return (detector, model, processor, loader, formatter, loaded, config)


def recognize(models, image_path, session=None):
    import torch
    from PIL import Image
    from glmocr.utils.image_utils import crop_image_region

    detector, model, processor, loader, formatter, loaded, config = (
        session or load_session(models)
    )
    image = Image.open(image_path).convert("RGB")
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
            region.get("polygon") if config.pipeline.layout.use_polygon else None,
        )
        payload = loader.build_request_from_image(crop, task_type=task)
        # Preserve the official SDK image resizing/JPEG and task prompt.
        content = payload["messages"][0]["content"]
        for item in content:
            if item["type"] == "image_url":
                encoded = item["image_url"]["url"].split(",", 1)[1]
                item.clear()
                item.update(
                    type="image",
                    image=Image.open(io.BytesIO(base64.b64decode(encoded))).convert(
                        "RGB"
                    ),
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
                repetition_penalty=payload["repetition_penalty"]
            )
        ids = output[0][inputs["input_ids"].shape[-1] :]
        if len(ids) >= payload["max_tokens"]:
            raise RuntimeError(
                "GLM generation reached token limit; output may be truncated"
            )
        text = processor.decode(ids, skip_special_tokens=True)
        region["content"] = text
        generated.append({"bbox_2d": region["bbox_2d"], "task": task, "content": text})
    formatted_json, markdown, _ = formatter.process([regions])
    blocks = []
    for region in regions:
        box = region["bbox_2d"]
        x1, y1, x2, y2 = [
            box[0] * image.width / 1000,
            box[1] * image.height / 1000,
            box[2] * image.width / 1000,
            box[3] * image.height / 1000,
        ]
        blocks.append(
            {
                "kind": region["label"],
                "text": region.get("content", ""),
                "confidence": None,
                "polygon": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
            }
        )
    return (
        {
            "regions": regions,
            "generated": generated,
            "official_formatted_json": json.loads(formatted_json),
            "official_markdown": markdown,
        },
        blocks,
        loaded,
    )
