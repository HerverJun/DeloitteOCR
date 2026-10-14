"""Exercise the installed application and experimental providers without source overrides."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    p=argparse.ArgumentParser()
    for name in ('bundle','data','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();a.bundle=a.bundle.resolve();a.output.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(a.bundle/'app'))
    from ocr_workbench import __version__
    from ocr_workbench.service import create_app
    from ocr_workbench.geometry_provider_config import verify_provider
    from ocr_workbench.adapter import EngineAdapter
    from ocr_workbench.structure_store import VERSION
    app=create_app(a.bundle,a.output/'workspace','installed-smoke-token',start_queue=False,review_only=True)
    health=next(r.endpoint for r in app.routes if getattr(r,'path',None)=='/api/health')()
    assert health['status']=='ready'
    assert app.state.store.rows('PRAGMA user_version')[0]['user_version']==10
    modules={name:str(Path(sys.modules['ocr_workbench.'+name].__file__).resolve()) for name in ('service','structure_store','geometry_provider_config')}
    assert all(Path(path).is_relative_to(a.bundle/'app') for path in modules.values())
    lock=json.loads((a.bundle/'locks/structure-source-lock.json').read_text('utf-8'))
    count=0
    for item in lock['files']:
        if item['path'].startswith('src/'):
            path=a.bundle/'app'/item['path'][4:]
            assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256'];count+=1
    sample='pilot-column-span-1-p1';native=json.loads((a.data/'pages'/sample/'native.json').read_text('utf-8'))['native']
    image=a.data/'pages'/sample/'input.png'
    sha=hashlib.sha256(image.read_bytes()).hexdigest()
    units=[dict(u,id='smoke-native-'+str(i),source_kind='native',granularity='word') for i,u in enumerate(native['units']) if u.get('reliable')]
    from PIL import Image
    with Image.open(image) as im:width,height=im.size
    from ocr_workbench.coordinates import box_polygon
    request={'image_version':'installed-smoke','image_sha256':sha,'ocr_blocks':units,'ocr_source':'immutable-native-smoke',
        'regions':[{'id':'whole-page-startup-only','polygon':box_polygon([0,0,width,height])}],'allow_auxiliary_ocr':False}
    rows=[]
    for provider in ('tableformer-raw','rapidtable'):
        verify_provider(provider);adapter=EngineAdapter(a.bundle,'geometry',a.output/'sessions');adapter.configure_geometry(provider)
        try:
            tick=time.perf_counter();adapter.load();loaded=time.perf_counter()-tick
            output=a.output/provider;output.mkdir()
            result=adapter.recognize(image,output,request)
            assert result['component']==provider and result['ocr_blocks']==units
            rows.append({'provider':provider,'status':'success','worker_load_seconds':loaded,'candidate_tables':len(result['candidate_tables'])})
        finally:adapter.unload()
    receipt={'version':__version__,'schema':10,'modules':modules,'source_files_verified':count,'health':health,'providers':rows,
        'scope':'installed offline startup and provider contract smoke; full-page region is for startup only, not a quality evaluation',
        'external_downloads':False,'structure_version':VERSION}
    (a.output/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
