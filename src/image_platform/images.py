from hashlib import sha256
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PREPROCESS = {
    "version": "letterbox-rgb-v1", "exif_transpose": True, "color_mode": "RGB",
    "alpha_background": [255, 255, 255], "padding_color": [255, 255, 255],
    "resize": "fit_long_edge_and_pad", "interpolation": "bicubic",
    "rounding": "floor_x_plus_half", "padding_alignment": "center_extra_right_bottom",
    "rescale": 1 / 255, "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225],
    "layout": "NCHW", "max_decode_pixels": 80_000_000,
}


def file_sha(path):
    hasher = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def decode(path, expected_sha=None, contract=None):
    contract = contract or PREPROCESS
    with Path(path).open("rb") as stream:
        # A single read(100 MiB) reserves that buffer even for tiny JPEGs on
        # Windows. Bound each allocation while retaining the hard byte limit.
        chunks, total = [], 0
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise ValueError("图片文件超过 100 MiB")
            chunks.append(chunk)
        blob = b"".join(chunks)
    digest = sha256(blob).hexdigest()
    if expected_sha and digest != expected_sha:
        raise ValueError("input_changed: 文件内容与快照不符")
    try:
        source = Image.open(BytesIO(blob))
    except Image.DecompressionBombError as exc:
        raise ValueError("图片超过解码安全限制") from exc
    with source:
        if source.format not in {"JPEG", "PNG", "WEBP", "BMP"}:
            raise ValueError("不支持的图片格式")
        if getattr(source, "n_frames", 1) != 1:
            raise ValueError("首版仅支持静态单帧图片")
        if source.width * source.height > contract["max_decode_pixels"]:
            raise ValueError("图片超过最大解码像素限制")
        ImageOps.exif_transpose(source, in_place=True)
        image = source
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, tuple(contract["alpha_background"]) + (255,))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")
    return image, digest


def tensor_from_image(image, height=224, width=224, contract=None):
    contract = contract or PREPROCESS
    ratio = min(width / image.width, height / image.height)
    size = (max(1, min(width, math.floor(image.width * ratio + 0.5))),
            max(1, min(height, math.floor(image.height * ratio + 0.5))))
    scaled = image.resize(size, Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (width, height), tuple(contract["padding_color"]))
    canvas.paste(scaled, ((width - size[0]) // 2, (height - size[1]) // 2))
    # RGB contains exactly 256 values per channel. A bounded LUT preserves the
    # original FP32 operation order while avoiding three full float image arrays.
    lookup = normalization_lookup(tuple(contract["mean"]), tuple(contract["std"]), contract["rescale"])
    pixels = np.asarray(canvas).transpose(2, 0, 1)
    return np.ascontiguousarray(lookup[np.arange(3)[:, None, None], pixels])


@lru_cache(maxsize=32)
def normalization_lookup(mean, std, rescale):
    values = np.arange(256, dtype=np.float32)[None, :] * np.float32(rescale)
    return (values - np.asarray(mean, dtype=np.float32)[:, None]) / np.asarray(std, dtype=np.float32)[:, None]


def preprocess(path, expected_sha=None, height=224, width=224, contract=None):
    image, _ = decode(path, expected_sha, contract)
    return tensor_from_image(image, height, width, contract)


def thumbnail(path, destination, expected_sha):
    image, _ = decode(path, expected_sha)
    image.thumbnail((480, 360), Image.Resampling.LANCZOS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="JPEG", quality=85)
