"""Image decoding and pinned-asset integrity; no CUDA or artificial accuracy claims."""
from pathlib import Path

import pytest
from PIL import Image

from modules.manual_multimodal.aigc_processing import decode_image,digest,verified_assets


@pytest.mark.parametrize('extension,format_name',[
    ('.jpg','JPEG'),('.png','PNG'),('.webp','WEBP'),('.bmp','BMP'),('.tiff','TIFF')])
def test_supported_single_frames_preserve_original_identity(tmp_path,extension,format_name):
    path=tmp_path/('image'+extension)
    Image.new('RGB',(37,23),(14,71,139)).save(path,format=format_name)
    identity=digest(path)
    image,metadata=decode_image(path)
    assert image.size==(37,23) and image.mode=='RGB'
    assert metadata['source_sha256']==identity==digest(path)
    assert metadata['source_format']==format_name


def test_exif_six_rotates_pixels_without_changing_source(tmp_path):
    path=tmp_path/'oriented.jpg'
    source=Image.new('RGB',(36,24),(255,0,0))
    for x in range(18,36):
        for y in range(24):source.putpixel((x,y),(0,0,255))
    exif=Image.Exif();exif[274]=6
    source.save(path,quality=100,subsampling=0,exif=exif)
    identity=digest(path)
    image,metadata=decode_image(path)
    assert image.size==(24,36)
    assert image.getpixel((12,5))[0]>240
    assert image.getpixel((12,30))[2]>240
    assert metadata['exif_orientation']==6 and metadata['source_sha256']==identity


def test_transparent_hidden_rgb_is_composited_white(tmp_path):
    path=tmp_path/'transparent.png'
    source=Image.new('RGBA',(2,1),(255,0,0,0))
    source.putpixel((1,0),(0,0,0,128))
    source.save(path)
    image,metadata=decode_image(path)
    assert image.getpixel((0,0))==(255,255,255)
    assert image.getpixel((1,0))==(127,127,127)
    assert metadata['alpha_handling']=='composite_over_white'


def test_palette_transparency_uses_white(tmp_path):
    path=tmp_path/'palette.png'
    source=Image.new('P',(2,1),0)
    source.putpalette([255,0,0]+[0,0,0]*255)
    source.save(path,transparency=0)
    image,_=decode_image(path)
    assert image.getpixel((0,0))==(255,255,255)


def test_multiframe_tiff_is_rejected(tmp_path):
    path=tmp_path/'multi.tiff'
    source=Image.new('RGB',(10,10))
    source.save(path,save_all=True,append_images=[Image.new('RGB',(10,10),(255,255,255))])
    with pytest.raises(ValueError,match='single-frame'):decode_image(path)


def test_corrupt_input_is_rejected_without_model_import(tmp_path):
    path=tmp_path/'corrupt.png';path.write_bytes(b'not an image')
    with pytest.raises(OSError):decode_image(path)


def test_byte_limit_precedes_decoder(tmp_path):
    path=tmp_path/'too-large.png'
    with path.open('wb') as stream:
        stream.seek(32*1024**2);stream.write(b'x')
    with pytest.raises(ValueError,match='32 MiB'):decode_image(path)


def test_pixel_limit_rejects_small_compressed_large_image(tmp_path):
    path=tmp_path/'pixel-limit.png'
    Image.new('RGB',(4001,3000)).save(path)
    with pytest.raises(ValueError,match='12 million'):decode_image(path)


def test_changed_model_and_path_traversal_refuse_to_load(tmp_path):
    path=tmp_path/'model.pth';path.write_bytes(b'pinned model bytes')
    binding={'asset_root':str(tmp_path),'resources':{'model.pth':{'bytes':path.stat().st_size,'sha256':digest(path)}}}
    verified_assets(binding)
    path.write_bytes(b'changed model bytes')
    with pytest.raises(ValueError,match='identity changed'):verified_assets(binding)
    binding['resources']={'../outside.pth':{'bytes':0,'sha256':'0'*64}}
    with pytest.raises(ValueError,match='outside'):verified_assets(binding)
