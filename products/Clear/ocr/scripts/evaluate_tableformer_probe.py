"""N3 development-only experiment; frozen first 12 development inputs, no GT."""
import argparse
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from geometry_eval_common import ROOT,ensure_lock,file_lock,sha,verify_inputs,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--model-dir',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--ocr',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--device',default='cuda:0');a=p.parse_args()
    manifest=verify_inputs(a.manifest)
    if manifest.get('split')!='development':raise ValueError('N3 probe is restricted to development data')
    samples=manifest['samples'][:12]
    sys.path[:0]=[str(a.source),str(ROOT/'src')]
    import numpy as np
    import torch
    from PIL import Image
    from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
    from ocr_workbench.coordinates import bounds
    from ocr_workbench.table_matching import local_mapping,default_policy
    config=json.loads((a.model_dir/'tm_config.json').read_text('utf-8'));config['model']['save_dir']=str(a.model_dir.resolve())
    config['predict']['disable_post_process']=True
    policy=default_policy()
    lock={'version':2,'experiment':'N3-development-first-12','samples':[s['id'] for s in samples],'manifest_sha256':sha(a.manifest),
        'upstream_revision':'d1569fdffee1d09cb3111d6d5490991cb59d59fd','upstream_code':file_lock((a.source/'docling_ibm_models/tableformer').rglob('*.py')),
        'model_revision':'2199320848bb9a8a519d22e4b528185a4f9a6f64','model_sha256':sha(a.model_dir/'tableformer_accurate.safetensors'),
        'config':config,'policy':policy,'device':a.device,'threads':4,'seed':20260913,
        'dependencies':{m:importlib.metadata.version(m) for m in ['torch','torchvision','safetensors','numpy','Pillow']},
        'ocr_snapshot':file_lock([a.ocr/s['id']/'result.json' for s in samples]),'local_code':file_lock((ROOT/'src/ocr_workbench').rglob('*.py')),
        'index_compression_used_for_matching':False,'ground_truth_given_to_model':False}
    ensure_lock(a.output/'run-lock.json',lock)
    def denied(*args,**kwargs):raise RuntimeError('N3 inference is offline')
    socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied
    torch.manual_seed(20260913);torch.set_num_threads(4)
    started=time.perf_counter();engine=TFPredictor(config,device=a.device,num_threads=4);engine.enable_post_process=False
    write_json(a.output/'timing.json',{'model_load_seconds':time.perf_counter()-started,'device_name':torch.cuda.get_device_name() if a.device.startswith('cuda') else 'CPU'})
    for sample in samples:
        folder=a.output/sample['id']
        if (folder/'evaluation.json').exists():continue
        started=time.perf_counter()
        try:
            blocks=json.loads((a.ocr/sample['id']/'result.json').read_text('utf-8'))['blocks']
            with Image.open(a.manifest.parent/sample['image']) as im:image=np.asarray(im.convert('RGB'))
            page={'image':image,'width':sample['width'],'height':sample['height'],
                'tokens':[{'id':i,'text':b['text'],'bbox':bounds(b['polygon'])} for i,b in enumerate(blocks)]}
            output=engine.multi_table_predict(page,[[0,0,sample['width'],sample['height']]],do_matching=True,sort_row_col_indexes=False)[0]
            inference=time.perf_counter()-started;details=output['predict_details']
            raw=deepcopy(details['table_cells']);post=engine._post_processor.process(deepcopy(details),False)
            compressed=deepcopy(output['tf_responses']);dimensions=engine._compress_row_col_indexes(compressed)
            base={'model_sha256':lock['model_sha256'],'ocr_blocks':blocks,'ocr_source':'independent-ppocr:'+sample['sha256'],
                'image_version':sample['sha256'],'inference_seconds':inference}
            raw_prediction={**base,'tf_table_cells':raw,'source_semantics':'raw_structure'}
            post_prediction={**base,'tf_table_cells':post['table_cells'],'source_semantics':'matched_text_extent'}
            write_json(folder/'prediction.json',{'raw':raw_prediction,'postprocessed':post_prediction,'upstream_prediction':details['prediction'],
                'responses_before_compression':output['tf_responses'],'responses_after_compression':compressed,'compressed_dimensions':dimensions})
            raw_mapping=local_mapping(sample['fixed_edit'],raw_prediction,sample['width'],sample['height'],policy=policy)
            post_mapping=local_mapping(sample['fixed_edit'],post_prediction,sample['width'],sample['height'],policy=policy)
            result={'status':'success','inference_seconds':inference,'raw_mappings':raw_mapping,'postprocessed_mappings':post_mapping,
                    'seconds':time.perf_counter()-started,'peak_cuda_bytes':torch.cuda.max_memory_allocated() if a.device.startswith('cuda') else None}
        except Exception as error:
            import traceback
            result={'status':'failed','error':str(error),'traceback':traceback.format_exc(),'seconds':time.perf_counter()-started}
        write_json(folder/'evaluation.json',result)
        print(sample['id'],result['status'],round(result['seconds'],3),result.get('error',''),flush=True)


if __name__=='__main__':main()
