"""Reusable CAT-Net session, original JPEG preparation and bounded inference.

No Torch/model imports or output writes occur at import time. Evaluation can
load CatNetSession once, prepare a JPEG once, and compare halo=0, halo=64 and
full_image=True using identical original RGB/DCT and one loaded model.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

try:
    from .image_geometry import image_geometry
    from .catnet_tiling import plan_tiles
except ImportError:  # The embedded worker imports siblings as top-level modules.
    from image_geometry import image_geometry
    from catnet_tiling import plan_tiles


@dataclass(frozen=True)
class CatNetInput:
    rgb: object
    coefficients: object
    quantization: object
    geometry: dict
    input_sha256: str


@dataclass(frozen=True)
class CatNetPrediction:
    probabilities: object
    inference: dict


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _native_temporary_root(temporary_root):
    configured = temporary_root or os.environ.get('CLEAR_NATIVE_TEMP_ROOT') or os.environ.get('CLEAR_PADDLE_TEMP') or os.environ.get('TEMP')
    if not configured:
        raise ValueError('CAT-Net requires an explicit native temporary directory')
    from runtime_paths import require_path
    root = require_path(configured, 'CAT-Net native temporary directory', role='write')
    if not str(root).isascii():
        raise ValueError('CAT-Net native temporary directory must be an ASCII path')
    if any(parent.is_symlink() or parent.is_junction() for parent in (root, *root.parents)):
        raise ValueError('CAT-Net native temporary directory cannot be redirected')
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_catnet_jpeg(source, *, temporary_root=None, max_pixels=16_000_000):
    """Read original quantized Y DCT and RGB without rotating or recompressing."""
    import numpy as np
    import jpeglib
    from PIL import Image
    source = Path(source)
    with Image.open(source) as image:
        if image.format != 'JPEG':
            raise ValueError('CAT-Net requires an original JPEG, not a renamed or converted input')
        width, height = image.size
        if width*height > max_pixels:
            raise ValueError('首版深度取证支持最多 1600 万像素 JPEG；原件未修改')
        orientation = image.getexif().get(274)
        rgb = np.asarray(image.convert('RGB')).copy()
    source_hash = file_digest(source)
    # libjpeg uses an ASCII path. The temporary copy is byte-identical.
    with tempfile.TemporaryDirectory(prefix='clear-catnet-', dir=_native_temporary_root(temporary_root)) as directory:
        compatible = Path(directory)/'input.jpg'
        shutil.copyfile(source, compatible)
        if file_digest(compatible) != source_hash:
            raise ValueError('JPEG temporary copy changed')
        jpeg = jpeglib.read_dct(str(compatible))
        y = jpeg.Y
        if y is None:
            raise ValueError('JPEG has no supported luminance DCT component')
        qindex = int(jpeg.quant_tbl_no[0])
        quantization = jpeg.qt[qindex].copy()
        coefficients = y.transpose(0,2,1,3).reshape(y.shape[0]*8, y.shape[1]*8).copy()
    if quantization.shape != (8, 8) or coefficients.shape[0] < height or coefficients.shape[1] < width:
        raise ValueError('JPEG coefficient or quantization dimensions do not match stored pixels')
    return CatNetInput(rgb, coefficients, quantization, image_geometry(width, height, orientation), source_hash)


class CatNetSession:
    """One loaded model. Call infer repeatedly; this API never writes results."""
    def __init__(self, model_root, device='cpu'):
        import torch
        from yacs.config import CfgNode
        import yaml
        self.root = Path(model_root)
        self.device = device
        self.torch_version = torch.__version__
        sys.path.insert(0, str(self.root/'source'))
        from lib.models.network_CAT import CAT_Net
        config = CfgNode(yaml.safe_load((self.root/'source/experiments/CAT_full.yaml').read_text('utf-8')))
        config.MODEL.PRETRAINED_RGB = ''
        config.MODEL.PRETRAINED_DCT = ''
        model = CAT_Net(config)
        checkpoint = torch.load(self.root/'CAT_full_v2.pth.tar', map_location='cpu', weights_only=False)
        model.load_state_dict(checkpoint['state_dict'], strict=True)
        self.model = model.eval().to(device)
        self.model_hashes = {
            'CAT_full_v2': file_digest(self.root/'CAT_full_v2.pth.tar'),
            'network': file_digest(self.root/'source/lib/models/network_CAT.py'),
        }

    def infer(self, prepared, *, tile_size=1024, halo=0, full_image=False, check=None, progress=None):
        """Return stored-pixel probabilities and a versioned inference description.

        progress(completed_tiles, total_tiles) runs after each completed tile.
        full_image=True is an explicit evaluation override of the input limit.
        check() may raise the caller's cancellation exception between tiles.
        """
        import numpy as np
        import torch
        import torch.nn.functional as functional
        check = check or (lambda: None)
        progress = progress or (lambda completed, total: None)
        height, width = prepared.rgb.shape[:2]
        hp, wp = prepared.coefficients.shape
        plan = plan_tiles(width, height, wp, hp, tile_size=tile_size, halo=halo, full_image=full_image)
        check()
        qtable = torch.tensor(prepared.quantization, dtype=torch.float32)[None,None].to(self.device)
        padded = np.full((hp,wp,3), 127.5, dtype=np.float32)
        padded[:height,:width] = prepared.rgb
        probabilities = np.zeros((height,width), dtype=np.float32)
        with torch.inference_mode():
            for index, tile in enumerate(plan.tiles):
                check()
                left, top, right, bottom = tile.input_box
                pixels = padded[top:bottom,left:right]
                block = np.abs(prepared.coefficients[top:bottom,left:right])
                volume = np.stack([block==i for i in range(20)]+[block>=20]).astype(np.float32)
                tensor = torch.cat([
                    torch.from_numpy(pixels.transpose(2,0,1)).float().sub(127.5).div(127.5),
                    torch.from_numpy(volume)])[None].to(self.device)
                logits = self.model(tensor,qtable)
                prediction = functional.interpolate(logits,size=pixels.shape[:2],mode='bilinear',align_corners=True).softmax(dim=1)[0,1].cpu().numpy()
                check()
                x1,y1,x2,y2 = tile.output_box
                cx1,cy1,cx2,cy2 = tile.crop_box
                probabilities[y1:y2,x1:x2] = prediction[cy1:cy2,cx1:cx2]
                progress(index+1,len(plan.tiles))
                # Do not retain one tile's device tensors while allocating the next.
                del tensor, logits, prediction, volume
        check()
        return CatNetPrediction(probabilities, plan.metadata)
