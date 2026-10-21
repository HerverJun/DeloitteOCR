"""Product glue around the pinned, already exercised official SAFIRE adapter.

Learned inference is shared read-only. All writable caches and artifacts belong
to the owned worker output; no network, download, or CPU fallback occurs here.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys


LIMITATIONS = [
    '区域响应和掩码质量均不是真实性或篡改概率；未进行独立正常页误报校准',
    '已曝光开发样例：中强痕迹定位7/16、其中强痕迹4/8、少量字符0/8；不代表真实业务检出率',
    '扫描、屏拍、中文合同及来源指纹能力尚未独立验证',
    '原尺寸分块属于产品适配，边界和上下文可能影响定位；未通过原定精度升级门槛',
]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _verified_assets(binding, digest):
    root = Path(binding['asset_root']).resolve()
    if root.drive.upper() != 'F:':
        raise ValueError('SAFIRE assets must remain on F:')
    for name, identity in binding['resources'].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('SAFIRE asset missing or outside its pinned root: ' + name)
        if path.stat().st_size != identity['bytes'] or digest(path) != identity['sha256']:
            raise ValueError('SAFIRE asset identity changed: ' + name)
    return root


def analyze(request, worker):
    import cv2
    import numpy as np
    from PIL import Image
    from image_geometry import image_geometry

    if request['options']['device'] != 'cuda':
        raise ValueError('SAFIRE currently requires qualified CUDA; explicit CPU requests are not substituted')
    source = Path(request['inputs'][0]['path'])
    with Image.open(source) as original:
        if original.format not in {'JPEG','PNG','WEBP','BMP','TIFF'} or getattr(original,'n_frames',1)!=1:
            raise ValueError('Only single-frame JPEG/PNG/WebP/BMP/TIFF are supported')
        if source.stat().st_size>32*1024**2 or original.width*original.height>12_000_000:
            raise ValueError('Input exceeds 32 MiB or 12 million pixels')
    binding = request['model_binding']
    root = _verified_assets(binding, worker.digest)
    worker.CONTROL.check()
    native = _load('clear_pinned_safire_native', root/'scripts/safire_native.py')

    def configure():
        # worker_environment already assigns every writable location on F:.
        os.environ.update(MPLBACKEND='Agg', MONAI_DATA_DIRECTORY=os.environ['XDG_CACHE_HOME'],
                          TORCHDYNAMO_DISABLE='1', CUDA_MODULE_LOADING='LAZY')
        sys.dont_write_bytecode = True
        for path in (root/'dependencies', native.UPSTREAM_ROOT):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

    def publish_load(_path, value):
        # The upstream host adapter's evidence output is redirected per request.
        worker.save('safire-load.json', value)

    native.configure_environment = configure
    native.write_json = publish_load
    configure()
    metrics = _load('clear_pinned_safire_metrics', root/'scripts/safire_metrics.py')
    worker.progress('加载 SAFIRE 官方权重')
    engine = native.NativeSafire(points_per_batch=request['runner'].get('safire_points_per_batch', 64))
    try:
        worker.CONTROL.check()
        def progress(tile):
            worker.CONTROL.check()
            worker.progress(f"SAFIRE 区域分析 {tile['tile_index']+1}/{tile['tile_count']}",
                            tiles_completed=tile['tile_index']+1, tiles_total=tile['tile_count'])
        source = request['inputs'][0]['path']
        heatmap, geometry, tiles = engine.infer(source, progress=progress)
        worker.CONTROL.check()
        rgb, repeated = engine.decode(source)
        if repeated['input_sha256'] != request['inputs'][0]['blob_hash']:
            raise ValueError('Original image hash changed during SAFIRE inference')
        height, width = heatmap.shape
        native.reserve_guard(width*height*12 + 65536)
        # Proposals use the real float32 response. Product persistence keeps a
        # compact response image; the 32 research float maps remain untouched.
        worker.png('heatmap-response.png',np.rint(heatmap*255).astype(np.uint8))
        colored = cv2.applyColorMap((heatmap*255).astype(np.uint8), cv2.COLORMAP_JET)
        preview = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        for name, array in (('preview.png',preview), ('preview-display.png',preview),
                            ('heatmap.png',colored), ('heatmap-display.png',colored)):
            worker.png(name, array)
        prediction, regions, page_score = metrics.extract_regions(heatmap, .9)
        worker.png('candidate-mask.png', prediction.astype(np.uint8)*255)
        regions = [{**row, 'pixel_box':row['box'],
                    'box':[row['box'][0]/width,row['box'][1]/height,row['box'][2]/width,row['box'][3]/height]}
                   for row in regions]
        triggered = any(row['score'] > 0 for row in regions)
        reason = '按候选区域正超阈值响应建议人工复核；未进行误报校准'
        assessment = {'state':'review_recommended' if triggered else 'no_significant_signal',
            'label':'建议复核' if triggered else '未发现显著线索', 'reason':reason,
            'score_is_probability':False, 'calibration':False, 'calibration_id':None,
            'operational_trigger':'any_candidate_region', 'threshold':0.0, 'page_score':page_score}
        # Persisted boxes and both previews share the same oriented-pixel basis.
        display_geometry = image_geometry(width, height, 1)
        display_geometry.update(source_coordinate_system='exif_transposed_pixels',
            persisted_region_coordinate_system='exif_transposed_pixels', encoded_geometry=geometry)
        model_hashes = {name:row['sha256'] for name,row in engine.metadata['models'].items()}
        recipe = {**metrics.RECIPE, 'pixel_threshold':.9, 'operational_trigger':'any_candidate_region',
                  'page_trigger_threshold':0.0, 'calibration':None}
        result = {'algorithm':'SAFIRE (official)', 'engine':'SAFIRE', 'device':'cuda', 'outcome':'analyzed',
            'width':width,'height':height,'source_sha256':geometry['input_sha256'], 'model_hashes':model_hashes,
            'detector':{**engine.metadata, 'version':'clear-safire-product-v1', 'recipe':recipe, 'calibration':False,
                        'business_accuracy_qualified':False, 'deployment_basis':'explicit_user_approval'},
            'candidate_regions':regions,'assessment':assessment,
            'deep_analysis':{'status':'completed','engine':'SAFIRE','source_format':geometry['input_format']},
            'metrics':{'map_min':float(heatmap.min()),'map_max':float(heatmap.max()),
                       'map_mean':float(heatmap.mean()),'candidate_count':len(regions),'page_score':page_score},
            'processing':{'jpeg_recompressed':False,'orientation':'exif_transposed_pixels',
                'tile_size':1024,'tile_stride':768,'geometry':display_geometry,'inference':tiles,
                'display_preview':'preview-display.png','display_heatmap':'heatmap-display.png',
                'heatmap_storage':{'file':'heatmap-response.png','format':'uint8_png',
                    'response_scale':1/255,'float32_inference':True,'proposals_before_quantization':True}},
            'performance':{'peak_memory_mib':engine.torch.cuda.max_memory_allocated()/1024**2,
                           'points_per_batch':engine.config['points_per_batch']},
            'limitations':list(LIMITATIONS)}
        return result
    finally:
        engine.close()


def classical(request, worker, result):
    """Keep native-format classical evidence separate from the neural response."""
    import shutil
    toolbox = Path(__file__).resolve().parents[1]/'image_forensics'
    manifest = json.loads((toolbox/'source-manifest.json').read_text('utf-8'))
    for row in manifest['derived']['files']:
        if worker.digest(toolbox/'forensic_toolbox'/row['name']) != row['sha256']:
            raise ValueError('Classical toolbox source identity changed')
    sys.path.insert(0, str(toolbox))
    from forensic_toolbox.core import run
    worker.CONTROL.check()
    worker.progress('检查原始格式经典线索')
    original = request['inputs'][0]['path']
    output = worker.OUT/'classic'
    evidence = run(original, output)
    for row in evidence['evidence']:
        if row.get('map_path'):
            old = row['map_path']
            row['map_path'] = 'classic-'+Path(old).name
            shutil.copyfile(output/old, worker.OUT/row['map_path'])
    result['classical'] = {'input_sha256':worker.digest(original),'verdict':None,
        'geometry':evidence.get('input',{}).get('geometry'),'evidence':evidence['evidence'],
        'toolbox_source_sha256':worker.digest(toolbox/'source-manifest.json')}
    worker.CONTROL.check()
    return result
