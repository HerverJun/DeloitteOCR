"""Pinned native whole-image detectors; no network, training, or surrogate scores.

The persistent ``load_model(...).score(path)`` API is shared by the worker and
the independent evaluation harness. Scores are the official sigmoid outputs,
not calibrated probabilities. Each model has its original image transform.
"""
from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time


LIMITATIONS = [
    '实验能力；尚未完成独立低误报验收，不参与默认审计判断',
    '模型分数不是 AI 生成概率；低分不能证明图片真实',
    '中文文档、截图、压缩和真实业务资料的准确率须分别验证',
    '整图分类不提供篡改区域定位；局部编辑请结合 SAFIRE 复核',
]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_assets(binding):
    from runtime_paths import require_path
    root = require_path(binding['asset_root'], 'AIGC assets', role='read')
    for name, identity in binding['resources'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Pinned AIGC asset missing or outside its root: ' + name)
        if path.stat().st_size != identity['bytes'] or digest(path) != identity['sha256']:
            raise ValueError('Pinned AIGC asset identity changed: ' + name)
    return root


def decode_image(path):
    from PIL import Image, ImageOps
    source = Path(path)
    if source.stat().st_size > 32 * 1024**2:
        raise ValueError('Image exceeds 32 MiB')
    with Image.open(source) as encoded:
        if encoded.format not in {'JPEG', 'PNG', 'WEBP', 'BMP', 'TIFF'} or getattr(encoded, 'n_frames', 1) != 1:
            raise ValueError('Only single-frame JPEG/PNG/WebP/BMP/TIFF are supported')
        if encoded.width * encoded.height > 12_000_000:
            raise ValueError('Image exceeds 12 million pixels')
        metadata = {'encoded_width':encoded.width, 'encoded_height':encoded.height,
                    'exif_orientation':int(encoded.getexif().get(274,1)), 'source_format':encoded.format}
        oriented=ImageOps.exif_transpose(encoded)
        if 'A' in oriented.getbands() or 'transparency' in oriented.info:
            rgba=oriented.convert('RGBA')
            white=Image.new('RGBA',rgba.size,(255,255,255,255))
            image=Image.alpha_composite(white,rgba).convert('RGB')
            metadata['alpha_handling']='composite_over_white'
        else:
            image=oriented.convert('RGB')
            metadata['alpha_handling']='opaque_rgb'
        image.load()
    metadata.update(width=image.width, height=image.height, source_sha256=digest(source))
    return image, metadata


def _module(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
        submodule_search_locations=[str(Path(path).parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class NativeDetector:
    def __init__(self, engine, binding, device='cuda', feature_batch=32, verify=True, check=None):
        self.binding = binding
        self.engine = engine
        self.device = device
        self.check = check or (lambda: None)
        self.feature_batch = int(feature_batch)
        if self.feature_batch not in {1,2,4,8,16,32}:
            raise ValueError('feature_batch must be a power of two up to 32')
        self.root = verified_assets(binding) if verify else Path(binding['asset_root']).resolve()
        settings = binding['settings']
        dependency_path = settings.get('dependencies')
        if dependency_path:
            sys.path.insert(0, str(self._path(dependency_path)))
        import torch
        from torchvision import transforms
        self.torch = torch
        self.transforms = transforms
        if device not in {'cuda','cpu'}:
            raise ValueError('Only explicit cuda or cpu devices are supported')
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable; CPU is never silently substituted')
        if device == 'cuda':
            torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        self.check()
        if engine == 'community_forensics384':
            self._community_forensics(settings)
        elif engine == 'univfd':
            self._univfd(settings)
        elif engine == 'spai':
            self._spai(settings)
        else:
            raise ValueError('Unknown AIGC detector engine: ' + engine)
        self.model.to(device).eval()
        self.load_seconds = time.monotonic()-started
        self.check()

    def _path(self, value):
        path=(self.root/value).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Model path outside pinned root')
        return path

    def _community_forensics(self, settings):
        import timm
        from safetensors.torch import load_file
        torch=self.torch
        # Original classifier without the constructor's redundant ImageNet download.
        class ViTClassifier(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.vit=timm.create_model('vit_small_patch16_384.augreg_in21k_ft_in1k',pretrained=False)
                self.vit.head=torch.nn.Linear(384,1)
            def forward(self,x):
                return self.vit(x)
        self.model=ViTClassifier()
        self.model.load_state_dict(load_file(str(self._path(settings['checkpoint'])),device='cpu'),strict=True)
        T=self.transforms
        self.transform=T.Compose([T.Resize(440),T.CenterCrop(384),T.ToTensor(),
            T.Normalize([.485,.456,.406],[.229,.224,.225])])
        self.preprocessing='community-forensics384-resize440-centercrop384-imagenet-v1'

    def _clip_package(self, settings):
        import importlib
        import types
        # Upstream uses the retired setuptools namespace solely to access the
        # independently installed packaging.version parser.
        if 'pkg_resources' not in sys.modules and importlib.util.find_spec('pkg_resources') is None:
            bridge=types.ModuleType('pkg_resources')
            bridge.packaging=importlib.import_module('packaging')
            importlib.import_module('packaging.version')
            sys.modules['pkg_resources']=bridge
        root=self._path(settings.get('clip_source_root',settings['source_root']))
        package=root/'models'/'clip'
        if 'clear_aigc_clip' not in sys.modules or not hasattr(sys.modules['clear_aigc_clip'],'clip'):
            _module('clear_aigc_clip',package/'__init__.py',package=True)
        return sys.modules['clear_aigc_clip'].clip

    def _univfd(self, settings):
        torch=self.torch
        clip=self._clip_package(settings)
        backbone,_=clip.load(str(self._path(settings['clip_checkpoint'])),device='cpu',download_root=str(self.root/'cache'/'clip'))
        class CLIPModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.model=backbone
                self.fc=torch.nn.Linear(768,1)
            def forward(self,x):
                return self.fc(self.model.encode_image(x))
        self.model=CLIPModel()
        state=torch.load(self._path(settings['checkpoint']),map_location='cpu',weights_only=True)
        self.model.fc.load_state_dict(state,strict=True)
        self.model.float()
        T=self.transforms
        # Detection test.py explicitly has no Resize. Generic CLIP preprocessing differs.
        self.transform=T.Compose([T.CenterCrop(224),T.ToTensor(),
            T.Normalize([.48145466,.4578275,.40821073],[.26862954,.26130258,.27577711])])
        self.preprocessing='univfd-centercrop224-no-resize-clipnorm-v1'

    def _spai(self, settings):
        import types
        torch=self.torch
        source=self._path(settings['source_root'])
        clip=self._clip_package(settings)
        sys.modules.setdefault('clip',sys.modules['clear_aigc_clip'])
        # sid imports a plotting utility that inference without attention export never
        # calls. Exclude that training/plotting dependency; native model code is intact.
        plotting=types.ModuleType('spai.utils')
        def unsupported_attention_export(*args,**kwargs):
            raise RuntimeError('Attention maps are not forensic localization and are not exported')
        plotting.save_image_with_attention_overlay=unsupported_attention_export
        sys.modules['spai.utils']=plotting
        sys.path.insert(0,str(source))
        from spai.config import get_custom_config
        from spai.models import build_cls_model
        config=get_custom_config(str(self._path(settings['config'])))
        config.defrost()
        config.MODEL.FEATURE_EXTRACTION_BATCH=self.feature_batch
        config.freeze()
        self.model=build_cls_model(config)
        state=torch.load(self._path(settings['checkpoint']),map_location='cpu',weights_only=False)
        state=state['model'] if 'model' in state else state
        if any(name.startswith('encoder.') for name in state):
            state={name.removeprefix('encoder.'):value for name,value in state.items() if name.startswith('encoder.')}
        self.model.load_state_dict(state,strict=True)
        def original_resolution_transform(image):
            # Albumentations 1.4.14 PadIfNeeded defaults to reflected padding,
            # then Normalize(mean=0,std=1) scales uint8 to positive [0,1].
            # Preserve that minimum-size edge case without importing training
            # augmentation and LMDB/metrics packages into a pure inference worker.
            import cv2
            import numpy as np
            array=np.asarray(image)
            pad_h=max(224-array.shape[0],0)
            pad_w=max(224-array.shape[1],0)
            if pad_h or pad_w:
                array=cv2.copyMakeBorder(array,pad_h//2,pad_h-pad_h//2,
                    pad_w//2,pad_w-pad_w//2,cv2.BORDER_REFLECT_101)
            array=np.ascontiguousarray(array,dtype=np.float32)*np.float32(1/255)
            return torch.from_numpy(array.transpose(2,0,1))
        self.transform=original_resolution_transform
        self.preprocessing='spai-original-resolution-positive01-nativepatches-v1'

    def score(self, image):
        """Return official sigmoid output, with larger values indicating generated signal."""
        if not hasattr(image,'convert'):
            image,_=decode_image(image)
        self.check()
        torch=self.torch
        tensor=self.transform(image).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            if self.engine == 'spai':
                output=self.model([tensor],feature_extraction_batch_size=self.feature_batch)
            else:
                output=self.model(tensor)
            score=float(torch.sigmoid(output).detach().cpu().reshape(-1)[0])
        if not math.isfinite(score):
            raise RuntimeError('Detector returned non-finite score')
        self.check()
        return score

    def performance(self):
        torch=self.torch
        return {'load_seconds':round(self.load_seconds,4),'feature_batch_size':self.feature_batch,
            'peak_memory_mib':round(torch.cuda.max_memory_allocated()/1024**2,2) if self.device=='cuda' else 0,
            'peak_reserved_memory_mib':round(torch.cuda.max_memory_reserved()/1024**2,2) if self.device=='cuda' else 0}

    def close(self):
        self.model=None
        gc.collect()
        if self.device=='cuda':
            self.torch.cuda.empty_cache()


def load_model(engine, binding, device='cuda', feature_batch=32, verify=True, check=None):
    return NativeDetector(engine,binding,device,feature_batch,verify,check)


def analyze(request,worker):
    from image_geometry import image_geometry
    source=request['inputs'][0]['path']
    image,geometry=decode_image(source)
    if geometry['source_sha256'] != request['inputs'][0]['blob_hash']:
        raise ValueError('Original image hash changed')
    image.save(worker.OUT/'preview.png',format='PNG')
    binding=request['model_binding']
    worker.progress('加载整图 AI 生成检测官方权重')
    model=load_model(binding['engine'],binding,request['options']['device'],
        request.get('runner',{}).get('aigc_feature_batch_size',32),check=worker.CONTROL.check)
    try:
        worker.progress('分析整图生成相关信号')
        started=time.monotonic()
        score=model.score(image)
        if digest(source)!=request['inputs'][0]['blob_hash']:
            raise ValueError('Original image changed during AIGC inference')
        elapsed=time.monotonic()-started
        settings=binding['settings']
        calibration=None
        if settings.get('calibration_file'):
            calibration=json.loads(model._path(settings['calibration_file']).read_text('utf-8'))
            if calibration.get('engine')!=binding['engine'] or calibration.get('preprocessing_version')!=model.preprocessing:
                raise ValueError('Calibration does not match this pinned model preprocessing')
            weights={name:identity['sha256'] for name,identity in binding['resources'].items()
                if name.endswith(('.pt','.pth','.safetensors'))}
            if calibration.get('model_weight_hashes')!=weights:
                raise ValueError('Calibration does not match this pinned model weight identity')
        threshold=calibration.get('threshold') if calibration else None
        if threshold is not None and (isinstance(threshold,bool) or not isinstance(threshold,(float,int))
                or not math.isfinite(threshold) or not 0<=threshold<=1):
            raise ValueError('Invalid calibration threshold')
        state='unable_to_assess' if threshold is None else ('signal_detected' if score>threshold else 'no_significant_signal')
        label={'unable_to_assess':'无法判断','signal_detected':'检出生成相关信号','no_significant_signal':'未发现显著信号'}[state]
        summary={'schema_version':'1.0','state':state,'raw_score':score,
            'score_direction':'higher_is_generated','threshold':threshold,'calibration':calibration,
            'qualification':'experimental','scope':['whole_image','document','general_image'],
            'limitations':list(LIMITATIONS),'preprocessing_version':model.preprocessing}
        display=image_geometry(image.width,image.height,1)
        display.update(source_coordinate_system='exif_transposed_pixels',encoded_geometry=geometry)
        return {'aigc_image':summary,'algorithm':binding['engine'],'engine':binding['engine'],
            'device':model.device,'outcome':'analyzed','width':image.width,'height':image.height,
            'source_sha256':geometry['source_sha256'],'model':{'engine':binding['engine'],
                'version':binding.get('version'),'source_revision':binding.get('code_revision'),
                'preprocessing_version':model.preprocessing,'weight_hashes':{name:identity['sha256']
                    for name,identity in binding['resources'].items() if name.endswith(('.pt','.pth','.safetensors'))}},
            'assessment':{'state':state,'label':label,'score_is_probability':False,'calibration':calibration is not None,
                'threshold':threshold,'reason':'未校准模型分数，仅供实验复核' if threshold is None else '按独立正常集冻结阈值复核'},
            'performance':{**model.performance(),'inference_seconds':round(elapsed,4)},
            'processing':{'display_preview':'preview.png','orientation':'exif_transposed_pixels','geometry':display},
            'limitations':list(LIMITATIONS),'outputs':[]}
    finally:
        model.close()
