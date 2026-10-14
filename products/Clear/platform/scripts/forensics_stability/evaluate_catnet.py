"""Gated, one-loaded-model comparison of legacy, halo and official full image.

The official reference executes the upstream _create_tensor method and network
without changing upstream files. Its JPEG reader is adapted to the already
verified original jpeglib DCT because upstream jpegio is unavailable on Windows.
Full-image logits use the existing product's recorded bilinear/softmax mapping
to stored pixels, so the tiling comparison does not conflate output resolutions.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import sys
import time
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE), str(SOURCE/'src')]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(output, name, value):
    path = output/name
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    for attempt in range(30):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.05)


def official_reference(session, prepared, source, output):
    import numpy as np
    import torch
    import torch.nn.functional as functional
    from PIL import Image
    upstream = session.root/'source/Splicing/data/AbstractDataset.py'
    tree = ast.parse(upstream.read_text('utf-8-sig'))
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AbstractDataset')
    method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == '_create_tensor')
    namespace = {'np': np, 'torch': torch, 'Image': Image, 'random': random}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(upstream), 'exec'), namespace)
    dataset = SimpleNamespace(_crop_size=None, _grid_crop=True, _blocks=('RGB','DCTvol','qtable'),
                              DCT_channels=1,
                              _get_jpeg_info=lambda path: ([prepared.coefficients], [prepared.quantization]))
    tensor, label, qtable = namespace['_create_tensor'](dataset, str(source), None)
    with torch.inference_mode():
        logits = session.model(tensor[None].to(session.device), qtable[None].to(session.device))
        native = logits.softmax(dim=1)[0,1].cpu().numpy()
        mapped = functional.interpolate(logits, size=tuple(tensor.shape[1:]), mode='bilinear', align_corners=True).softmax(dim=1)[0,1].cpu().numpy()
    height, width = prepared.rgb.shape[:2]
    np.save(output.with_suffix('.native.npy'), native, allow_pickle=False)
    return mapped[:height,:width].copy(), {
        'official_tensor_source_sha256': sha(upstream),
        'official_network_sha256': session.model_hashes['network'],
        'tensor_shape': list(tensor.shape), 'native_map_shape': list(native.shape),
        'reader_adapter': 'Original quantized jpeglib Y and actual luminance quantization table; no recompression',
        'output_mapping': 'Product-compatible bilinear logits to padded pixel grid, align_corners=True, then softmax and crop',
        'scope': 'Official dataset tensor constructor and network full-image reference; not an unmodified upstream CLI run'}


def verify_frozen_worker(request, sample, expected, output, control):
    import numpy as np
    baseline = Path(request['baseline_source'])/'modules/manual_multimodal/worker.py'
    spec = importlib.util.spec_from_file_location('frozen_forensics_worker', baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = output/'frozen-worker-parity'
    directory.mkdir()
    module.OUT, module.CONTROL = directory, control
    module.initialize_temporary_directory(directory)
    value = module.catnet({'models': str(Path(request['program'])/'models/multimodal'),
                           'inputs': [{'path': str(sample)}], 'options': {'device': request['options']['device']},
                           'runner': {'catnet_tile_size': 1024}})
    actual = np.load(directory/'heatmap-probabilities.npy', allow_pickle=False)
    maximum = float(np.max(np.abs(actual - expected)))
    result = {'frozen_worker_sha256': sha(baseline), 'input_sha256': sha(sample),
              'max_absolute_difference': maximum, 'tolerance': 1e-6,
              'equivalent': actual.shape == expected.shape and maximum <= 1e-6,
              'algorithm': value['algorithm']}
    publish(output, 'legacy-parity.json', result)
    if not result['equivalent']:
        raise ValueError('Refactored legacy inference differs from the actual frozen worker')
    return result


def run(request, output, control):
    import numpy as np
    import torch
    from PIL import Image
    from modules.manual_multimodal.catnet_inference import CatNetSession, read_catnet_jpeg
    from scripts.forensics_stability.metrics import top_one_percent, pixel_metrics, calibrated_threshold, image_metrics, seam_band
    from scripts.forensics_stability.build_dataset import local_f
    dataset = local_f(request['dataset'])
    manifest_path = dataset/'manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    if len(manifest['samples']) != 320 or manifest['base_families'] != 64:
        raise ValueError('The frozen benchmark must contain 64 base families and 320 samples')
    if shutil.disk_usage(output).free < 2 * 1024**3:
        raise ValueError('Less than 2 GiB evaluation reserve on F:')
    samples = sorted(manifest['samples'], key=lambda row: ({'development':0,'calibration':1,'final_test':2}[row['split']], row['sample_id']))
    if request.get('limit'):
        samples = samples[:request['limit']]
    source_files = [*sorted((SOURCE/'modules/manual_multimodal').glob('*.py')), Path(__file__), SOURCE/'scripts/forensics_stability/metrics.py']
    source_hashes = {path.relative_to(SOURCE).as_posix(): sha(path) for path in source_files}
    publish(output, 'identity.json', {'dataset_sha256': sha(manifest_path), 'source_hashes': source_hashes,
                                    'device': request['options']['device'], 'samples': len(samples),
                                    'baseline_source': request['baseline_source'], 'pixel_threshold': .5,
                                    'seam_band': {'vertical_boundaries':[896,1024], 'radius':64}})
    torch.manual_seed(20261014)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    session = CatNetSession(Path(request['program'])/'models/multimodal/catnet', request['options']['device'])
    synchronize = torch.cuda.synchronize if session.device == 'cuda' else lambda: None
    prepared = read_catnet_jpeg(dataset/samples[0]['input'], temporary_root=output/'runtime/native')
    warmup = []
    for halo in (0,64):
        start = time.perf_counter()
        session.infer(prepared, halo=halo, check=control.check)
        synchronize()
        warmup.append({'halo':halo, 'seconds':time.perf_counter()-start})
    publish(output, 'warmup.json', warmup)
    rows, seams, thresholds = [], [], None
    for number, sample in enumerate(samples):
        control.check()
        if sample['split'] == 'final_test' and thresholds is None:
            thresholds = {mode: calibrated_threshold([r['image_score'] for r in rows if r['mode'] == mode and r['split'] == 'calibration' and not r['is_modified']])
                          for mode in ('legacy','halo64')}
            publish(output, 'calibration-frozen.json', {'thresholds':thresholds, 'before_final_test':True,
                    'calibration_sample_ids': sorted({r['sample_id'] for r in rows if r['split'] == 'calibration'}),
                    'selection': 'Higher empirical 95th percentile of normal calibration top-one-percent responses; ties negative',
                    'created_unix':time.time()})
        path, mask_path = local_f(dataset/sample['input']), local_f(dataset/sample['mask'])
        if sha(path) != sample['input_sha256'] or sha(mask_path) != sample['mask_sha256']:
            raise ValueError('Frozen input or operation mask changed: ' + sample['sample_id'])
        prepared = read_catnet_jpeg(path, temporary_root=output/'runtime/native')
        with Image.open(mask_path) as mask_image:
            mask = np.asarray(mask_image) != 0
        predictions = {}
        # Alternate order to avoid consistently favouring one mode's warm state.
        modes = (('legacy',0),('halo64',64)) if number % 2 == 0 else (('halo64',64),('legacy',0))
        for mode, halo in modes:
            synchronize()
            start = time.perf_counter()
            prediction = session.infer(prepared, halo=halo, check=control.check,
                progress=lambda done,total: publish(output,'progress.json',{'stage':f"{number+1}/{len(samples)} {sample['sample_id']} {mode} {done}/{total}",
                                                       'worker_epoch':control.epoch}))
            synchronize()
            elapsed = time.perf_counter()-start
            probabilities = prediction.probabilities
            folder = output/mode
            folder.mkdir(exist_ok=True)
            map_path = folder/(sample['sample_id']+'.npy')
            np.save(map_path, probabilities, allow_pickle=False)
            row = {'sample_id':sample['sample_id'],'base_id':sample['base_id'],'split':sample['split'],
                   'family':sample['family'],'variant':sample['variant'],'is_modified':sample['is_modified'],
                   'mode':mode,'image_score':top_one_percent(probabilities),'pixel':pixel_metrics(probabilities,mask),
                   'infer_seconds':elapsed,'map':map_path.relative_to(output).as_posix(),'map_sha256':sha(map_path),
                   'input_sha256':prepared.input_sha256,'inference':prediction.inference}
            rows.append(row)
            predictions[mode] = probabilities
            with (output/'records.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        if number == 0:
            publish(output,'progress.json',{'stage':'Verifying actual frozen-worker legacy equivalence','worker_epoch':control.epoch})
            verify_frozen_worker(request,path,predictions['legacy'],output,control)
        if sample['seam_comparison']:
            folder = output/'official-full-reference'
            folder.mkdir(exist_ok=True)
            full_path = folder/(sample['sample_id']+'.npy')
            control.check()
            reference, reference_metadata = official_reference(session,prepared,path,full_path)
            np.save(full_path,reference,allow_pickle=False)
            band = seam_band(reference.shape)
            row = {'sample_id':sample['sample_id'], 'base_id':sample['base_id'],
                   'is_modified':sample['is_modified'], 'input_sha256':prepared.input_sha256,
                   'legacy_seam_mae':float(np.abs(predictions['legacy']-reference)[band].mean(dtype=np.float64)),
                   'halo_seam_mae':float(np.abs(predictions['halo64']-reference)[band].mean(dtype=np.float64)),
                   'legacy_full_mae':float(np.abs(predictions['legacy']-reference).mean(dtype=np.float64)),
                   'halo_full_mae':float(np.abs(predictions['halo64']-reference).mean(dtype=np.float64)),
                   'reference_map':full_path.relative_to(output).as_posix(),'reference_sha256':sha(full_path),
                   'reference':reference_metadata}
            seams.append(row)
            publish(output,'seam-records.json',seams)
        if sha(path) != sample['input_sha256']:
            raise ValueError('Input bytes changed during evaluation')
        publish(output,'progress.json',{'stage':f'Completed {number+1}/{len(samples)}', 'worker_epoch':control.epoch,
                                       'completed_samples':number+1,'total_samples':len(samples)})
        if (number+1) % 20 == 0:
            print(f'CAT comparison completed {number+1}/{len(samples)}',flush=True)
    if any(sha(SOURCE/name) != identity for name,identity in source_hashes.items()):
        raise ValueError('Inference/evaluation source changed during the frozen run')
    final = {mode: image_metrics([row for row in rows if row['mode'] == mode and row['split'] == 'final_test'], thresholds[mode])
             for mode in ('legacy','halo64')} if thresholds is not None else None
    result = {'format':'forensics-catnet-comparison-v1','status':'complete','samples':len(samples),
              'model_hashes':session.model_hashes,'torch':session.torch_version,'actual_device':session.device,
              'dataset_sha256':sha(manifest_path),'source_hashes':source_hashes,'thresholds':thresholds,
              'final_test':final,'seam_groups':len({r['base_id'] for r in seams}),'seam_records':seams,
              'scope':manifest['scope'],'records_file':'records.jsonl','normal_map_f1_policy':'undefined; normal errors reported separately',
              'business_accuracy_qualified':False,'network_policy':'all_outbound_connections_denied'}
    publish(output,'result.json',result)
    control.check()
    control.event('completed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',type=Path,required=True)
    args = parser.parse_args()
    output = args.request.resolve().parent
    if output.drive.upper() != 'F:':
        raise ValueError('Evaluation output must stay on F:')
    request = json.loads(args.request.read_text('utf-8'))
    from modules.manual_multimodal.worker import initialize_temporary_directory, deny_network
    from modules.manual_multimodal.worker_protocol import WorkerControl
    initialize_temporary_directory(output)
    deny_network()
    control = WorkerControl(request,lambda name,value:publish(output,name,value))
    control.wait('start')
    run(request,output,control)


if __name__ == '__main__':
    main()
