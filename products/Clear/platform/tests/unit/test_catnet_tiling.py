"""CAT coverage, grid and context guarantees; no Torch, model or CUDA imports."""
import pytest
from modules.manual_multimodal.catnet_tiling import plan_tiles
from modules.manual_multimodal.catnet_inference import CatNetSession


def padded(value):return (value+7)//8*8


@pytest.mark.parametrize('size', [(1,1),(7,9),(511,513),(1024,1024),(1025,1025),(1200,896),(4097,3011)])
@pytest.mark.parametrize('tile_size,halo', [(1024,0),(1024,64),(512,0),(512,64)])
def test_tiles_cover_exactly_once_and_keep_both_streams_on_jpeg_grid(size,tile_size,halo):
    width,height=size
    plan=plan_tiles(width,height,padded(width),padded(height),tile_size=tile_size,halo=halo)
    area=0
    for tile in plan.tiles:
        left,top,right,bottom=tile.input_box
        x1,y1,x2,y2=tile.output_box
        cx1,cy1,cx2,cy2=tile.crop_box
        assert all(edge % 8 == 0 for edge in tile.input_box)
        assert right-left<=tile_size and bottom-top<=tile_size
        assert 0<=left<=x1<x2<=right<=padded(width)
        assert 0<=top<=y1<y2<=bottom<=padded(height)
        assert (cx2-cx1,cy2-cy1)==(x2-x1,y2-y1)
        assert (left+cx1,top+cy1,left+cx2,top+cy2)==tile.output_box
        area+=(x2-x1)*(y2-y1)
    assert area==width*height
    for index,tile in enumerate(plan.tiles):
        x1,y1,x2,y2=tile.output_box
        for other in plan.tiles[index+1:]:
            ox1,oy1,ox2,oy2=other.output_box
            assert min(x2,ox2)<=max(x1,ox1) or min(y2,oy2)<=max(y1,oy1)
    assert plan.metadata['tile_count']==len(plan.tiles)


@pytest.mark.parametrize('tile_size,expected_stride', [(1024,896),(512,384)])
def test_interior_center_has_context_without_exceeding_old_input_limit(tile_size,expected_stride):
    plan=plan_tiles(3001,2003,padded(3001),padded(2003),tile_size=tile_size,halo=64)
    assert plan.metadata['core_stride']==expected_stride
    for tile in plan.tiles:
        left,top,right,bottom=tile.input_box
        x1,y1,x2,y2=tile.output_box
        if x1>0:assert x1-left==64
        if y1>0:assert y1-top==64
        if x2<3001:assert right-x2==64
        if y2<2003:assert bottom-y2==64


def test_zero_halo_preserves_legacy_input_and_write_bounds():
    width,height=1200,896
    plan=plan_tiles(width,height,padded(width),padded(height),tile_size=1024,halo=0)
    assert [tile.input_box for tile in plan.tiles]==[(0,0,1024,896),(1024,0,1200,896)]
    assert [tile.output_box for tile in plan.tiles]==[(0,0,1024,896),(1024,0,1200,896)]


def test_small_image_is_single_whole_image_not_redundant_center_tiles():
    plan=plan_tiles(1001,1003,1008,1008,halo=64)
    assert len(plan.tiles)==1
    assert plan.tiles[0].input_box==(0,0,1008,1008)
    assert plan.tiles[0].output_box==(0,0,1001,1003)
    assert plan.metadata['mode']=='whole_image'


def test_explicit_full_image_is_reference_override_and_records_it():
    plan=plan_tiles(1200,896,1200,896,halo=64,full_image=True)
    assert len(plan.tiles)==1 and plan.tiles[0].input_box==(0,0,1200,896)
    assert plan.metadata['mode']=='full_image_reference'
    assert plan.metadata['input_upper_bound_applied'] is False


@pytest.mark.parametrize('kwargs', [{'halo':1},{'halo':-8},{'halo':512},{'tile_size':900},{'halo':True},{'full_image':1}])
def test_invalid_grid_parameters_fail_before_any_inference(kwargs):
    with pytest.raises(ValueError):plan_tiles(1200,896,1200,896,**kwargs)


def test_geometry_planning_and_session_import_need_no_heavy_runtime():
    # CatNetSession imports Torch only when constructing a real session; the
    # platform runtime does not include Torch and can still import this module.
    assert callable(CatNetSession.infer)
    with pytest.raises(ValueError):plan_tiles(100,100,101,104)
