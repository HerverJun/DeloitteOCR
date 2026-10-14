"""Coordinate persistence and exact image permutations; no models or CUDA."""
import math
import pytest
from modules.manual_multimodal.image_geometry import image_geometry, transform_point, transform_box, orient_array


@pytest.mark.parametrize('orientation,expected', [
    (1, [20, 15, 60, 55]), (2, [60, 15, 100, 55]),
    (3, [60, 25, 100, 65]), (4, [20, 25, 60, 65]),
    (5, [15, 20, 55, 60]), (6, [25, 20, 65, 60]),
    (7, [25, 60, 65, 100]), (8, [15, 60, 55, 100]),
])
def test_non_square_rectangle_has_expected_orientation_and_roundtrip(orientation, expected):
    geometry = image_geometry(120, 80, orientation)
    assert transform_box([20, 15, 60, 55], geometry) == expected
    assert transform_box(expected, geometry, inverse=True) == [20, 15, 60, 55]
    assert geometry['stored_size'] == [120, 80]
    assert geometry['display_size'] == ([120, 80] if orientation <= 4 else [80, 120])
    # Persisted region coordinates and source identity do not change with display.
    assert geometry['persisted_region_coordinate_system'] == 'stored_pixels'
    assert geometry['coordinate_version'] == 'exif-pixel-edges-v1'


@pytest.mark.parametrize('orientation', range(1, 9))
@pytest.mark.parametrize('box', [[0,0,1,1], [0,.1,.01,.9], [.91,.92,1,1], [.1,.2,.4,.8]])
def test_normalized_edges_and_small_regions_roundtrip(orientation, box):
    geometry = image_geometry(123, 81, orientation)
    displayed = transform_box(box, geometry, normalized=True)
    assert min(displayed) >= -1e-15 and max(displayed) <= 1+1e-15
    assert transform_box(displayed, geometry, normalized=True, inverse=True) == pytest.approx(box, abs=1e-14)


@pytest.mark.parametrize('orientation', range(1, 9))
def test_all_continuous_image_edges_map_inside_display(orientation):
    geometry = image_geometry(123, 81, orientation)
    dw, dh = geometry['display_size']
    mapped = [transform_point(point, geometry['stored_to_display']) for point in [(0,0),(123,0),(0,81),(123,81)]]
    assert sorted(map(tuple,mapped)) == sorted([(0,0),(dw,0),(0,dh),(dw,dh)])
    for before, after in zip([(0,0),(123,0),(0,81),(123,81)], mapped):
        assert transform_point(after, geometry['display_to_stored']) == list(before)


@pytest.mark.parametrize('orientation,status,raw', [(None,'absent',None),(0,'invalid',0),(9,'invalid',9),('6','invalid',None),(True,'invalid',None)])
def test_missing_or_invalid_exif_uses_recorded_identity(orientation, status, raw):
    geometry = image_geometry(123, 81, orientation)
    assert geometry['exif_orientation'] == 1
    assert geometry['orientation_status'] == status
    assert geometry['source_exif_orientation'] == raw
    assert transform_box([1,2,3,4],geometry) == [1,2,3,4]


@pytest.mark.parametrize('orientation,expected', [
    (1, [[0,1,2],[3,4,5]]), (2, [[2,1,0],[5,4,3]]),
    (3, [[5,4,3],[2,1,0]]), (4, [[3,4,5],[0,1,2]]),
    (5, [[0,3],[1,4],[2,5]]), (6, [[3,0],[4,1],[5,2]]),
    (7, [[5,2],[4,1],[3,0]]), (8, [[2,5],[1,4],[0,3]]),
])
def test_array_permutation_matches_pixel_centers_and_keeps_values_exact(orientation, expected):
    import numpy as np
    source = np.arange(6,dtype=np.float32).reshape(2,3)
    actual = orient_array(source,orientation)
    assert actual.tolist() == expected
    geometry = image_geometry(3,2,orientation)
    # The continuous-edge transform must locate every original pixel center.
    for y in range(2):
        for x in range(3):
            dx,dy=transform_point([x+.5,y+.5],geometry['stored_to_display'])
            assert actual[math.floor(dy),math.floor(dx)] == source[y,x]
    rgb=np.stack([source,source+10,source+20],axis=2)
    assert orient_array(rgb,orientation).shape == (*actual.shape,3)
    assert orient_array(rgb,orientation)[:,:,1].tolist() == (actual+10).tolist()


@pytest.mark.parametrize('box', [[0,0,0,1],[0,2,1,1],[0,0,float('nan'),1],[0,0,1,float('inf')]])
def test_invalid_rectangles_are_rejected(box):
    with pytest.raises(ValueError):transform_box(box,image_geometry(120,80,6))
