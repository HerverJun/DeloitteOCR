"""Source preview declarations must inherit exact native candidate file identities."""
from pathlib import Path

from scripts.development_native import runtime_source_record


def test_runtime_sources_use_only_sealed_modules_in_explicit_aliases(tmp_path):
    asset = tmp_path / 'program/bundles/ocr'
    pillow = {'sha256': 'a' * 64, 'bytes': 123}
    cuda = {'sha256': 'b' * 64, 'bytes': 456}
    sealed = {
        'bundles/ocr/runtimes/hunyuan/Lib/site-packages/PIL/image.pyd': pillow,
        'bundles/ocr/runtimes/ppocr/Lib/site-packages/paddle/cuda.dll': cuda,
        'bundles/ocr/runtimes/hunyuan/Lib/site-packages/PIL/helper.py': pillow,
        'bundles/ocr/runtimes/hunyuan/LibExtra/injected.dll': pillow,
        'bundles/classification/runtimes/hunyuan/Lib/site-packages/PIL/image.pyd': cuda,
        'bundles/ocr/models/model.dll': cuda,
    }
    record = runtime_source_record(asset, ['runtimes/hunyuan/Lib', 'runtimes/ppocr/Lib'], sealed, 'c' * 64)
    assert Path(record['source_bundle']) == asset
    assert record['candidate_sha256'] == 'c' * 64
    assert record['sources'] == [
        {'path': 'runtimes/hunyuan/Lib', 'files': {'site-packages/PIL/image.pyd': pillow}},
        {'path': 'runtimes/ppocr/Lib', 'files': {'site-packages/paddle/cuda.dll': cuda}},
    ]
    assert sealed['bundles/ocr/runtimes/hunyuan/Lib/site-packages/PIL/image.pyd'] is pillow
