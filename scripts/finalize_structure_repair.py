"""Close the exploration with measured evidence and explicit conditional gaps."""
from collections import Counter
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from structure_repair_run import ROOT,AUDIT,BUILD,RUN,read,save,sha,update


def evidence():
    rows=read(BUILD/'baseline/rows.json')
    ablation=read(AUDIT/'local-ablations-verified.json')
    cases=[];table_rows=[]
    for row in rows:
        for p in row['proposals']:
            reasons=list(dict.fromkeys(c['kind'] for c in p['conflicts']))
            cases.append({'sample':row['id'],'proposal':p['id'],'tables':p['table_indices'],
                'candidate_set':p['candidate_set_id'],'kind':p['kind'],'primary':reasons[0] if reasons else None,
                'secondary':reasons[1:],'affected_cells':p.get('impact',{}).get('cells',[]),
                'classification':'correct rejection or unresolved evidence; not proof candidate topology is wrong',
                'conflicts':p['conflicts']})
        for ti,t in enumerate(row['adopted']['tables']):
            relevant=[p for p in row['proposals'] if ti in p['table_indices']]
            table_rows.append({'sample':row['id'],'table':ti,'cells':len(t['cells']),
                'adopted_with_source_ids':sum(bool(c.get('structure_source',{}).get('token_ids')) for c in t['cells']),
                'adopted_with_full_cell_evidence':sum(c.get('structure_source',{}).get('range_semantics')=='full_cell' for c in t['cells']),
                'proposals':len(relevant),'applicable':sum(p['can_apply'] for p in relevant),
                'rejection_categories':sorted({c['kind'] for p in relevant for c in p['conflicts']})})
    save(AUDIT/'rejection-cases.json',{'candidate_denominator':len(cases),'cases':cases,'table_denominator':len(table_rows),'tables':table_rows})
    candidates=[c for r in rows for c in r['candidates']]
    matched=[c for c in candidates if c['adopted_table_indices']]
    grid=[c for c in matched if c['grid_error'] is None]
    original=read(AUDIT/'baseline-funnel.json')
    legacy_pass=[p for r in rows for p in r['proposals'] if not any(c['kind'] in ('current_value_unmapped','manual_value_unmapped','table_identity_ambiguous') for c in p['conflicts'])]
    local=[p for p in legacy_pass if p['kind']!='replace_table']
    save(AUDIT/'gate-funnel.json',{'inputs':120,'adopted_tables':121,
        'candidate_funnel':{'candidate_tables':len(candidates),'identifiable':len(matched),'identifiable_and_grid_valid':len(grid)},
        'proposal_funnel':{'proposals':original['proposals'],'legacy_value_check_only':len(legacy_pass),
            'legacy_local_value_check_only':len(local),'all_product_checks':0,'budget_qualified':0,'model_calls':0,'adoptable':0},
        'strict_content_funnel':ablation['variants'],'tables':table_rows,
        'reason_accounting':'one primary per proposal; deduplicated secondary kinds are not additional failed proposals',
        'literal_pass_note':'legacy_value_check_only is not strict identity conservation and is not a sendability claim',
        'legacy_primary_rejections':original['primary_rejections']})
    save(AUDIT/'input-integrity.json',{'passed':all(r['input_sha256_before']==r['input_sha256_after'] for r in rows),
        'imports':'only temporary image copies; original add_image source-consumption audited',
        'inputs':[{'sample':r['id'],'before':r['input_sha256_before'],'after':r['input_sha256_after']} for r in rows],
        'plans_unchanged':sha(ROOT/'docs/structure-repair-exploration-tasks-20260919.json')==read(AUDIT/'run-identity.json')['plan_sha256']})
    save(AUDIT/'content-contract.json',{'contract':'docs/structure-repair-exploration-plan-20260919.md#不可突破的保全契约',
        'implementation':'src/ocr_workbench/structure_repair.py','test':'tests/test_structure_repair.py',
        'namespace':'one adopted table/result/revision/image fingerprint for every candidate; IDs do not imply coordinates',
        'supported':'one-to-one source identity, full-cell geometry/manual binding, exact unchanged-slot header metadata',
        'unsupported':'splits and merges needing adopted fragment boundaries; no character interpolation, new values, unknown destinations or guessed duplicates',
        'scope':'opt-in research contract, not a replacement of the legacy content-enrichment workflow',
        'legacy_isolation':'unsafe split/merge normalization fallbacks removed; prior-version proposals cannot be accepted; native export literal comparison tightened'})
    save(AUDIT/'patch-contract.json',{'implementation':'src/ocr_workbench/structure_repair.py',
        'max_affected_cells':8,'max_patches':6,'composition':'not implemented; never automatically combined; full table revalidated at patch formation and application',
        'revision_and_image_bound':True,'moves':'all content identity source/destination slots, including displaced later rows',
        'default_enabled':False,'actual_product_adoptions':0})
    qualification=read(AUDIT/'dataset-qualification.json')
    save(AUDIT/'boundary-evidence.json',{'native_tables':len(qualification['native']),
        'multi_table_pages':sorted({r['image'] for r in qualification['native'] if r['page_tables']>1}),
        'native_source':'PDF vector lines and native words extracted before any reference scoring; no label-derived production boundary',
        'visual_audit':'contact.jpg inspected: unruled statement, two-table financial page, multilevel header, captions/footnotes; source fields not promoted to truth labels',
        'real_images':'s002-s004 show physical historic documents; precise scan vs camera modality unresolved; s101-s104 may be native renders and do not count as physical scans',
        'quality_boundary_regression':'unmeasured without paired adopted baselines and independent labels',
        'mechanisms':'tests reject title/footnote/new blank and crossing independent table region; accept contained multilevel header metadata'})
    save(AUDIT/'model-eligibility.json',{'product_inputs':120,'adopted_tables':121,'strict_eligible_tables':0,
        'requests':0,'reason':'T09 continuation gate not met; no product-valid content-preserving patches',
        'keep':'current revision unchanged for every table','abstain':'no model was invoked; no model abstentions inferred',
        'compression':'not used; no projected diagnostic evidence sent','luna_gain':'not_measured'})
    save(AUDIT/'model-lock.json',{'requested_model':'gpt-5.6-luna','reasoning_effort_requested':'medium',
        'reasoning_effort_observed':None,'context':'would be independent per table','seed':20260919,
        'prompt_status':'no request constructed because no eligible input','request_hashes':[],
        'model_simulation_calls':0,'real_service_calls':0,'automatic_retries':0,
        'endpoint_identity':None,'usage':None,'cost':None,'endpoint_latency':None})
    save(AUDIT/'confirmation-results.json',{'status':'not_measured','documents':0,'tables':0,'cells':0,
        'target':{'documents':10,'tables':20,'cells':1000},
        'reason':'T09 stopped: zero safe patches; new PDF years share exposed template; image samples lack adopted revisions and independent text identities',
        'no_claim':'historical 11,278 target cells do not substitute for independent confirmation or formal acceptance'})
    save(AUDIT/'integration-decision.json',{'new_repair_algorithm_enabled':False,'new_editor':False,
        'decision':'retain research prototype only; integrate narrowly demonstrated safety fixes',
        'production_changes':['reject unproved normalized split/merge','invalidate stale policy proposals','require exact literal when reusing native PDF units'],
        'legacy_workflow':'source-enrichment suggestions can contain additional OCR text; they are excluded from the pure structure-repair contract and all new repair/model benefit claims',
        'quality_claim':None})
    save(AUDIT/'performance.json',{'historical_product_compute_seconds':original['seconds'],
        'baseline_wall_seconds':None,'baseline_wall_note':'initial tool rewrote growing 500MB+ checkpoint; compute time excludes serialization, so no end-to-end claim',
        'checkpoint_fix':'subsequent run writes each case once, then one aggregate; old baseline retained',
        'hardened_replay':read(AUDIT/'hardened-full-funnel.json'),
        'binding_variants':[{k:v for k,v in r.items() if k in ('method','seconds','max_binding_ms')} for r in ablation['variants']],
        'timing_limitations':'CPU checks ran concurrently; not controlled benchmark; worst binding exceeds 200ms and no performance pass claimed',
        'memory_peak_bytes':None,'human_operation_time':None,'model_usage':None,
        'patch_review_cost':{'triggered':0,'affected_contents':0,'human_actions_measured':0,'max_damage_observed':0}})
    gaps=[
        {'id':'adopted-evidence','attempts':['exclusive source identity','full-cell geometry/manual binding','constrained unchanged-slot adjacency'],
         'finding':'historical adopted cells lack token identity and full-cell evidence; adjacent metadata candidates still fail product unassigned-token/grid gates',
         'next_condition':'real adopted revisions with verified source tokens/manual boxes or a separately documented new-recognition experiment'},
        {'id':'fragments','attempts':['saved whitespace split counterexample','duplicate identity merge counterexample'],
         'finding':'whole-cell strings have no proven internal fragment boundaries; merge/split support deliberately withheld',
         'next_condition':'persist verifiable adopted fragments and separators through editing, adoption and export'},
        {'id':'new-independent-data','attempts':['two official Chinese budget PDFs: 13 extracted tables','ICDAR repository and bounded samples: 7 images','WTW official repository/download landing','treasury old URL returned 404; switched official source'],
         'finding':'new-year budget PDFs share exposed template; images lack adopted OCR/content bindings; physical modality or independent text labels missing',
         'next_condition':'10 new independent document/template groups, 20 tables, 1000 independently identified targets; physical scan/photo provenance and labels'},
        {'id':'quality-and-model','attempts':['120-input fixed-pool ablations','hard product eligibility check'],
         'finding':'0 safe repairs; continuation gate 5 repairs/3 documents not met; Luna and confirmation conditional tasks not activated',
         'next_condition':'frozen local algorithm meeting continuation gate with zero conservation violations'},
        {'id':'performance','attempts':['bounded variants measured','hardened replay timed including checkpoint IO'],
         'finding':'binding max >200ms under CPU concurrency; peak memory and human time unmeasured; historical candidate timeout changed on regeneration',
         'next_condition':'controlled source-first timing and memory benchmark on qualifying real inputs'},
        {'id':'legacy-contract-scope','attempts':['existing workflow smoke','strict research eligibility exclusion'],
         'finding':'legacy row-insertion/source-enrichment is not a pure adopted-content-only patch; no claim of global legacy compliance with the stricter research contract',
         'next_condition':'separate explicit content-enrichment operation from pure structure adoption before generalizing the new contract'}]
    save(AUDIT/'gaps.json',gaps)
    hypotheses={'H0':{'status':'supported','scope':'three-input HTTP parity and all-input zero-trigger replay; stored predictions only'},
        'H1':{'status':'inconclusive','finding':'mechanism works with synthetic independent identity; historical evidence insufficient for real safe repair'},
        'H2':{'status':'inconclusive','finding':'zero product-eligible local patches; no quality comparison possible'},
        'H3':{'status':'inconclusive','finding':'boundary mechanism tests pass, but paired real quality is unmeasured'},
        'H4':{'status':'inconclusive','finding':'zero Luna calls because zero eligible inputs'}}
    save(AUDIT/'hypothesis-outcomes.json',hypotheses)
    save(AUDIT/'hypothesis-decisions.json',{'continue':False,'real_correct_repairs':0,'documents_with_correct_repairs':0,
        'criteria':{'correct_repairs':5,'documents':3,'conservation_violations':0},'new_variants_allowed':False,
        'interpretation':'stop this evidence-limited branch, not evidence that every possible local repair or Luna is ineffective'})
    save(AUDIT/'invariant-cases.json',{'test_file':'tests/test_structure_repair.py','cases':19,
        'baseline_failure':'baseline-invariants-whitespace.json','baseline_rejected_probe':'baseline-invariants.json',
        'negative_probe_correction':'first full-width A probe was correctly rejected; actual flaw uses whitespace, not compatibility folding',
        'passed_run':'regression/content-contract-final.json'})
    save(AUDIT/'environment-errors.json',{'resolved_setup_errors':[
        'embedded service Python excludes scripts directory: added script-local sys.path before imports',
        'fitz absent in system/PDF runtime: used pinned pdfplumber path for native inspection; isolated existing scoring runtime for GriTS',
        'initial strengthened correspondence self-check changed reason when source and geometry agreed: kept source precedence, reran tests and all ablations with -verified name'],
        'not_product_failures':True})
    save(AUDIT/'regression/frontend.json',{'passed':True,'tests':51,'files':11,
        'command':'npm test -- --reporter=json --outputFile=../audit/structure-repair-20260919-01/regression/frontend.json',
        'receipt_origin':'observed terminal exit 0; npm did not forward reporter arguments, this summary records actual stdout',
        'not_machine_test_runner_json':True})
    save(AUDIT/'regression/build.json',{'passed':True,'command':'npm.cmd run build','checks':['tsc -b','vite build'],
        'observed':'3741 modules, 13.97 seconds Vite build, exit 0','warnings':['JS chunk >500 kB'],
        'artifacts':{str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'frontend/dist').rglob('*') if p.is_file()}})
    save(AUDIT/'regression/browser.json',{'receipt':read(BUILD/'ui-final/receipt.json'),'shutdown':read(BUILD/'ui-final/shutdown.json'),
        'transport':'external Playwright Edge headless --disable-gpu --disable-gpu-compositing',
        'screenshots':['build/'+RUN+'/ui-final/01-structure-recommendation.png','build/'+RUN+'/ui-final/02-financial-checks.png'],
        'visual_inspection':'recommendation screenshot inspected; readable page/table/confirmation; no app errors',
        'scope':'legacy queue/adoption/undo smoke on synthetic fixture, not strict repair quality or real API'})
    save(AUDIT/'regression/grits.json',{'passed':True,'tests':6,'skipped':0,
        'command':'service Python with src,tests,scripts,build/complex-tables-20260919-01/runtime/scoring first; unittest test_structure_evaluation',
        'observed_seconds':.320,'source_first':True})
    save(AUDIT/'regression/content-contract-final.json',{'passed':True,'tests':19,'seconds':.009,
        'command':'service Python with src,tests first; unittest test_structure_repair',
        'test_sha256':sha(ROOT/'tests/test_structure_repair.py'),'source_sha256':sha(ROOT/'src/ocr_workbench/structure_repair.py')})
    save(AUDIT/'regression/export.json',{'covered_by':['backend-final','test_native_export_does_not_restore_a_normalized_manual_literal'],
        'checks':['Excel/JSON export','PDF positioned text and fallback','structure source index','accept/undo/redo','manual binding preservation'],
        'new_prototype_export':'not integrated; no new coordinate semantics; fragment export not claimed'})
    sources=[]
    for p in (BUILD/'sources').glob('*/receipt.json'):sources.append(read(p))
    (AUDIT/'sources.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in sources),'utf-8')
    (AUDIT/'exposure.jsonl').write_text(''.join(json.dumps({'id':r['id'],'split':r.get('split','development'),
        'reason':'inspected source/layout; never independent confirmation'},ensure_ascii=False)+'\n' for r in qualification['native']+qualification['real_image_samples']),'utf-8')
    files=[ROOT/'src/ocr_workbench'/n for n in ['structure_repair.py','structure_diagnostics.py','structure_store.py','structure_export.py']]
    save(AUDIT/'candidate-freeze.json',{'frozen_utc':datetime.now(timezone.utc).isoformat(),'enabled':False,
        'source_files':{str(p.relative_to(ROOT)):sha(p) for p in files},'input_pool_sha256':sha(BUILD/'baseline/rows.json'),
        'protocol_sha256':sha(AUDIT/'protocol.json'),'variants':3,'final_ablation':'local-ablations-verified.json'})
    (AUDIT/'decisions.md').write_text('''# Decisions

- T01 used copied inputs and product functions. Three HTTP checks agree. Historical provider routing remains explicit.
- Protocol frozen before effect scoring. All historical labels are offline regression diagnostics only.
- First full-width-letter probe did not reproduce a defect. Whitespace-only split did; both receipts retained.
- Removed legacy normalization-based split/merge bypasses; no inferred fragment boundaries. Old-version proposals fail closed.
- Prototype explores exactly three cumulative correspondence paths. Self-check correction is a software bug fix; reruns saved with -verified suffix, original results retained.
- Zero eligible repairs across all 121 adopted tables. Stop branch under the frozen 5-repair/3-document gate.
- No Luna invocation, projected input, independent-confirmation scoring or new repair UI integration. These conditional tasks close with explicit gaps.
- Legacy content-enrichment remains outside the stricter pure-repair experiment; compatibility tests are not conservation-quality evidence.
- Regenerating candidates changed one timeout outcome. Same-input ablations exclusively use the frozen baseline pool. Rerun counts are not improvement evidence.
- Checkpoint serialization was unnecessarily quadratic in audit size. New runs write per-case checkpoints; no old evidence removed.
- No release-version increase or offline ZIP regeneration; deliver source safeguards, experimental module and research evidence.
''','utf-8')


def finish():
    backend=read(AUDIT/'regression/backend-final.json')
    if not backend['passed']:raise ValueError('backend regression not passed')
    mappings={
        'T01':['run-identity.json','input-integrity.json','replay-parity.json','reprepare-drift.json'],
        'T02':['gate-funnel.json','rejection-cases.json'],
        'T03':['protocol.json','split-lock.json','sources.jsonl','dataset-qualification.json','exposure.jsonl'],
        'T04':['content-contract.json','invariant-cases.json','baseline-invariants-whitespace.json','regression/content-contract-final.json'],
        'T05':['repairability.json','grouped-net-benefit.json'],
        'T06':['local-ablations-verified.json','content-contract.json'],
        'T07':['patch-contract.json','regression/content-contract-final.json'],
        'T08':['boundary-evidence.json','regression/content-contract-final.json'],
        'T09':['local-ablations-verified.json','hypothesis-decisions.json','candidate-freeze.json'],
        'T10':['model-eligibility.json'], 'T11':['model-lock.json','model-eligibility.json'],
        'T12':['confirmation-results.json'], 'T13':['integration-decision.json'],
        'T14':['regression/backend-final.json','regression/frontend.json','regression/build.json','regression/browser.json','regression/export.json','performance.json','regression/grits.json'],
        'T15':['final-report.md','artifact-index.json','gaps.json','hypothesis-outcomes.json'],
        'T16':['delivery-receipt.json','source-change-lock.json']}
    for tid,paths in mappings.items():
        status='done' if tid in ('T01','T02','T04','T09','T15','T16') else 'done_with_gaps'
        update([tid],status,['audit/'+RUN+'/'+p for p in paths],engineering='passed_scoped' if tid not in ('T10','T11','T12') else 'not_applicable',
               quality='continuation_gate_not_met',model_simulation='not_measured',real_service='not_measured')
    state=read(AUDIT/'progress.json')
    for task in state['tasks']:
        task['attempts']=[{'evidence':p} for p in task['evidence']]
        task['next_command']=None
    state['outcomes']={'engineering':'scoped safety and regression passed','quality':'continuation_gate_not_met',
        'formal_quality':'not_met','model_simulation':'not_measured_zero_eligible','real_service':'not_measured'}
    save(AUDIT/'progress.json',state)
    changed=[];diff=[]
    for old in (BUILD/'baseline-source').rglob('*.py'):
        relative=old.relative_to(BUILD/'baseline-source');current=ROOT/relative
        if current.exists() and sha(old)!=sha(current):
            changed.append({'path':relative.as_posix(),'before':sha(old),'after':sha(current)})
            diff.extend(difflib.unified_diff(old.read_text('utf-8').splitlines(True),current.read_text('utf-8').splitlines(True),fromfile='baseline/'+relative.as_posix(),tofile=relative.as_posix()))
    new=[ROOT/'src/ocr_workbench/structure_repair.py',ROOT/'tests/test_structure_repair.py',*sorted((ROOT/'scripts').glob('*structure_repair*.py'))]
    save(AUDIT/'source-change-lock.json',{'modified_from_preexisting_dirty_tree':changed,
        'new':{str(p.relative_to(ROOT)):sha(p) for p in new},'preexisting_modifications_retained':True})
    (AUDIT/'task-changes.patch').write_text(''.join(diff),'utf-8')
    save(AUDIT/'delivery-receipt.json',{'scope':'source research and narrow safety fixes, no release package',
        'report':str(AUDIT/'final-report.md'),'source_change_lock':str(AUDIT/'source-change-lock.json'),
        'new_algorithm_enabled':False,'new_version':None,'new_offline_bundle':None,
        'old_zip':'E:/DeloitteOCR-0.13.0rc1-ComplexTables-20260919.zip','old_zip_action':'not modified',
        'engineering':'scoped regression passed','quality':'not_met','luna_calls':0,'real_service':'not_measured'})
    (AUDIT/'NEXT.md').write_text('''# Exploration closed

All T01-T16 have a measured result or conditional done_with_gaps disposition in progress.json.
No running jobs. External browser, temporary HTTP server and queues have closed.
Read final-report.md and gaps.json before further work. Do not restart the baseline or overwrite this run.
Next experiment requires new adopted-content geometry/fragment evidence and independently grouped documents.
The prototype is not imported by product routing. Existing source-enrichment UI is not a pure-content repair claim.
Use a new structure-repair run name for new research; old source snapshots, inputs and ZIP remain intact.
''','utf-8')
    indexed=[]
    paths=[*AUDIT.rglob('*'),*new,*(BUILD/'sources').rglob('*'),*(BUILD/'development-pages').glob('*'),
        BUILD/'baseline/rows.json',BUILD/'hardened-full/rows.json',*BUILD.glob('*ablation*.json'),BUILD/'oracle-diagnostic-cases.json',BUILD/'paired-events.json']
    for p in sorted(set(paths)):
        if p.is_file() and p.name!='artifact-index.json':indexed.append({'path':str(p.relative_to(ROOT)),'sha256':sha(p),'bytes':p.stat().st_size})
    save(AUDIT/'artifact-index.json',{'files':indexed,'index_self_excluded':True,'original_manifest':'build/tableformer-next-20260915/dataset/inputs/test.json',
        'original_input_verification':'input-integrity.json','raw_labels':'build/tableformer-next-20260915/dataset/sealed/test.annotations.json',
        'labels_sha256':sha(ROOT/'build/tableformer-next-20260915/dataset/sealed/test.annotations.json')})
    for task in read(AUDIT/'progress.json')['tasks']:
        for path in task['evidence']:
            if not (ROOT/path).is_file():raise ValueError('missing task evidence '+path)
    print('Closed all 16 tasks; evidence files:',len(indexed))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['evidence','finish']);a=p.parse_args()
    evidence() if a.phase=='evidence' else finish()
