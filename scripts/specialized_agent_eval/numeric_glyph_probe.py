"""Bounded raster-template diagnostic for numeric sign glyphs, CPU only.

The existing transcription supplies digits, never the correct sign. Every
supported sign variant is rendered, and only a unique match of the complete
numeric ink can propose a glyph. Unknown fonts/blur/ambiguous shapes stay
unresolved. This does not change a native candidate or release review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment

SIGNS = ('-', '−', '–', '—', '﹣', '－')
FONTS = ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhbd.ttc',
         'C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/calibri.ttf',
         'C:/Windows/Fonts/segoeui.ttf')


def ink_crop(mask):
    ys, xs = np.nonzero(mask)
    return mask[ys.min():ys.max()+1, xs.min():xs.max()+1] if len(xs) else None


def template(text, font, size):
    canvas = Image.new('L', (512, 96), 255)
    ImageDraw.Draw(canvas).text((8, 8), text, font=ImageFont.truetype(font, size), fill=0)
    return ink_crop(np.asarray(canvas) < 128)


def similarity(source, rendered):
    # Isotropic scaling preserves the sign/digit width ratio. Width fitting or
    # stretching would hide glyph differences and is deliberately unavailable.
    if source is None or rendered is None:
        return 0.
    h, w = source.shape
    scale = h/rendered.shape[0]
    target_width = round(rendered.shape[1]*scale)
    if abs(target_width-w) > max(1, w*.015):
        return 0.
    resized = cv2.resize(rendered.astype(np.uint8), (target_width, h), interpolation=cv2.INTER_NEAREST).astype(bool)
    width = max(w, target_width)
    best = 0.
    for offset in range(abs(w-target_width)+1):
        a, b = np.zeros((h, width), bool), np.zeros((h, width), bool)
        source_offset = offset if w < target_width else 0
        rendered_offset = offset if target_width < w else 0
        a[:, source_offset:source_offset+w] = source
        b[:, rendered_offset:rendered_offset+target_width] = resized
        union = np.count_nonzero(a | b)
        if union:
            best = max(best, np.count_nonzero(a & b)/union)
    return best


def match_sign(image, box, value, fonts):
    if not re.fullmatch(r'[-−–—﹣－]\d{1,12}(?:,\d{3})*(?:\.\d{1,8})?', value):
        return None
    x0, y0, x1, y1 = [round(v) for v in box]
    if x1-x0 < 24 or y1-y0 < 18:
        return {'reason': 'numeric_cell_too_small', 'proposal': None}
    # Full observed cell boundaries may be coloured, so remove a fixed small
    # border before reading dark text. No digit or sign box is fabricated.
    crop = image.crop((x0+4, y0+4, x1-4, y1-4)).convert('L')
    source = ink_crop(np.asarray(crop) < 96)
    if source is None or not 8 <= source.shape[0] <= 56 or source.shape[1] > 300:
        return {'reason': 'single_numeric_ink_unavailable', 'proposal': None}
    scores = {}
    for sign in SIGNS:
        options = []
        for font in fonts:
            for size in range(12, 37):
                score = similarity(source, template(sign+value[1:], font, size))
                options.append((score, font, size))
        score, font, size = max(options)
        scores[sign] = {'iou': float(score), 'font_path': font, 'font_size': size}
    ranked = sorted(scores, key=lambda sign: scores[sign]['iou'], reverse=True)
    first, second = scores[ranked[0]]['iou'], scores[ranked[1]]['iou']
    accepted = first >= .985 and first-second >= .10
    return {'reason': 'unique_full_numeric_raster_match' if accepted else 'font_or_sign_ambiguous',
        'proposal': ranked[0]+value[1:] if accepted else None,
        'source_ink_size': [source.shape[1], source.shape[0]], 'scores': scores,
        'best_iou': first, 'distinct_sign_iou_margin': first-second,
        'full_numeric_literal_compared': True, 'native_value_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('observations', 'decisions', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(); require_environment()
    observations_path, decisions_path, output = map(fpath, (args.observations, args.decisions, args.output))
    if output.exists():
        raise ValueError('Use a new immutable glyph diagnostic output')
    fonts = [path for path in FONTS if Path(path).is_file()]
    if not fonts:
        raise ValueError('Supported read-only system fonts are unavailable')
    output.mkdir(parents=True)
    records = {row['id']: row for row in read_json(observations_path)['records']}
    recipe = {'format': 'numeric-sign-raster-diagnostic-v1', 'utc': datetime.now(timezone.utc).isoformat(),
        'helper': bound(__file__), 'observations': bound(observations_path), 'decisions': bound(decisions_path),
        'supported_read_only_fonts': {path: digest(path) for path in fonts}, 'sign_variants': list(SIGNS),
        'font_sizes': [12, 36], 'minimum_iou': .985, 'minimum_distinct_sign_margin': .10,
        'digits_taken_only_from_candidate': True, 'truth_read': False, 'model_calls': 0,
        'native_values_or_review_changed': False, 'automatic_delivery_qualification': False}
    frozen_json(output/'recipe.json', recipe)
    results = []
    for row in read_json(decisions_path)['decisions']:
        if 'complete_observed_table_cell_partition' in row['reasons'] or not row['cells']:
            continue
        raw = records[row['id']]['candidate']['original']
        prep = raw['screen']['prepared']
        path = fpath(prep['input_path'])
        if digest(path) != records[row['id']]['image_sha256']:
            raise ValueError('Observed numeric source pixel identity changed')
        image = Image.open(BytesIO(path.read_bytes())).convert('RGB')
        for cell in row['cells']:
            values = [c['text'] for c in raw['tables'][cell['table']]['cells']
                      if (c['row'], c['column']) == (cell['row'], cell['column'])]
            if len(values) != 1:
                raise ValueError('Full observed numeric cell has ambiguous candidate slot')
            match = match_sign(image, cell['box'], values[0], fonts)
            if match is None:
                continue
            image.crop(tuple(round(v) for v in cell['box'])).save(
                output/(row['id']+'__t'+str(cell['table'])+'_r'+str(cell['row'])+'_c'+str(cell['column'])+'.png'))
            results.append({'id': row['id'], 'table': cell['table'], 'row': cell['row'], 'column': cell['column'],
                            'source_sha256': digest(path), 'current': values[0], 'box': cell['box'], **match})
    frozen_json(output/'result.json', {**recipe, 'complete_mapped_scope_sign_cells': len(results),
        'unique_raster_proposals': sum(row['proposal'] is not None for row in results), 'results': results})
    print({'sign_cells': len(results), 'unique_proposals': sum(row['proposal'] is not None for row in results),
           'matches': [{'id': row['id'], 'current': row['current'], 'proposal': row['proposal'],
                       'iou': row.get('best_iou'), 'margin': row.get('distinct_sign_iou_margin')} for row in results]})


if __name__ == '__main__':
    main()
