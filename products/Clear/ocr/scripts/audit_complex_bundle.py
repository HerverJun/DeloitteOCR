"""CPU-only smoke, importing only the newly packaged application and PDF runtime."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--pdf-only',action='store_true')
    a=p.parse_args();bundle=a.bundle.resolve();out=a.output.resolve()
    sys.path.insert(0,str(bundle/'app'))
    if a.pdf_only:
        from fpdf import FPDF
        from ocr_workbench.pdf_tables import extract_tables
        doc=FPDF(unit='pt',format=(300,200));doc.set_auto_page_break(False);doc.add_page()
        for x in [30,150,270]:doc.line(x,30,x,150)
        for y in [30,70,110,150]:doc.line(30,y,270,y)
        doc.set_font('Helvetica',size=12)
        for x,y,text in [(40,55,'Account'),(160,55,'Amount'),(40,95,'Revenue'),
                         (160,95,'1.00'),(40,135,'Total'),(160,135,'1.00')]:doc.text(x,y,text)
        sample=out/'synthetic.pdf';doc.output(str(sample))
        result=extract_tables(sample,1,{'render_dpi':72,'user_unit':1,'width':300,'height':200})
        assert len(result['pdfplumber_tables'])==1
        assert len(result['pdfplumber_tables'][0]['cells'])==6
        print(json.dumps({'passed':True,'runtime':sys.executable,'component':result['component'],'cells':6,'network_used':False}))
        return
    out.mkdir(parents=True,exist_ok=False)
    from ocr_workbench import __version__
    from ocr_workbench.service import create_app
    from ocr_workbench.complex_table_contract import validate_grid
    from ocr_workbench.financial_checks import check_tables
    from ocr_workbench.table_matching import policy_for_algorithm
    from ocr_workbench.structure_arbitration import VERSION, LIMITS
    app=create_app(bundle,out/'workspace','isolated-package-smoke',start_queue=False,review_only=True)
    health=next(r.endpoint for r in app.routes if getattr(r,'path',None)=='/api/health')()
    assert health['status']=='ready' and __version__=='0.13.0rc1'
    schema=app.state.store.rows('PRAGMA user_version')[0]['user_version'];assert schema==12
    values=[['Account','Amount'],['A','0.10'],['B','0.20'],['Total','0.31']]
    table={'rows':4,'columns':2,'caption':'单位：万元','cells':[
        {'row':r,'column':c,'row_span':1,'column_span':1,'text':v,'is_header':r==0}
        for r,row in enumerate(values) for c,v in enumerate(row)]}
    before=deepcopy(table);assert validate_grid(table)['covered_slots']==8
    report=check_tables([table]);assert table==before
    assert any(i.get('difference')=='0.01' for i in report['issues'])
    assert policy_for_algorithm('local-v4')['spatial_index']
    modules={k:str(Path(v.__file__).resolve()) for k,v in sys.modules.items()
             if k.startswith('ocr_workbench') and getattr(v,'__file__',None)}
    assert all(Path(v).is_relative_to(bundle/'app') for v in modules.values())
    copied=json.loads((bundle/'locks/multimodal-copy-receipt.json').read_text('utf-8'))
    count=0
    for row in copied['copied_source']:
        parts=Path(row['path']).parts
        target=bundle/'app'/Path(*parts[1:]) if parts[0]=='src' else None
        if target:
            assert hashlib.sha256(target.read_bytes()).hexdigest()==row['sha256'];count+=1
    probe=subprocess.run([str(bundle/'runtimes/pdf/python.exe'),'-B','-X','utf8',str(Path(__file__).resolve()),
        '--bundle',str(bundle),'--output',str(out),'--pdf-only'],capture_output=True,text=True,encoding='utf-8',check=True)
    pdf=json.loads(probe.stdout)
    receipt={'passed':True,'version':__version__,'schema':schema,'health':health,'modules':modules,
        'packaged_source_files_verified':count,'pdf':pdf,'structure_contract':'passed','financial_immutable_decimal':'passed',
        'arbitration_version':VERSION,'limits':LIMITS,'gpu_inference':'not_measured','paid_api_calls':0,
        'runtime':sys.executable,'scope':'packaged CPU startup, source, schema, PDF and structure/finance contracts'}
    (out/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(receipt,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
