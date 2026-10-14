"""Frozen native-PDF provenance and bounded header-repair experiment."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
RUN=os.environ.get('OCR_NATIVE_REPAIR_RUN','structure-repair-native-20260919-01')
if not RUN.startswith('structure-repair-native-') or not RUN.replace('-','').isalnum():raise ValueError('Invalid run')
BUILD=ROOT/'build'/RUN
AUDIT=ROOT/'audit'/RUN
PAGES=[('budget-2023','hk-budget-speech',74,[0,1]),
       ('budget-2024','hk-budget-speech',72,[0,1]),
       ('audit-86-ch01','hk-audit-report',21,[0]),
       ('audit-86-ch01','hk-audit-report',24,[0])]
if (AUDIT/'pilot-config.json').exists():PAGES=json.loads((AUDIT/'pilot-config.json').read_text('utf-8'))['pages']


def read(path):return json.loads(path.read_text('utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')


def extract():
    from ocr_workbench.pdf_worker import render_page
    from ocr_workbench.pdf_tables import extract_tables
    from ocr_workbench.page_processing import merge_page
    from ocr_workbench.native_tables import native_table_preview
    sources=BUILD/'sources'
    sources.mkdir(parents=True,exist_ok=True)
    for year in (2023,2024):
        target=sources/f'budget-{year}.pdf'
        if not target.exists():shutil.copy2(ROOT/f'build/structure-repair-20260919-01/sources/hk-budget-{year}/source.pdf',target)
    audit_source=sources/'hk-audit-86-ch01.pdf'
    sources.mkdir(parents=True,exist_ok=True)
    if not audit_source.exists():shutil.copy2(ROOT/'build/structure-repair-native-20260919-01/sources/hk-audit-86-ch01.pdf',audit_source)
    if not (sources/'audit-86-ch01.pdf').exists():shutil.copy2(audit_source,sources/'audit-86-ch01.pdf')
    protocol={'frozen_before_reference_calls':True,'pages':PAGES,'source_change':'fresh production PDF extraction, not historical same-input OCR replay',
        'render_dpi':150,'selection':'previous bounded discovery found real vector grids; first two selected budget tables and first small audit tables; no labels or model scores',
        'documents':len({p[0] for p in PAGES}),'template_groups':len({p[1] for p in PAGES}),'split':'development/regression; budget years share an exposed template',
        'mechanisms':['first grid row as column headers','first two grid rows as column headers','first column of body as row headers'],
        'max_affected_contents':8,'max_patches_per_table':6,'candidate_selection':'fewest affected IDs, then variant name; no reference-based selection',
        'content_preservation':'exact adopted literal and identity; do not split or merge; unknown blank remains unknown',
        'baseline':'actual native_table_preview over production render_page native words and pinned pdfplumber default grid',
        'reviewer':'gpt-6-astra; image-only reference including headers, frozen before comparisons',
        'continue_gate':{'correct_repairs':5,'documents':3,'preservation_violations':0},
        'scope':'header metadata repairs only; do not count as span/merge/split gains',
        'luna':'only after continue gate and actual product snapshot eligibility; no bypass',
        'formal_quality':False}
    pp=AUDIT/'protocol.json'
    if pp.exists():assert read(pp)==json.loads(json.dumps(protocol)), 'Protocol changed'
    else:save(pp,protocol)
    rows=[]
    for document,template,number,indices in PAGES:
        ident=f'{document}-p{number}';folder=BUILD/'pages'/ident
        folder.mkdir(parents=True,exist_ok=True)
        checkpoint=folder/'page.json'
        if checkpoint.exists():rows.append(read(checkpoint));continue
        source=sources/f'{document}.pdf';source_hash=sha(source);started=time.monotonic()
        rendered=render_page({'path':str(source),'page_number':number,'dpi':150,'image_output':str(folder/'page.png')})
        metadata=rendered['metadata'];image_hash=sha(folder/'page.png')
        version={'id':ident+':'+image_hash[:16],'sha256':image_hash,'width':metadata['width'],'height':metadata['height']}
        prediction=extract_tables(source,number,metadata)
        raw=merge_page(rendered['native'],[],version,{'id':document,'sha256':source_hash},{'id':ident,'page_number':number},'native')
        preview=native_table_preview(raw,prediction,version['width'],version['height'])
        row={'id':ident,'document':document,'template':template,'page':number,'selected_indices':indices,
             'source_sha256':source_hash,'image':str(folder/'page.png'),'image_sha256':image_hash,
             'version':version,'metadata':metadata,'raw':raw,'prediction':prediction,'preview':preview,
             'native_flagged':len(rendered['native']['flagged']),'seconds':time.monotonic()-started}
        save(checkpoint,row);rows.append(row)
        print(ident,'predicted',len(prediction['pdfplumber_tables']),'preview',len(preview['tables']) if preview else 0,'seconds',row['seconds'],flush=True)
        assert sha(source)==source_hash
    save(BUILD/'pages.json',rows)
    save(AUDIT/'extraction.json',[{k:v for k,v in r.items() if k not in ('raw','prediction','preview','metadata')} |
        {'preview_tables':len(r['preview']['tables']) if r['preview'] else 0} for r in rows])


REFERENCE_PROMPT='''You are an independent GPT-Astra visual reviewer, authorized to replace manual research annotation.
Read only this case directory. Do not inspect repository source, other cases, labels, model candidates,
old experiments or scores. Do not delegate. First inspect the full page image(s) using view_image at
original detail. You may make enlarged crops in this directory. Request.json identifies table regions
in image pixels to distinguish them from page prose; it does not supply structure or transcription.
For EACH requested table, reconstruct the complete logical grid from the image, excluding table
caption and footnotes. Include merged cells, empty cells and repeated values. Transcribe visible
characters; preserve signs, leading zeros, superscripts. Do not use arithmetic to correct the document.
Mark each cell is_header true or false. Header means a column/group heading or row label identifying
body values; totals and ordinary data values are not headers merely because they are bold. Use
header_role column/row/corner/null. If a heading role or cell boundary is ambiguous, record it as
uncertain. Do not invent precise source geometry. Record all unresolved positions.
Write response.json with {case_id, image_inspected:true, tables:[{id:requested table id, rows,columns,
cells:[{row,column,row_span,column_span,text,is_header,header_role}], unresolved:[{row,column,reason}],
notes:string}]}. Every requested table must be included. All logical slots must be covered exactly
once. This is a model reference, not a human label. No feedback-driven retry or editing after scoring.
'''


def packets():
    from PIL import Image
    rows=read(BUILD/'pages.json');documents=list(dict.fromkeys(r['document'] for r in rows));mapping=[]
    for i,document in enumerate(documents,1):
        case=f'case-{i:02d}';folder=BUILD/'reference-inbox'/case;folder.mkdir(parents=True,exist_ok=False)
        requests=[]
        for j,row in enumerate(r for r in rows if r['document']==document):
            filename=f'page-{j+1}.png';shutil.copy2(row['image'],folder/filename)
            for index in row['selected_indices']:
                original=row['prediction']['pdfplumber_tables'][index]
                ti=f'table-{len(requests)+1:02d}'
                requests.append({'id':ti,'image':filename,'region_polygon':original['polygon']})
                mapping.append({'case_id':case,'table_id':ti,'page_id':row['id'],'candidate_index':index,
                    'document':document,'template':row['template'],'image_sha256':sha(folder/filename)})
        save(folder/'request.json',{'case_id':case,'tables':requests})
        (folder/'INSTRUCTIONS.md').write_text(REFERENCE_PROMPT,'utf-8')
    save(AUDIT/'reference-assignments.json',mapping)
    save(AUDIT/'reference-protocol.json',{'prompt':REFERENCE_PROMPT,'requested_model':'gpt-6-astra',
        'context':'one independent image-only case per document, fork_turns=none','manual_reviews':0,
        'requests':[{'case_id':f'case-{i+1:02d}','sha256':sha(BUILD/'reference-inbox'/f'case-{i+1:02d}'/'request.json')} for i in range(len(documents))]})
    print(json.dumps(mapping,ensure_ascii=False,indent=2))


def patches():
    from ocr_workbench.structure_repair import content_baseline, native_header_patches, apply_patch
    rows={r['id']:r for r in read(BUILD/'pages.json')}
    records=[]
    for assignment in read(AUDIT/'reference-assignments.json'):
        row=rows[assignment['page_id']];index=assignment['candidate_index']
        record={**assignment,'candidates':[],'rejected':[]}
        if not row['preview']:
            record['rejected']=[{'kind':'native_preview_unavailable'}];records.append(record);continue
        # Exact preview order matches these no-preexisting-table page baselines.
        current=row['preview']['tables'][index]
        b=content_baseline(current,result_id=row['id']+':native-preview',revision=0,
            image_version=row['version']['id'],image_sha256=row['image_sha256'])
        started=time.perf_counter();candidates,rejected=native_header_patches(b)
        for c in candidates:
            c['applied']=apply_patch(b,c['patch'],revision=0,image_version=row['version']['id'],image_sha256=row['image_sha256'])
            c['literal_changes']=sum(x['text']!=y['text'] for x,y in zip(current['cells'],c['applied']['cells']))
            assert c['literal_changes']==0
        record.update(baseline=b,candidates=candidates,rejected=rejected,milliseconds=(time.perf_counter()-started)*1000)
        records.append(record)
    save(BUILD/'patches.json',records)
    summary={'tables':len(records),'documents':len({r['document'] for r in records}),
        'candidate_patches':sum(len(r['candidates']) for r in records),
        'tables_with_patches':sum(bool(r['candidates']) for r in records),
        'literal_changes':sum(c['literal_changes'] for r in records for c in r['candidates']),
        'largest_impact':max((len(c['patch']['affected_content_ids']) for r in records for c in r['candidates']),default=0),
        'max_milliseconds':max(r.get('milliseconds',0) for r in records),
        'quality':'unscored; Astra references not read','source_sha256':sha(ROOT/'src/ocr_workbench/structure_repair.py'),
        'candidates_sha256':sha(BUILD/'patches.json')}
    save(AUDIT/'patch-generation.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


def score_references():
    from ocr_workbench.complex_table_contract import validate_grid
    assignments=read(AUDIT/'reference-assignments.json')
    cases=sorted({a['case_id'] for a in assignments})
    locks=[{'case_id':c,'sha256':sha(BUILD/'reference-inbox'/c/'response.json')} for c in cases]
    lockpath=AUDIT/'reference-response-lock.json'
    if lockpath.exists():assert read(lockpath)==locks
    else:save(lockpath,locks)
    generated=read(AUDIT/'patch-generation.json')
    assert sha(BUILD/'patches.json')==generated['candidates_sha256']
    reference={}
    for case in cases:
        response=read(BUILD/'reference-inbox'/case/'response.json')
        assert response['case_id']==case and response['image_inspected'] is True
        requested=read(BUILD/'reference-inbox'/case/'request.json')['tables']
        assert sorted(t['id'] for t in response['tables'])==sorted(t['id'] for t in requested)
        for table in response['tables']:
            validate_grid(table)
            assert all(type(c.get('is_header')) is bool for c in table['cells'])
            reference[case,table['id']]=table
    normalize=lambda value:''.join(unicodedata.normalize('NFKC',value).split())
    records=[]
    for record in read(BUILD/'patches.json'):
        ref=reference[record['case_id'],record['table_id']]
        cells={(c['row'],c['column']):c for c in ref['cells']}
        uncertain={(u['row'],u['column']) for u in ref['unresolved']}
        def measure(table):
            result={'targets':len(cells),'structure_text_correct':0,'with_header_correct':0,'unmatched_outputs':0}
            byslot={(c['row'],c['column']):c for c in table['cells']}
            for key,expected in cells.items():
                actual=byslot.get(key)
                good=bool(actual and all(actual[k]==expected[k] for k in ('row_span','column_span')) and normalize(actual['text'])==normalize(expected['text']))
                result['structure_text_correct']+=good
                result['with_header_correct']+=bool(good and actual.get('is_header',False)==expected['is_header'] and key not in uncertain)
            result['unmatched_outputs']=sum(k not in cells for k in byslot)
            return result
        before=record['baseline']['table'];before_by={(c['row'],c['column']):c for c in before['cells']}
        item={k:record[k] for k in ('case_id','table_id','page_id','document','template')}
        item.update(before=measure(before),candidates=[],unresolved=ref['unresolved'])
        for option in record['candidates']:
            after=option['applied'];changes=[]
            for c in after['cells']:
                key=(c['row'],c['column']);old=before_by[key]
                if c.get('is_header',False)==old.get('is_header',False):continue
                expected=cells.get(key)
                qualified=bool(expected and key not in uncertain and all(c[k]==expected[k] for k in ('row_span','column_span')) and normalize(c['text'])==normalize(expected['text']))
                changes.append({'row':key[0],'column':key[1],'before':old.get('is_header',False),'after':c.get('is_header',False),
                    'reference':expected.get('is_header') if expected else None,'qualified':qualified,
                    'corrected':bool(qualified and old.get('is_header',False)!=expected['is_header'] and c.get('is_header',False)==expected['is_header']),
                    'harmed':bool(qualified and old.get('is_header',False)==expected['is_header'] and c.get('is_header',False)!=expected['is_header'])})
            item['candidates'].append({'variant':option['variant'],'after':measure(after),'changes':changes,
                'corrected':sum(c['corrected'] for c in changes),'harmed':sum(c['harmed'] for c in changes),
                'unqualified':sum(not c['qualified'] for c in changes),'literal_changes':option['literal_changes'],
                'safe_correct_repair':bool(changes and all(c['corrected'] for c in changes) and option['literal_changes']==0)})
        item['selected']=item['candidates'][0] if item['candidates'] else None
        records.append(item)
    selected=[r for r in records if r['selected'] and r['selected']['safe_correct_repair']]
    summary={'tables':len(records),'selected_correct_repairs':len(selected),'selected_correct_documents':len({r['document'] for r in selected}),
        'selected_correct_template_groups':len({r['template'] for r in selected}),
        'selected_corrected_header_cells':sum(r['selected']['corrected'] for r in records if r['selected']),
        'selected_harmed_header_cells':sum(r['selected']['harmed'] for r in records if r['selected']),
        'literal_changes':sum(r['selected']['literal_changes'] for r in records if r['selected']),
        'continue_gate_met':len(selected)>=5 and len({r['document'] for r in selected})>=3 and all(r['selected']['literal_changes']==0 for r in records if r['selected']),
        'scope':'Astra-reviewed header metadata only; not span/merge/split repair, objective truth or independent confirmation',
        'records':records}
    save(AUDIT/'reference-scores.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='records'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['extract','packets','patches','score']);a=p.parse_args()
    {'extract':extract,'packets':packets,'patches':patches,'score':score_references}[a.phase]()
