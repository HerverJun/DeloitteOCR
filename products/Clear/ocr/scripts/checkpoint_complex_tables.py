"""Consolidate existing evidence only; never infer completion from task titles."""
import json
from pathlib import Path
from complex_table_run import ROOT,BUILD,AUDIT,save,sha,update


def main():
    focused=json.loads((AUDIT/'regression/focused-01.json').read_text('utf-8'))
    save(AUDIT/'annotation-contract-checks.json',{'passed':focused['passed'],'test_source':'tests/test_complex_tables.py::AnnotationTests',
        'receipt':'regression/focused-01.json','invariants':['complete grid','unique IDs','header references','invertible coordinates','blank text semantics']})
    update('A04','done',[f'audit/{AUDIT.name}/annotation-contract.json',f'audit/{AUDIT.name}/annotation-contract-checks.json'],engineering='passed',next_step='Final source regression and evidence reconciliation in progress.')
    manifest=json.loads((BUILD/'dataset/manifest.json').read_text('utf-8'))
    gaps=[
      {'id':'formal-data','evidence':'dataset/qualified-manifest.json','attempted':['5 downloaded public government PDFs / 10 tables',
          'official PubTables license','FinTabNet original broken URL and successful dataset-card mirror','WTW official README, license and linked public landing page'],
        'missing':'100 new independent documents, 120 tables, 6000 cells and all required subgroups; independent official labels; fresh scan/photo categories',
        'impact':['formal local quality not_met','fresh sealed acceptance not_measured'],
        'fallback':'7 Agent-audited exploratory tables, 3 retained weak/rejected labels, 120 historical crop replays; no render-as-scan claim',
        'retry_trigger':'New public independently labelled documents and unexposed template groups become available'},
      {'id':'real-api','attempted':['inspected application connection contract without reading personal credential stores','OpenAI and Anthropic loopback services with image and invalid-response tests'],
        'missing':'An explicitly authorized real service connection for this public-data evaluation',
        'impact':['real API net benefit, model version, usage and monetary cost not_measured'],
        'fallback':'real product queues with loopback providers and paired metric pipeline',
        'retry_trigger':'An authorized connection is available; run frozen second sealed group without reusing inspected data'},
      {'id':'human-device','attempted':['automated browser adoption/undo','source backend and frontend tests'],
        'missing':'actual human efficiency timing and target-device GPU inference in this round',
        'impact':['human time saved not_measured','target-device GPU inference not_measured'],
        'fallback':'reproducible script operation counts and validated bundled model hashes',
        'retry_trigger':'A real operator trial or target-device session is available'},
      {'id':'performance-high-risk','attempted':['spatial pruning','axis scan pruning','direct axis index; no further tuning'],
        'missing':'P95 <=200ms including final correspondence evidence assembly on 1000/2000-cell synthetic tables',
        'impact':['performance gate not_met for large-table group','local-v4 remains experimental'],
        'fallback':'deadline-bounded search and completely checked anchors; explicit fallback for unresolved regions',
        'retry_trigger':'New independently benchmarked implementation with lower evidence-assembly cost'},
      {'id':'initial-timing-receipt','attempted':['initial synthetic timing','later preserved direct-axis scan and indexed receipts'],
        'missing':'unrounded first spatial-only timing file overwritten by initial measurement helper; rounded tool output remains',
        'impact':['do not use first spatial-only timings as final comparison evidence'],
        'fallback':'all subsequent timing receipts use unique names; final selected strategy has retained raw timing evidence',
        'retry_trigger':'Re-run the preserved baseline source in a fresh output name if that initial exact timing comparison is needed'}]
    save(AUDIT/'gaps.json',gaps)
    save(AUDIT/'candidate-contract.json',{'version':'structure-review-v2','implementation':['structure_diagnostics.py','complex_table_contract.py'],
        'fields':['original_cell_id','candidate_cell_id','token_ids','cell_polygon','original_polygon','transform','image_version','range_semantics','candidate_variant'],
        'native_route':'original PDF native text/vector provider plus labelled shading-free alternative',
        'raster_route':'existing Paddle/TableFormer/RapidTable predictions with fixed OCR pool','automatic_adoption':False})
    (AUDIT/'decisions.md').write_text((AUDIT/'decisions.md').read_text('utf-8')+'\n'
        '- Official public discovery obtained 5 documents / 10 tables; all requiring visual examination remain development/regression.\n'
        '- 7 labels visually audited; 3 retained weak due to incomplete grids/shading. No official independent accuracy claim.\n'
        '- Shading ablation: remove-all-fill failed; preserve thin filled rules and remove only >3pt rectangles selected as an experimental alternative.\n'
        '- Spatial strategy keeps default local-v2 unchanged; new local-v4 remains explicit experimental. Large-table performance gate remains not_met.\n'
        '- API v1 chooses existing valid candidates only, one-shot dispatch, 2 submitted requests per table/revision. No paid provider called.\n'
        '- Packaging new owned E:/DeloitteOCR-ComplexTables-20260919; source old bundle is immutable.\n','utf-8')
    progress=json.loads((AUDIT/'progress.json').read_text('utf-8'))
    for t in progress['tasks']:
        if t['id'].startswith(('B','C')) or t['id'] in ('D01','D02','D04'):
            if t['status']=='pending':t.update(status='running',attempts=t['attempts']+1,next_command='Reconcile final regression, local-selection-03 and new delivery evidence')
    save(AUDIT/'progress.json',progress)
    (AUDIT/'NEXT.md').write_text('# Resume\n\n'
        'Implementation 0.13.0rc1 is complete; final acceptance and packaging ongoing.\n\n'
        '- Full source backend regression: exec session 91340; audit/regression/full-final-01.log.\n'
        '- Final selected local-v4 replay: exec session 80027; build/runs/local-selection-03.\n'
        '- Earlier broad replays complete; preserve historical-replay-01 and historical-replay-final source-freeze directories.\n'
        '- Assets staged successfully at E:/DeloitteOCR-ComplexTables-20260919/bundle. Not finalized or ZIPped yet.\n'
        '- Browser ui-final passed, shut down. 51 frontend tests and production build passed.\n'
        '- Next: inspect regression/replay, finish metrics/seals/docs, overlay fresh delivery-info, finalize/verify bundle, generate and CRC/hash verify new ZIP.\n'
        '- Do not rerun downloads or overwrite historical experiment output. Do not edit production source during full regression unless fixing a failure; repeat affected checks afterward.\n','utf-8')


if __name__=='__main__':main()
