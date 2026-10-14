"""Pure 8-aligned CAT tile planning with a bounded input and center cropping."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    # Bounds are continuous pixel edges in left, top, right, bottom order.
    input_box: tuple[int, int, int, int]
    output_box: tuple[int, int, int, int]

    @property
    def crop_box(self):
        left, top, _, _ = self.input_box
        x1, y1, x2, y2 = self.output_box
        return (x1-left, y1-top, x2-left, y2-top)


@dataclass(frozen=True)
class TilePlan:
    tiles: tuple[Tile, ...]
    metadata: dict


def plan_tiles(width, height, padded_width, padded_height, *, tile_size=1024, halo=0, full_image=False):
    for value in (width, height, padded_width, padded_height, tile_size, halo):
        if type(value) is not int:
            raise ValueError('CAT dimensions and halo must be integers')
    if min(width, height) <= 0 or padded_width < width or padded_height < height:
        raise ValueError('Invalid image or JPEG padded dimensions')
    if padded_width % 8 or padded_height % 8:
        raise ValueError('JPEG coefficients must use an 8-pixel grid')
    if tile_size not in (512, 768, 1024):
        raise ValueError('CAT-Net tile size must be 512, 768 or 1024')
    if halo < 0 or halo % 8 or 2*halo >= tile_size:
        raise ValueError('CAT-Net halo must be nonnegative, 8-aligned, and smaller than half the input limit')
    if type(full_image) is not bool:
        raise ValueError('full_image must be boolean')
    stride = tile_size-2*halo
    whole = full_image or (padded_width <= tile_size and padded_height <= tile_size)
    if whole:
        tiles = (Tile((0, 0, padded_width, padded_height), (0, 0, width, height)),)
        rows, columns = 1, 1
    else:
        rows, columns = (height+stride-1)//stride, (width+stride-1)//stride
        tiles = tuple(Tile(
            (max(0, left-halo), max(0, top-halo),
             min(padded_width, left+stride+halo), min(padded_height, top+stride+halo)),
            (left, top, min(width, left+stride), min(height, top+stride)))
            for top in range(0, height, stride) for left in range(0, width, stride))
    metadata = {
        'schema_version': '1.0', 'strategy_version': 'catnet-center-crop-v1',
        'mode': 'full_image_reference' if full_image else 'whole_image' if whole else 'tiled',
        'tile_input_limit': tile_size, 'input_upper_bound_applied': not full_image,
        'halo': halo, 'core_stride': stride, 'grid_shape': [rows, columns],
        'tile_count': len(tiles), 'stitching': 'center_crop',
        'jpeg_grid_alignment': 8, 'coordinate_system': 'stored_pixels',
        'stored_size': [width, height], 'jpeg_padded_size': [padded_width, padded_height],
        'outer_padding': 'original JPEG DCT extent; RGB 127.5 only outside stored pixels',
        'tiles': [{'input_box': list(tile.input_box), 'output_box': list(tile.output_box),
                   'crop_box': list(tile.crop_box)} for tile in tiles],
    }
    return TilePlan(tiles, metadata)
