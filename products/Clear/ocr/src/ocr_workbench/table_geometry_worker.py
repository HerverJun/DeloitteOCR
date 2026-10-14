"""Auditable adapter around PaddleX 3.7.0 table_recognition_v2.

PaddleX pipeline and matching implementations are imported unchanged (Apache-2.0).
We capture predictions before its merge/supplement/sort postprocessing. This is
an auxiliary geometry component and never returns a replacement text result.
"""

from copy import deepcopy
import json
from pathlib import Path
import time
from unittest.mock import patch

from ocr_workbench.coordinates import bounds
from ocr_workbench.worker import json_safe


MODEL_MODULES = {
    'TableClassification': ('table_classification', 'PP-LCNet_x1_0_table_cls'),
    'WiredTableStructureRecognition': ('table_structure_recognition', 'SLANeXt_wired'),
    'WirelessTableStructureRecognition': ('table_structure_recognition', 'SLANeXt_wireless'),
    'WiredTableCellsDetection': ('table_cells_detection', 'RT-DETR-L_wired_table_cell_det'),
    'WirelessTableCellsDetection': ('table_cells_detection', 'RT-DETR-L_wireless_table_cell_det'),
}


def serializable(value):
    return json.loads(json.dumps(value, default=json_safe))


class GeometryContextSession:
    """Shared page detection/OCR, loaded separately from candidate models."""
    def __init__(self, models):
        self.models = Path(models)
        self.layout = None
        self.loaded = 0

    def predict(self, image_path, request):
        import numpy as np
        from PIL import Image
        started = time.perf_counter()
        regions = request.get('regions', [])
        if not regions:
            from paddlex import create_model
            if self.layout is None:
                self.layout = create_model('PP-DocLayoutV3', model_dir=str(self.models / 'PP-DocLayoutV3'), device='gpu:0')
            with Image.open(image_path) as im:
                pixels = np.ascontiguousarray(np.asarray(im.convert('RGB'))[:, :, ::-1])
            layout = next(iter(self.layout(pixels)))
            regions = [{'box': b['coordinate'], 'source': 'PP-DocLayoutV3'}
                       for b in layout['boxes'] if b['label'] == 'table']
        blocks, source = request.get('ocr_blocks', []), request.get('ocr_source')
        if not blocks:
            if request.get('allow_auxiliary_ocr') is False:
                raise ValueError('冻结 OCR 输入没有文字框；禁止隐式补识别')
            from ocr_workbench.worker import load_paddle, paddle_engine
            ocr, seconds = load_paddle('ppocr', self.models)
            _, blocks, _ = paddle_engine('ppocr', self.models, image_path, ocr, seconds)
            del ocr
            source = 'PP-OCRv6-auxiliary'
        return {'status': 'success', 'component': 'shared-geometry-context', 'regions': regions,
                'ocr_blocks': blocks, 'ocr_source': source, 'image_version': request.get('image_version'),
                'contributes_to_votes': False, 'inference_seconds': time.perf_counter() - started}

    def write(self, image, output, request):
        from ocr_workbench.atomic_files import write_json
        write_json(Path(output) / 'geometry.json', serializable(self.predict(image, request)), durable=True)


class GeometrySession:
    def __init__(self, models):
        import paddle
        if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
            raise RuntimeError('表格定位需要 CUDA GPU')
        from paddlex.inference.pipelines.table_recognition.pipeline_v2 import _TableRecognitionPipelineV2
        self.models = Path(models)
        for _, name in MODEL_MODULES.values():
            if not (self.models / name / 'inference.pdiparams').is_file():
                raise ValueError('缺少本地表格模型：' + name)
        self.revisions = {name: json.loads((self.models / name / 'source-manifest.json').read_text('utf-8'))['revision']
                          for _, name in MODEL_MODULES.values()}
        config = {'pipeline_name': 'table_recognition_v2', 'use_doc_preprocessor': False,
                  'use_layout_detection': False, 'use_ocr_model': False,
                  'SubModules': {key: {'module_name': module, 'model_name': name,
                                       'model_dir': str(self.models / name)}
                                 for key, (module, name) in MODEL_MODULES.items()}}
        started = time.perf_counter()
        self.pipeline = _TableRecognitionPipelineV2(config, device='gpu:0')
        self.pipeline.cells_split_ocr = True
        self.loaded = time.perf_counter()-started
        self.layout = None

    def predict(self, image_path, request):
        import numpy as np
        from PIL import Image
        from paddlex.inference.pipelines.table_recognition import table_recognition_post_processing_v2 as post
        started = time.perf_counter()
        with Image.open(image_path) as image:
            pixels = np.ascontiguousarray(np.asarray(image.convert('RGB'))[:, :, ::-1])
        regions = request.get('regions', [])
        if not regions:
            from paddlex import create_model
            if self.layout is None:
                if not (self.models / 'PP-DocLayoutV3/inference.pdiparams').is_file():
                    raise ValueError('缺少 PP-DocLayoutV3，请框选表格区域或补全离线模型')
                self.layout = create_model('PP-DocLayoutV3', model_dir=str(self.models / 'PP-DocLayoutV3'), device='gpu:0')
            layout = next(iter(self.layout(pixels)))
            regions = [{'box': b['coordinate'], 'source': 'PP-DocLayoutV3'} for b in layout['boxes'] if b['label'] == 'table']
        blocks = [b for b in request.get('ocr_blocks', []) if b.get('polygon') and b.get('text', '').strip()]
        ocr_source = request.get('ocr_source', 'external')
        if not blocks:
            if request.get('allow_auxiliary_ocr') is False:
                raise ValueError('冻结 OCR 输入没有文字框；禁止隐式补识别')
            # This is only needed when this version has no PP-OCR/native word
            # evidence. It is recorded as auxiliary and is never counted as a vote.
            from ocr_workbench.worker import load_paddle, paddle_engine
            ocr, seconds = load_paddle('ppocr', self.models)
            _, blocks, _ = paddle_engine('ppocr', self.models, image_path, ocr, seconds)
            del ocr
            ocr_source = 'PP-OCRv6-auxiliary'
        overall = {'rec_texts': [b['text'] for b in blocks],
                   'rec_polys': np.asarray([b['polygon'] for b in blocks], dtype=np.float32),
                   'rec_boxes': np.asarray([bounds(b['polygon']) for b in blocks], dtype=np.float32).reshape((-1, 4)),
                   'rec_scores': np.asarray([b.get('confidence') or 0 for b in blocks], dtype=np.float32),
                   'doc_preprocessor_res': {'output_img': pixels}}
        output = []
        for region in regions:
            box = region.get('box') or bounds(region['polygon'])
            x0, y0, x1, y1 = [int(round(v)) for v in box]
            if not (0 <= x0 < x1 <= pixels.shape[1] and 0 <= y0 < y1 <= pixels.shape[0]):
                raise ValueError('表格区域超出当前图像版本')
            box = [x0, y0, x1, y1]
            evidence = {'region_id': region.get('id'), 'table_box': box, 'raw': {}, 'steps': [], 'matching': []}
            extract = self.pipeline.extract_results
            reprocess = self.pipeline.cells_det_results_reprocessing
            match = post.match_table_and_ocr

            def capture_extract(prediction, task):
                evidence['raw'][task] = serializable({k: v for k, v in prediction.items()
                    if k not in ('input_img', 'image', 'output_img')})
                return extract(prediction, task)

            def capture_reprocess(*args, **kwargs):
                before = serializable(args)
                result = reprocess(*args, **kwargs)
                evidence['steps'].append({'operation': 'cells_det_results_reprocessing', 'inputs': before,
                                          'output': serializable(result)})
                return result

            def capture_match(*args, **kwargs):
                result = match(*args, **kwargs)
                evidence['matching'].append({'inputs': serializable(args), 'output': serializable(result)})
                return result

            with patch.object(self.pipeline, 'extract_results', capture_extract), \
                 patch.object(self.pipeline, 'cells_det_results_reprocessing', capture_reprocess), \
                 patch.object(post, 'match_table_and_ocr', capture_match):
                result = self.pipeline.predict_single_table_recognition_res(
                    pixels[y0:y1, x0:x1], deepcopy(overall), box,
                    use_ocr_results_with_table_cells=False, flag_find_nei_text=False)
            evidence['final'] = serializable(dict(result))
            from ocr_workbench.geometry_providers import paddle_lineage
            evidence['lineage'] = paddle_lineage(evidence)
            evidence['coordinate_contract'] = {'version': 2, 'raw_detector': 'crop-xyxy',
                'raw_structure': 'crop-quad8', 'final': 'image-xyxy', 'interval_end': 'exclusive'}
            output.append(evidence)
        return {'status': 'success', 'component': 'paddle-table-v2', 'experimental': True,
                'paddlex_version': '3.7.0', 'model_revisions': self.revisions,
                'tables': output, 'ocr_source': ocr_source, 'contributes_to_votes': False,
                'image_version': request.get('image_version'), 'contract_version': 2,
                'ocr_blocks': blocks, 'load_seconds': self.loaded, 'inference_seconds': time.perf_counter()-started}

    def write(self, image, output, request):
        from ocr_workbench.atomic_files import write_json
        write_json(Path(output) / 'geometry.json', self.predict(image, request), durable=True)
