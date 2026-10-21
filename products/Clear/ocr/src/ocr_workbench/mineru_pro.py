"""Pinned MinerU Pro, in its own offline Python 3.12 CUDA/BF16 process."""
from importlib.metadata import version
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import time

MODEL = 'MinerU2.5-Pro-2605-1.2B'
UTILS_VERSION = '2.0.5'
ENGINE_SPEC = {
    'name': 'MinerU 2.5 Pro · 2605',
    'route': 'Isolated Python 3.12 / mineru-vl-utils 2.0.5 / Transformers CUDA BF16',
    'models': [MODEL],
    'capabilities': {'text': True, 'text_coordinates': False, 'region_coordinates': True,
                     'confidence': False, 'tables': True, 'formula': True},
}


def engine_catalog(bundle):
    """Keep sealed asset engines; extend only an explicitly registered Pro runtime.

    Thin candidates reuse the old asset bundle read-only. The same declaration
    must reach registry, adapter and worker, whose bundle can predate Pro.
    """
    values = json.loads((Path(bundle) / 'config/engines.json').read_text('utf-8'))
    if os.environ.get('CLEAR_MINERU_PRO_PYTHON'):
        values.setdefault('mineru_pro', deepcopy(ENGINE_SPEC))
    return values


def model_directory(models):
    requested = Path(os.environ['CLEAR_MINERU_PRO_MODEL']) if os.environ.get('CLEAR_MINERU_PRO_MODEL') else Path(models) / MODEL
    resolved = requested.resolve()
    if requested.absolute().drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
        raise ValueError('MinerU Pro model must remain on local F drive')
    return resolved


def normalize_blocks(items, width, height):
    """Keep reading order and region evidence; never invent cell coordinates."""
    raw, blocks = [], []
    if not isinstance(items, (list, tuple)):
        raise ValueError('MinerU Pro did not return a block list')
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError('MinerU Pro returned an invalid content block')
        value = dict(item)
        raw.append(value)
        content = value.get('content')
        if content is not None and not isinstance(content, str):
            raise ValueError('MinerU Pro block content is not text')
        box = value.get('bbox')
        polygon = None
        if (isinstance(box, (list, tuple)) and len(box) == 4
                and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
                and box[0] < box[2] and box[1] < box[3]):
            left, top, right, bottom = box[0]*width, box[1]*height, box[2]*width, box[3]*height
            polygon = [[left, top], [right, top], [right, bottom], [left, bottom]]
        blocks.append({'text': content or '', 'kind': value.get('type', 'unknown'),
                       'polygon': polygon, 'confidence': None, 'reading_order': index,
                       'angle': value.get('angle'), 'source': 'mineru_pro',
                       'source_level': 'region' if polygon else 'page',
                       'merge_prev': value.get('merge_prev', False)})
    return {'content_list': raw, 'model': MODEL, 'mineru_vl_utils': UTILS_VERSION,
            'coordinate_system': 'normalized_page', 'precision': 'bfloat16'}, blocks


class Session:
    def __init__(self, models, progress=None):
        started = time.perf_counter()
        phase = progress or (lambda name: None)
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError('MinerU Pro requires its isolated Python 3.12 runtime')
        if version('mineru-vl-utils') != UTILS_VERSION:
            raise RuntimeError('MinerU Pro requires mineru-vl-utils 2.0.5')
        local = model_directory(models)
        if not local.is_dir() or not (local / 'source-manifest.json').is_file():
            raise FileNotFoundError('MinerU Pro local model/provenance is missing; automatic download is disabled')
        phase('import_transformers')
        import torch
        from transformers import AutoConfig, PretrainedConfig, Qwen2VLForConditionalGeneration, AutoProcessor, AutoImageProcessor
        from mineru_vl_utils import MinerUClient
        phase('cuda_check')
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError('MinerU Pro requires a CUDA GPU with BF16 support')
        phase('initialize_pipeline')
        # Same tied-text-head correction as the pinned upstream loading helper.
        config = AutoConfig.from_pretrained(str(local), local_files_only=True)
        raw_config, _ = PretrainedConfig.get_config_dict(str(local), local_files_only=True)
        text_config = config.get_text_config(decoder=True)
        tied = raw_config.get('tie_word_embeddings', text_config.tie_word_embeddings)
        text_config.tie_word_embeddings = config.tie_word_embeddings = tied
        self.model = Qwen2VLForConditionalGeneration.from_pretrained(
            str(local), config=config, dtype=torch.bfloat16, device_map='cuda:0',
            local_files_only=True, attn_implementation='sdpa').eval()
        processor = AutoProcessor.from_pretrained(str(local), local_files_only=True)
        processor.image_processor = AutoImageProcessor.from_pretrained(
            str(local), backend='torchvision', local_files_only=True)
        self.client = MinerUClient(backend='transformers', model=self.model, processor=processor,
            image_analysis=False, abandon_paratext=False, batch_size=1,
            use_tqdm=False, enable_cross_page_table_merge=False)
        self.loaded = time.perf_counter() - started
        phase('ready')

    def recognize(self, image, output=None):
        from PIL import Image
        import torch
        with Image.open(image) as source:
            pixels = source.convert('RGB')
            try:
                with torch.inference_mode():
                    items = self.client.two_step_extract(pixels)
                raw, blocks = normalize_blocks(items, *pixels.size)
            finally:
                pixels.close()
        torch.cuda.empty_cache()
        return raw, blocks, self.loaded

    def close(self):
        self.client = self.model = None
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()


def recognize(models, image, output=None):
    session = Session(models)
    try:
        return session.recognize(image, output)
    finally:
        session.close()
