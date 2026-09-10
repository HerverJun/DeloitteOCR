"""Official GLM layout/crop/format components with local Transformers BF16 recognition.

Sequential orchestration deliberately propagates failures; SDK async workers can
otherwise turn failed layout/recognition into apparently successful empty output.
"""
import base64
import io
import json
import time


def recognize(models, image_path):
    import torch
    from PIL import Image
    from transformers import AutoProcessor, AutoModelForImageTextToText
    from glmocr.config import load_config
    from glmocr.layout import PPDocLayoutDetector
    from glmocr.dataloader import PageLoader
    from glmocr.postprocess import ResultFormatter
    from glmocr.utils.image_utils import crop_image_region

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError('GLM requires CUDA with BF16 support')
    start = time.perf_counter()
    config = load_config()
    config.pipeline.maas.enabled = False
    config.pipeline.max_workers = 1
    config.pipeline.layout.model_dir = str(models / 'PP-DocLayoutV3_safetensors')
    config.pipeline.layout.device = 'cpu'
    detector = PPDocLayoutDetector(config.pipeline.layout)
    detector.start()
    model = AutoModelForImageTextToText.from_pretrained(str(models / 'GLM-OCR'),
                dtype=torch.bfloat16, local_files_only=True, attn_implementation='sdpa').to('cuda').eval()
    processor = AutoProcessor.from_pretrained(str(models / 'GLM-OCR'), local_files_only=True)
    loader = PageLoader(config.pipeline.page_loader)
    formatter = ResultFormatter(config.pipeline.result_formatter)
    loaded = time.perf_counter() - start
    image = Image.open(image_path).convert('RGB')
    pages, _ = detector.process([image], save_visualization=False, global_start_idx=0,
                                use_polygon=config.pipeline.layout.use_polygon)
    regions = pages[0]
    generated = []
    for region in regions:
        task = region.get('task_type', 'text')
        if task in {'skip', 'abandon'}:
            region['content'] = ''
            continue
        crop = crop_image_region(image, region['bbox_2d'],
                region.get('polygon') if config.pipeline.layout.use_polygon else None)
        payload = loader.build_request_from_image(crop, task_type=task)
        # Preserve the official SDK image resizing/JPEG and task prompt.
        content = payload['messages'][0]['content']
        for item in content:
            if item['type'] == 'image_url':
                encoded = item['image_url']['url'].split(',', 1)[1]
                item.clear()
                item.update(type='image', image=Image.open(io.BytesIO(base64.b64decode(encoded))).convert('RGB'))
        inputs = processor.apply_chat_template(payload['messages'], tokenize=True,
                    add_generation_prompt=True, return_dict=True, return_tensors='pt').to(model.device)
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=payload['max_tokens'],
                                    do_sample=False, repetition_penalty=payload['repetition_penalty'])
        ids = output[0][inputs['input_ids'].shape[-1]:]
        if len(ids) >= payload['max_tokens']:
            raise RuntimeError('GLM generation reached token limit; output may be truncated')
        text = processor.decode(ids, skip_special_tokens=True)
        region['content'] = text
        generated.append({'bbox_2d': region['bbox_2d'], 'task': task, 'content': text})
    formatted_json, markdown, _ = formatter.process([regions])
    blocks = []
    for region in regions:
        box = region['bbox_2d']
        x1, y1, x2, y2 = [box[0]*image.width/1000, box[1]*image.height/1000,
                          box[2]*image.width/1000, box[3]*image.height/1000]
        blocks.append({'kind': region['label'], 'text': region.get('content', ''),
                       'confidence': None, 'polygon': [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]})
    return {'regions': regions, 'generated': generated,
            'official_formatted_json': json.loads(formatted_json), 'official_markdown': markdown}, blocks, loaded
