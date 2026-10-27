"""Bounded local worker. Optional OCR/model downloads are intentionally excluded."""
import json
import sys
import warnings
from pathlib import Path

# Embedded/isolated runtimes can carry an older toolbox in their ._pth file.
# Bind this worker to the code shipped beside its own script before importing.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image
import forensic_toolbox
from forensic_toolbox.core import run
from forensic_toolbox.review import render


def main():
    source, output, tools, name = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.runtime import require_path
    source = require_path(source, 'Forensics input', role='read')
    output = require_path(output, 'Forensics output', role='write')
    if set(tools) - {'metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move'}:
        raise ValueError('Tool is not admitted')
    Image.MAX_IMAGE_PIXELS = 12_000_000
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    with Image.open(source) as image:
        if image.format not in {'JPEG', 'PNG', 'WEBP', 'BMP', 'TIFF'} or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Only single-frame JPEG/PNG/WebP/BMP/TIFF images are supported')
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError('Image exceeds 12 million pixels')
    report = run(source, output, tools=tools)
    report['input']['path'] = name
    report['runtime'] = {'python': sys.version.split()[0], 'toolbox': {
        'path': str(Path(forensic_toolbox.__file__).resolve().parent),
        'version': forensic_toolbox.__version__}}
    for module_name in ('numpy', 'cv2', 'PIL'):
        module = __import__(module_name)
        report['runtime'][module_name] = module.__version__
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    render(report, output / 'review.html')


if __name__ == '__main__':
    main()
