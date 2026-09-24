"""Small, bounded public collection and frozen qualification, without inference."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time

from complex_table_run import ROOT, BUILD, AUDIT, initialize, save, sha, update


def inventory():
    initialize()
    inputs = []
    for folder in ('build/document-workflow', 'build/table-matching-v2',
                   'build/tableformer-expanded-20260914', 'build/tableformer-next-20260915',
                   'build/public-quality-20260916', 'build/structure-workflow-20260916'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and path.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg'}:
                inputs.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': sha(path)})
    save(AUDIT / 'exclusions.json', {'rule': 'All historical documents and their template/derived variants are regression only', 'files': inputs})
    paths = [* (ROOT / 'config').glob('*.json'), * (ROOT / 'config/runtime-locks').glob('*'),
             * (ROOT / 'src/ocr_workbench').glob('structure*.py'), ROOT / 'src/ocr_workbench/table_matching.py',
             ROOT / 'src/ocr_workbench/external_review.py']
    save(AUDIT / 'inventory.json', {'source_version': (ROOT/'src/ocr_workbench/__init__.py').read_text('utf-8'),
        'working_tree': subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True),
        'files': [{'path':p.relative_to(ROOT).as_posix(),'sha256':sha(p)} for p in paths],
        'runtimes': {str(p):p.exists() for p in [Path('E:/DeloitteOCR-Intranet-20260918/bundle/runtimes/service/python.exe'),
            Path('E:/DeloitteOCR-Intranet-20260918/bundle/runtimes/pdf/python.exe')]},
        'reuse': ['structure_candidates', 'structure_proposals', 'geometry_evidence manual precedence',
                 'multimodal_requests external queue', 'DPAPI single connection', 'structure decisions and history'],
        'gaps': ['quadratic token candidate generation', 'no structure API contract', 'no financial consistency rules',
                 'empty state does not distinguish absent evidence from true blank']})
    update('A01','done',[f'audit/{AUDIT.name}/inventory.json',f'audit/{AUDIT.name}/exclusions.json'],engineering='passed',next_step='Freeze annotation and evaluation contracts; collect public sources.')


def contracts():
    protocol = json.loads((ROOT/'config/geometry-evaluation-v3.json').read_text('utf-8'))
    protocol.update(version='complex-tables-v1', frozen_utc=datetime.now(timezone.utc).isoformat(),
        sampling_unit='document/template cluster; 2000 seeded cluster bootstrap resamples, 95% percentile interval',
        strict_offered_quality='C/(A+extra+duplicate); missing/timeout/inference failure remain in N',
        merge_relation={'minimum_recall':.9,'minimum_precision':.99,'identity_and_exact_span_required':True},
        row_column={'minimum_accuracy':.99,'identity_required':True},
        text_preservation={'maximum_omitted':0,'maximum_added':0,'maximum_duplicate':0},
        empty={'minimum_precision':.99,'minimum_recall':.9,'unknown_is_not_blank':True},
        repeated={'minimum_coverage':.9,'maximum_wrong':.01},
        usable_table={'minimum_rate':.8,'definition':'Every target has correct identity, literal text, row/column and span, no extras/duplicates; critical values all exact'},
        group_minimums={'merged':{'cells':200,'documents':20},'empty':{'cells':200,'documents':20},
            'repeated':{'cells':500,'documents':20},'native_pdf':{'documents':20},'physical_scan':{'documents':20},
            'physical_photo':{'documents':20},'large_table':{'tables':20},'wide_table':{'tables':20},'multiple_tables':{'pages':20}},
        large_table='at least 500 slots or 1000 source tokens',wide_table='at least 15 columns',
        API_net_benefit='paired all-target and triggered-target corrected minus harmed; document-cluster interval lower bound > 0; harm <= 1%',
        thresholds_rationale='Retain historical geometry gates; strict text invariants; high precision for risk groups; 80% whole-table exploratory gate does not override cell gates',
        scopes=['engineering','agent_audited_exploration','official_verified_acceptance'],
        automatic_adoption=False)
    save(AUDIT/'evaluation-protocol.json',protocol)
    rules={'formal_documents':100,'formal_tables':120,'formal_cells':6000,'all_group_minimums_required':True,
        'label_levels':['official_verified','agent_audited','weak','unlabelled'],
        'weak_in_primary':False,'human_signoff':False,'render_is_scan':False,
        'split_rule':'sha256(template_group) modulo 10: 0-5 dev, 6 validation, 7-8 local sealed, 9 API sealed; historical/seen groups regression; unresolved groups dev',
        'exposure_rule':'Inspecting a sealed input/label transfers its entire group to development; never relabel as unseen',
        'near_duplicates':'Exact file/image hashes, normalized text, digit-normalized text and quantized cell layout; candidate matches union template groups conservatively'}
    save(AUDIT/'qualification-rules.json',rules)
    contract={'version':1,'cell_required':['id','row','column','row_span','column_span','text','text_state','text_polygon','cell_polygon','header_ids'],
        'table_required':['id','document_id','template_group','page','polygon','rows','columns','cells','image_sha256','image_version','page_to_crop'],
        'text_states':['sourced','verified_blank','missing','illegible','unknown'],
        'coordinate_convention':'half-open zero-based slots; full cell polygon distinct from tight text polygon; homogeneous 3x3 page-to-crop transform',
        'source_identity':'IDs assigned from source annotation; never inferred from evaluated prediction',
        'boundary_disputes':'Record ambiguity; excluded geometry items reported separately with original denominator and reason; no removal because a model failed',
        'continuations':'page boundary flagged; no single-page completeness claim for truncated table',
        'validation':['positive rectangular spans','all slots covered exactly once','unique cell/token IDs','finite bounded polygons','invertible transform','known header references']}
    save(AUDIT/'annotation-contract.json',contract)
    update('A06','done',[f'audit/{AUDIT.name}/evaluation-protocol.json',f'audit/{AUDIT.name}/qualification-rules.json'],engineering='passed',next_step='Run annotation contract validator before marking A04 complete.')


SOURCES = [
    ('hk-budget-2025','https://www.budget.gov.hk/2025/chi/pdf/c_budget_speech_2025-26.pdf','native_pdf','hk-budget-speech','zh','public government report; local research only; not redistributed'),
    ('hk-accounts-2024','https://www.try.gov.hk/internet/pde_ac_202324.pdf','native_pdf','hk-treasury-accounts','zh-en','public government accounts; local research only; not redistributed'),
    ('hk-audit-85','https://www.aud.gov.hk/pdf_c/c85ch01.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('hk-audit-84','https://www.aud.gov.hk/pdf_c/c84ch01.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('microsoft-annual-2025','https://www.microsoft.com/investor/reports/ar25/download-center/2025_Annual_Report.pdf','native_pdf','microsoft-annual','en','public issuer report; local research only; not redistributed'),
    ('pubtables-license','https://raw.githubusercontent.com/microsoft/table-transformer/main/LICENSE','metadata','pubtables','en','official license discovery'),
    ('fintabnet-readme','https://raw.githubusercontent.com/ibm-aur-nlp/FinTabNet/master/README.md','metadata','fintabnet','en','official dataset and terms discovery'),
    ('wtw-readme','https://raw.githubusercontent.com/wangwen-whu/WTW-Dataset/master/README.md','metadata','wtw','zh-en','official photographed table dataset discovery'),
    ('hk-audit-85-ch1','https://www.aud.gov.hk/pdf_ca/c85ch01.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('hk-audit-85-ch2','https://www.aud.gov.hk/pdf_ca/c85ch02.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('hk-audit-85-ch3','https://www.aud.gov.hk/pdf_ca/c85ch03.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('hk-audit-85-ch4','https://www.aud.gov.hk/pdf_ca/c85ch04.pdf','native_pdf','hk-audit-chapter','zh','public government audit; local research only; not redistributed'),
    ('fintabnet-card-mirror','https://hf-mirror.com/datasets/docling-project/FinTabNet_OTSL/raw/main/README.md','metadata','fintabnet','en','official dataset card via public mirror; verify provenance'),
    ('wtw-license','https://raw.githubusercontent.com/wangwen-whu/WTW-Dataset/main/License','metadata','wtw','en','official terms'),
    ('wtw-access','https://tianchi.aliyun.com/dataset/108587','metadata','wtw','zh','official linked dataset landing page'),
]


def collect():
    import requests
    def fetch(source):
        key,url,category,group,language,usage=source
        folder=BUILD/'raw'/key
        folder.mkdir(parents=True,exist_ok=True)
        receipt=folder/'receipt.json'
        if receipt.exists():
            prior=json.loads(receipt.read_text('utf-8'))
            if prior['status']=='success' and sha(ROOT/prior['path'])==prior['sha256']:
                return prior
            if prior['status']=='failed':
                return prior  # same failed method is not a new attempt
        record={'id':key,'url':url,'category':category,'template_group':group,'language':language,'usage':usage,
            'accessed_utc':datetime.now(timezone.utc).isoformat(),'status':'failed','label_level':'unlabelled','attempts':[]}
        session=requests.Session();session.trust_env=False
        try:
            started=time.monotonic()
            with session.get(url,timeout=(12,25),stream=True) as response:
                record.update(final_url=response.url,http_status=response.status_code)
                response.raise_for_status()
                target=folder/('source.pdf' if category=='native_pdf' else 'source.txt')
                partial=target.with_suffix('.part')
                with partial.open('wb') as out:
                    for chunk in response.iter_content(256*1024):
                        if time.monotonic()-started>100 or out.tell()>60*1024*1024:
                            raise TimeoutError('Bounded document download budget exceeded')
                        out.write(chunk)
                if category=='native_pdf' and not partial.read_bytes()[:5]==b'%PDF-':
                    raise ValueError('Response is not a PDF')
                partial.replace(target)
                record.update(status='success',path=target.relative_to(ROOT).as_posix(),sha256=sha(target),bytes=target.stat().st_size)
        except Exception as error:
            record['error']=str(error)
        record['attempts'].append({'method':'official direct GET with bounded stream','status':record['status'],'error':record.get('error')})
        save(receipt,record)
        print(key,record['status'],flush=True)
        return record
    with ThreadPoolExecutor(max_workers=3) as pool:
        records=list(pool.map(fetch,SOURCES))
    (BUILD/'sources.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),'utf-8')
    save(AUDIT/'acquisition-report.json',{'sources':records,'human_signoff':False,'not_independent_labels':True})
    update('A02','done_with_gaps',[f'build/{BUILD.name}/sources.jsonl',f'audit/{AUDIT.name}/acquisition-report.json'],engineering='passed',quality='not_met',next_step='Inspect downloaded document pages, follow official dataset links; build grouped manifests.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['inventory','contracts','collect'])
    args=parser.parse_args()
    globals()[args.phase]()
