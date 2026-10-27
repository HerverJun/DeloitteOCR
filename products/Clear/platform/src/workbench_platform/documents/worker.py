"""Fixed CPU parser entry. Never invoke PDF classification, OCR or downloads."""
from dataclasses import asdict
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def main():
    source, output, suffix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    package = os.environ.get('CLEAR_RUNTIME_PACKAGE_ROOT')
    if package:
        sys.path.insert(0, package)
    from packages.runtime import require_path
    source = require_path(source, 'Document input', role='read')
    output = require_path(output, 'Document output', role='write')
    # Native parsing must be offline even when an upstream optional helper is
    # accidentally reached. No access to document URLs or model endpoints.
    def offline(*args, **kwargs):
        raise RuntimeError('Document CPU worker is offline; external access disabled')
    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    os.environ.update(CUDA_VISIBLE_DEVICES='', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    if importlib.metadata.version('docvortex') != '0.5.16':
        raise RuntimeError('docvortex 0.5.16 required')
    from docvortex.api import analyze, postprocess
    analysis = analyze(source, file_suffix=suffix, **({'parse_mode': 'txt'} if suffix == 'pdf' else {}))
    if suffix == 'pdf':
        # The fixed embedded runtime deliberately omits script directories.
        spec = importlib.util.spec_from_file_location('clear_document_pdf_coverage', Path(__file__).with_name('pdf_coverage.py'))
        coverage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(coverage)
        write(output / 'native-coverage.json', coverage.inspect_pdf(source))
    write(output / 'raw-model.json', analysis.model_json.model_dump(mode='json', by_alias=True))
    middle_pages = []
    metadata = None
    # Upstream postprocessing merges adjacent page tables. Parse each page in
    # isolation so Clear's page sources and separate tables remain intact.
    for index, raw_page in zip(analysis.model_json.resolved_page_indices, analysis.model_json.pages, strict=True):
        single = analysis.model_json.model_copy(deep=True, update={'pages': [raw_page], 'page_index_map': [index]})
        result = postprocess(single, assets=analysis.assets, keep_model_json=True)
        result.save_bundle(output / f'page-{index + 1}.bundle')
        data = result.to_dict()
        metadata = data.get('metadata')
        middle_pages.extend(data['pages'])
    write(output / 'raw-middle.json', {'schema': 'docvortex.middle', 'pages': middle_pages, 'metadata': metadata,
                                      'extensions': analysis.model_json.extensions})
    write(output / 'diagnostics.json', [asdict(value) for value in analysis.diagnostics])


if __name__ == '__main__':
    main()
