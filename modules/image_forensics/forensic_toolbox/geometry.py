"""Exact EXIF pixel permutations and their pixel-edge coordinate contract."""
from __future__ import annotations

from typing import Any

import numpy as np

COORDINATE_VERSION = "exif-pixel-edges-v1"


def orientation_info(raw: Any) -> tuple[int, str]:
    if raw is None:
        return 1, "absent"
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 8:
        return raw, "valid"
    return 1, "invalid"


def transpose_pixels(pixels: np.ndarray, orientation: Any) -> np.ndarray:
    """Return the display pixels without resampling or changing any pixel value."""
    value, _ = orientation_info(orientation)
    if value == 2:
        result = pixels[:, ::-1]
    elif value == 3:
        result = pixels[::-1, ::-1]
    elif value == 4:
        result = pixels[::-1]
    elif value == 5:
        result = np.swapaxes(pixels, 0, 1)
    elif value == 6:
        result = np.rot90(pixels, 3)
    elif value == 7:
        result = np.swapaxes(pixels[::-1, ::-1], 0, 1)
    elif value == 8:
        result = np.rot90(pixels)
    else:
        result = pixels
    return np.ascontiguousarray(result)


def describe_geometry(stored_size: tuple[int, int], raw_orientation: Any) -> dict[str, Any]:
    """Coordinates describe pixel edges, so the outer corner is (W, H)."""
    width, height = stored_size
    if width < 1 or height < 1:
        raise ValueError("stored image dimensions must be positive")
    orientation, status = orientation_info(raw_orientation)
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
    forward = matrices[orientation]
    a, b, tx = forward[0]
    c, d, ty = forward[1]
    inverse = [[a, c, -a * tx - c * ty],
               [b, d, -b * tx - d * ty], [0, 0, 1]]
    return {
        "schema_version": "1.0",
        "coordinate_version": COORDINATE_VERSION,
        "source_coordinate_system": "stored_pixels",
        "display_coordinate_system": "exif_transposed_pixels",
        "map_coordinate_system": "exif_transposed_pixels",
        "stored_size": [width, height],
        "display_size": [height, width] if orientation >= 5 else [width, height],
        "source_exif_orientation": (raw_orientation if isinstance(raw_orientation, int)
                                    and not isinstance(raw_orientation, bool) else None),
        "exif_orientation": orientation,
        "orientation_status": status,
        "coordinate_convention": "pixel_edges",
        "stored_to_display": forward,
        "display_to_stored": inverse,
        "persisted_region_coordinate_system": "exif_transposed_pixels",
    }
