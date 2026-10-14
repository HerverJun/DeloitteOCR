"""Read-only deployment/model inventory; does not change installed bundles."""
import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
from geometry_eval_common import ROOT,sha,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('Runtime receipt is immutable')
    names=['PP-OCRv6_medium_det','PP-OCRv6_medium_rec','PaddleOCR-VL-1.6','PP-DocLayoutV3',
        'PP-LCNet_x1_0_table_cls','SLANeXt_wired','SLANeXt_wireless','RT-DETR-L_wired_table_cell_det','RT-DETR-L_wireless_table_cell_det']
    models=[]
    for name in names:
        folder=a.bundle/'models'/name;source=folder/'source-manifest.json';manifest=json.loads(source.read_text('utf-8'));files=[]
        for declared in manifest['files']:
            filename=declared.get('name') or Path(declared['file']).name
            path=folder/filename;actual=sha(path)
            files.append({'name':filename,'bytes':path.stat().st_size,'sha256':actual,'matches_source':actual==declared['sha256']})
        models.append({'name':name,'revision':manifest['revision'],'source_manifest_sha256':sha(source),'files':files})
    program="import sys,json;sys.path.insert(0,sys.argv[1]);from ocr_workbench._vendor.tableformer.tf_cell_matcher import CellMatcher;from ocr_workbench._vendor.tableformer.otsl import otsl_clean;import numpy;print(json.dumps({'python':sys.version,'numpy':numpy.__version__,'torch_imported':'torch' in sys.modules,'matcher_loaded':True}))"
    import_result=subprocess.run([str(a.bundle/'runtimes/service/python.exe'),'-B','-I','-c',program,str(ROOT/'src')],capture_output=True,text=True,check=True)
    vendor=ROOT/'src/ocr_workbench/_vendor/tableformer';upstream=json.loads((vendor/'upstream-lock.json').read_text('utf-8'))
    vendor_checked=[{'path':f['path'],'sha256':sha(vendor/f['path']),'matches_lock':sha(vendor/f['path'])==f['vendored_sha256']} for f in upstream['files']]
    gpu=subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv,noheader'],capture_output=True,text=True,check=True).stdout.strip()
    import psutil
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as registry:
        cpu=winreg.QueryValueEx(registry,'ProcessorNameString')[0]
    result={'version':2,'bundle_read_only':str(a.bundle),'models':models,'all_models_match_source':all(f['matches_source'] for m in models for f in m['files']),
        'vendor':vendor_checked,'service_import':json.loads(import_result.stdout),'gpu':gpu,'cpu':cpu,
        'ram_bytes':psutil.virtual_memory().total,'logical_cpu_count':psutil.cpu_count(),'platform':platform.platform(),
        'limitation':'Inventory verifies this existing local bundle; no new package or clean target-machine acceptance performed'}
    write_json(a.output,result);print(json.dumps({k:v for k,v in result.items() if k not in ('models','vendor')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
