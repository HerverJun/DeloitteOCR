"""EXIF display geometry; persisted CAT regions stay in stored pixel coordinates.

Matrices transform continuous pixel edges, whose extents are [0, width] and
[0, height]. They must not be mixed with discrete pixel-center coordinates.
The image operation itself is an exact permutation: no interpolation occurs.
"""
from __future__ import annotations
import math

COORDINATE_VERSION = 'exif-pixel-edges-v1'


def image_geometry(width, height, orientation=None):
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('Image dimensions must be positive integers')
    original = orientation if type(orientation) is int else None
    valid = type(orientation) is int and 1 <= orientation <= 8
    effective = orientation if valid else 1
    status = 'valid' if valid else 'absent' if orientation is None else 'invalid'
    matrices = {
        1: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        2: [[-1, 0, width], [0, 1, 0], [0, 0, 1]],
        3: [[-1, 0, width], [0, -1, height], [0, 0, 1]],
        4: [[1, 0, 0], [0, -1, height], [0, 0, 1]],
        5: [[0, 1, 0], [1, 0, 0], [0, 0, 1]],
        6: [[0, -1, height], [1, 0, 0], [0, 0, 1]],
        7: [[0, -1, height], [-1, 0, width], [0, 0, 1]],
        8: [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
    }
    forward = matrices[effective]
    a, b, tx = forward[0]
    c, d, ty = forward[1]
    determinant = a*d-b*c
    ia, ib, ic, id_ = d//determinant, -b//determinant, -c//determinant, a//determinant
    inverse = [[ia, ib, -(ia*tx+ib*ty)], [ic, id_, -(ic*tx+id_*ty)], [0, 0, 1]]
    return {
        'schema_version': '1.0', 'coordinate_version': COORDINATE_VERSION,
        'source_coordinate_system': 'stored_pixels',
        'display_coordinate_system': 'exif_transposed_pixels',
        'persisted_region_coordinate_system': 'stored_pixels',
        'coordinate_convention': 'pixel_edges',
        'stored_size': [width, height],
        'display_size': [width, height] if effective <= 4 else [height, width],
        'source_exif_orientation': original, 'exif_orientation': effective,
        'orientation_status': status,
        'stored_to_display': forward, 'display_to_stored': inverse,
    }


def transform_point(point, matrix):
    x, y = point
    if not all(math.isfinite(value) for value in (x, y)):
        raise ValueError('Coordinates must be finite')
    return [matrix[0][0]*x + matrix[0][1]*y + matrix[0][2],
            matrix[1][0]*x + matrix[1][1]*y + matrix[1][2]]


def transform_box(box, geometry, *, inverse=False, normalized=False):
    """Map all four rectangle corners, preserving the enclosing rectangle."""
    x1, y1, x2, y2 = box
    if not all(math.isfinite(value) for value in box) or x1 >= x2 or y1 >= y2:
        raise ValueError('A finite, nonempty rectangle is required')
    source_size = geometry['display_size'] if inverse else geometry['stored_size']
    target_size = geometry['stored_size'] if inverse else geometry['display_size']
    if normalized:
        x1, x2 = x1*source_size[0], x2*source_size[0]
        y1, y2 = y1*source_size[1], y2*source_size[1]
    matrix = geometry['display_to_stored'] if inverse else geometry['stored_to_display']
    corners = [transform_point(point, matrix) for point in ((x1,y1),(x1,y2),(x2,y1),(x2,y2))]
    mapped = [min(point[0] for point in corners), min(point[1] for point in corners),
              max(point[0] for point in corners), max(point[1] for point in corners)]
    if normalized:
        mapped = [mapped[0]/target_size[0], mapped[1]/target_size[1],
                  mapped[2]/target_size[0], mapped[3]/target_size[1]]
    return mapped


def orient_array(array, orientation):
    """Exact EXIF permutation of a numpy-compatible HxW[xC] array."""
    if type(orientation) is not int or orientation not in range(1, 9):
        orientation = 1
    if orientation == 1: return array
    if orientation == 2: return array[:, ::-1]
    if orientation == 3: return array[::-1, ::-1]
    if orientation == 4: return array[::-1]
    transposed = array.swapaxes(0, 1)
    if orientation == 5: return transposed
    if orientation == 6: return transposed[:, ::-1]
    if orientation == 7: return transposed[::-1, ::-1]
    return transposed[::-1]
