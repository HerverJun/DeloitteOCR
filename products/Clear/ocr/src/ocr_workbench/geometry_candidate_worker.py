"""Offline raw geometry providers. The only text input is the shared OCR snapshot."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

from ocr_workbench.coordinates import bounds, box_polygon
from ocr_workbench.geometry_contract import checked_polygon, fingerprint, intersection_area, signed_area
from ocr_workbench.geometry_provider_config import ROOT, verify_provider, file_sha


def region_inputs(image, request):
    """Yield crop pixels and whole, exclusively owned OCR tokens in crop space."""
    regions = request.get('regions', [])
    width, height = image.size
    boxes = []
    for region in regions:
        box = [int(round(v)) for v in (region.get('box') or bounds(region['polygon']))]
        checked_polygon(box_polygon(box), width, height)
        boxes.append(box)
    for index, (region, box) in enumerate(zip(regions, boxes)):
        blocks = []
        for source in request.get('ocr_blocks', []):
            if not source.get('text', '').strip() or not source.get('polygon'):
                continue
            if source.get('image_version', request.get('image_version')) != request.get('image_version'):
                raise ValueError('共享 OCR 图像版本不一致')
            poly = checked_polygon(source['polygon'], width, height)
            owners = [i for i, candidate in enumerate(boxes)
                      if intersection_area(poly, box_polygon(candidate)) / abs(signed_area(poly)) >= .8]
            # Crossing a crop edge is not repaired by clipping or splitting a token.
            if owners != [index] or any(x < box[0] or x > box[2] or y < box[1] or y > box[3] for x, y in poly):
                continue
            blocks.append({**source, 'polygon': [[x - box[0], y - box[1]] for x, y in poly]})
        yield region, box, image.crop(box), blocks


class CandidateSession:
    def __init__(self, provider):
        self.provider = provider
        self.config = verify_provider(provider)
        started = time.perf_counter()
        if provider == 'tableformer-raw':
            sys.path.insert(0, str(ROOT / self.config['source']))
            import torch
            from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
            torch.manual_seed(self.config['seed'])
            torch.set_num_threads(self.config['threads'])
            model = ROOT / self.config['model']
            config = json.loads((model / 'tm_config.json').read_text('utf-8'))
            config['model']['save_dir'] = str(model.resolve())
            config['predict']['disable_post_process'] = True
            self.engine = TFPredictor(config, device='cuda:0', num_threads=self.config['threads'])
            self.engine.enable_post_process = False
        else:
            sys.path.insert(0, str(ROOT / self.config['source']))
            from rapid_table import RapidTable, RapidTableInput, ModelType, EngineType
            self.engine = RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS,
                model_dir_or_path=ROOT / self.config['model'], engine_type=EngineType.ONNXRUNTIME,
                use_ocr=False, engine_cfg={'intra_op_num_threads': 4, 'inter_op_num_threads': 1, 'use_cuda': False}))
        self.loaded = time.perf_counter() - started

    def predict(self, image_path, request):
        import numpy as np
        from PIL import Image
        started = time.perf_counter()
        if request.get('image_sha256') and file_sha(image_path) != request['image_sha256']:
            raise ValueError('几何请求图像内容已变化')
        tables, upstream = [], []
        with Image.open(image_path) as source:
            image = source.convert('RGB')
            for index, (region, box, crop, blocks) in enumerate(region_inputs(image, request)):
                if self.provider == 'tableformer-raw':
                    page = {'image': np.asarray(crop), 'width': crop.width, 'height': crop.height,
                            'tokens': [{'id': i, 'text': b['text'], 'bbox': bounds(b['polygon'])}
                                       for i, b in enumerate(blocks)]}
                    outputs = self.engine.multi_table_predict(page, [[0, 0, crop.width, crop.height]],
                                                             do_matching=True, sort_row_col_indexes=False)
                    if len(outputs) != 1:
                        raise ValueError('TableFormer 输出表格数量不一致')
                    raw = outputs[0]
                    prediction = {'tf_table_cells': deepcopy(raw['predict_details']['table_cells']),
                                  'source_semantics': 'raw_structure', 'model_sha256': self.config['model_sha256']}
                else:
                    imgs = self.engine._load_imgs(np.asarray(crop)[:, :, ::-1].copy())
                    structures, boxes = self.engine.table_structure(imgs)
                    logic = self.engine.table_matcher.decode_logic_points(structures)
                    external = [(np.asarray([b['polygon'] for b in blocks], dtype=np.float32).reshape((-1, 4, 2)),
                                 tuple(b['text'] for b in blocks), tuple(float(b.get('confidence') or 0) for b in blocks))]
                    dt, rec = self.engine.get_ocr_results(imgs, 0, 1, external)
                    html = self.engine.table_matcher(structures, boxes, dt, rec)[0]
                    prediction = {'html': html, 'cell_bboxes': boxes[0].tolist(), 'logic_points': logic[0].tolist(),
                        'box_format': 'quad8', 'span_format': 'inclusive-r0-r1-c0-c1', 'model_sha256': self.config['model_sha256']}
                    raw = {'structure': structures, **prediction}
                tables.append({'region_id': region.get('id'), 'table_box': box, 'prediction': prediction,
                    'crop_to_image': [1, 0, box[0], 0, 1, box[1], 0, 0, 1], 'original_index': index,
                    'upstream_index': index, 'crop_ocr_sha256': fingerprint(blocks)})
                upstream.append(raw)
        return {'status': 'success', 'component': self.provider, 'experimental': True,
            'candidate_tables': tables, 'upstream_predictions': upstream,
            'coordinate_contract': 'crop-pixels-to-image-affine-v1', 'contract_version': 2,
            'image_version': request.get('image_version'), 'image_sha256': file_sha(image_path),
            'ocr_blocks': request.get('ocr_blocks', []), 'ocr_source': request.get('ocr_source'),
            'shared_ocr_sha256': fingerprint(request.get('ocr_blocks', [])),
            'model_revisions': self.config, 'source_sha256': fingerprint(self.config['files']),
            'contributes_to_votes': False, 'load_seconds': self.loaded,
            'inference_seconds': time.perf_counter() - started}

    def write(self, image, output, request):
        from ocr_workbench.atomic_files import write_json
        from ocr_workbench.table_geometry_worker import serializable
        prediction = serializable(self.predict(image, request))
        upstream = prediction.pop('upstream_predictions')
        write_json(Path(output) / 'upstream.json', upstream, durable=True)
        prediction['upstream_artifact'] = {'path': 'upstream.json', 'sha256': file_sha(Path(output) / 'upstream.json')}
        write_json(Path(output) / 'geometry.json', prediction, durable=True)
