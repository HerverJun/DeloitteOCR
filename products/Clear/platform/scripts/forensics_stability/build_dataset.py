"""Generate 64 authored base families and 320 source-grouped JPEG controls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SEED = 20261014
SEAM_BASES = (0, 1, 16, 17, 24, 25, 32, 33, 40, 44, 48, 60)
FONT = Path('F:/OCR/frontend/public/brand/fonts/NotoSansSC.ttf')


def local_f(path):
    path = Path(path).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('All evaluation files must stay on local F:')
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('Evaluation output cannot use redirected paths')
    return path


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def partition(index):
    local, count = (index, 32) if index < 32 else (index - 32, 16) if index < 48 else (index - 48, 16)
    return 'development' if local < count // 2 else 'calibration' if local < count * 3 // 4 else 'final_test'


def make_base(index):
    rng = np.random.default_rng(SEED + index)
    family = 'document' if index < 32 else 'table' if index < 48 else 'controlled_texture'
    width, height = (1200, 896) if index in SEAM_BASES else ((448, 640, 800, 960)[index % 4], (320, 480, 600, 720)[index % 4])
    paper = rng.normal(248, 1.4, (height, width, 1)).clip(0, 255).astype('uint8')
    rgb = np.repeat(paper, 3, axis=2)
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT), max(14, width // 42))
    small = ImageFont.truetype(str(FONT), max(12, width // 54))
    if family == 'document':
        draw.text((28, 18), '合成审计资料 · 仅作工程评测', font=font, fill=(28, 31, 35))
        draw.text((28, 60), f'资料编号 SYN-{index:03d}    日期 2026-10-14', font=small, fill=(46, 48, 52))
        for row, y in enumerate(range(110, height - 30, max(28, height // 17))):
            number = int(rng.integers(1000, 99999))
            draw.text((28, y), f'项目 {row + 1:02d}  测试业务记录  金额 {number}.00 元', font=small, fill=(38, 40, 44))
        draw.rectangle((width * .72, height * .68, width * .94, height * .86), outline=(137, 35, 35), width=3)
    elif family == 'table':
        draw.text((24, 16), '合成明细表 · 无真实业务资料', font=font, fill=(32, 33, 36))
        cols, rows = 6, 12
        x0, y0 = 24, 70
        cell_w, cell_h = (width - 48) // cols, (height - 94) // rows
        for column in range(cols + 1):
            draw.line((x0 + column * cell_w, y0, x0 + column * cell_w, y0 + rows * cell_h), fill=(115, 118, 122))
        for row in range(rows + 1):
            draw.line((x0, y0 + row * cell_h, x0 + cols * cell_w, y0 + row * cell_h), fill=(115, 118, 122))
        for row in range(rows):
            for column in range(cols):
                draw.text((x0 + column * cell_w + 3, y0 + row * cell_h + 2), f'{int(rng.integers(10, 999))}', font=small, fill=(42, 43, 46))
    else:
        yy, xx = np.mgrid[:height, :width]
        noise = rng.normal(0, 12, (height, width))
        base = 128 + 40 * np.sin(xx / (7 + index % 9)) + 28 * np.cos(yy / 11) + noise
        image = Image.fromarray(np.stack((base, base * .78 + 35, base * .55 + 57), axis=2).clip(0, 255).astype('uint8'))
        draw = ImageDraw.Draw(image)
        for _ in range(14):
            x, y = int(rng.integers(0, width - 60)), int(rng.integers(0, height - 60))
            draw.ellipse((x, y, x + 48, y + 48), outline=(235, 210, 140), width=3)
    return image, family, font


def save_jpeg(image, path, orientation, quality):
    exif = Image.Exif()
    exif[274] = orientation
    image.save(path, 'JPEG', quality=quality, subsampling=0, exif=exif)


def build(output):
    output = local_f(output)
    if output.exists():
        raise ValueError('Use a new dataset directory; frozen datasets are never overwritten')
    if not FONT.is_file():
        raise ValueError('The existing F-drive font asset is required')
    output.mkdir(parents=True)
    records = []
    for index in range(64):
        original, family, font = make_base(index)
        directory = output / f'base-{index:02d}'
        directory.mkdir()
        orientation = 1 + index % 8
        width, height = original.size
        original_path = directory / 'original.jpg'
        save_jpeg(original, original_path, orientation, 95)
        with Image.open(original_path) as decoded:
            decoded_rgb = decoded.convert('RGB').copy()
        for variant in ('original', 'normal_recompression', 'normal_resize', 'modified_text', 'modified_patch'):
            image = decoded_rgb.copy()
            mask = Image.new('L', image.size, 0)
            operations = []
            quality = 95
            if variant == 'normal_recompression':
                quality = 70
                operations = [{'operation': 'normal_jpeg_recompression', 'quality': quality}]
            elif variant == 'normal_resize':
                image = image.resize((max(64, width * 3 // 4), max(64, height * 3 // 4)), Image.Resampling.BICUBIC)
                mask = Image.new('L', image.size, 0)
                quality = 85
                operations = [{'operation': 'normal_resize', 'scale': .75, 'quality': quality}]
            elif variant in {'modified_text', 'modified_patch'}:
                target_w, target_h = min(144, width // 3), min(82, height // 3)
                x = 1024 - target_w // 2 if index in SEAM_BASES else width * 3 // 5
                y = height // 2 - target_h // 2
                box = (x, y, min(width, x + target_w), min(height, y + target_h))
                if variant == 'modified_text':
                    draw = ImageDraw.Draw(image)
                    draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=(251, 251, 249))
                    draw.text((x + 3, y + 4), '金额 90000', font=font, fill=(20, 22, 24))
                else:
                    donor = (24, 90, 24 + box[2] - box[0], 90 + box[3] - box[1])
                    image.paste(decoded_rgb.crop(donor), box)
                    operations.append({'operation': 'copy_patch', 'donor_box': list(donor)})
                ImageDraw.Draw(mask).rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=255)
                operations.append({'operation': variant, 'affected_pixel_edge_box': list(box),
                                   'mask_definition': 'operation footprint before final JPEG encoding'})
            path = original_path if variant == 'original' else directory / (variant + '.jpg')
            if variant != 'original':
                save_jpeg(image, path, orientation, quality)
            mask_path = directory / (variant + '.mask.png')
            mask.save(mask_path)
            records.append({'sample_id': f'b{index:02d}-{variant}', 'base_id': f'b{index:02d}',
                            'family': family, 'split': partition(index), 'variant': variant,
                            'is_modified': variant.startswith('modified_'),
                            'input': path.relative_to(output).as_posix(), 'input_sha256': digest(path),
                            'mask': mask_path.relative_to(output).as_posix(), 'mask_sha256': digest(mask_path),
                            'stored_size': list(image.size), 'exif_orientation': orientation,
                            'mask_coordinate_system': 'stored_pixels', 'operations': operations,
                            'seam_comparison': index in SEAM_BASES and variant in {'original', 'modified_patch'},
                            'seed': SEED + index,
                            'rights': 'Self-authored synthetic control; no real business records or external photographs'})
    manifest = {'format': 'forensics-stability-dataset-v1', 'seed': SEED,
                'generator_sha256': digest(__file__), 'font_sha256': digest(FONT),
                'base_families': 64, 'samples': records,
                'split_base_counts': {'development': 32, 'calibration': 16, 'final_test': 16},
                'scope': 'Synthetic stability and relative-quality evaluation only; no real scan, screen-recapture, natural-image or business accuracy qualification',
                'mask_policy': 'Known editing-operation footprints in stored pixels; never generated from model output',
                'public_regression_policy': 'Existing repeatedly inspected upstream examples remain engineering_regression only'}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'dataset': str(output), 'samples': len(records), 'base_families': 64,
                      'manifest_sha256': digest(output / 'manifest.json')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
