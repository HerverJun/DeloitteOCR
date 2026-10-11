"""Reconcile actual native detail geometry with a fresh source-only replay."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import shutil
import sqlite3
import sys

from common import digest,fpath,fingerprint,frozen_json,read_json
from controlled_recovery import bound,require_environment
from native_diagnostics import diagnostic_source,STATIC_DIAGNOSTIC_FIELDS,STATIC_ITEM_FIELDS


def main():
    require_environment()
    run,source,independent,capsule,output=map(fpath,sys.argv[1:])
    if output.exists():raise ValueError('New native geometry review identity required')
    verified=read_json(independent/'review.json')
    observations_path=independent/'verified-observations.json'
    observations=read_json(observations_path)
    trial=read_json(source/'trial-source-binding.json')
    policy=read_json(capsule/'policy.json')
    if (verified['status']!='passed' or verified['verified_observations_sha256']!=digest(observations_path)
            or verified['run_result_sha256']!=digest(run/'result.json')
            or any(trial['sources']['ocr']['files'].get(name)!=sha for name,sha in policy['files'].items())):
        raise ValueError('Actual source/readback/frozen geometry binding differs')
    output.mkdir()
    db_files={p.name:digest(p) for p in independent.glob('workbench.sqlite3*') if p.is_file()}
    for name,sha in db_files.items():
        shutil.copyfile(independent/name,output/name)
        if digest(output/name)!=sha:raise ValueError('Native ledger copy differs')
    with closing(sqlite3.connect(output/'workbench.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        items={row['input_version_id']:dict(row) for row in db.execute('SELECT * FROM screen_items')}
    decisions=[];details=[];local_issues=0
    with diagnostic_source(source/'ocr',trial['sources']['ocr']['files']) as module:
        for record in observations['records']:
            path=run/'native-receipts'/(record['id']+'.json')
            actual=read_json(path)
            stored=items[record['input_version_id']]
            if (actual['result_id']!=record['result_id'] or actual['result_revision']!=record['result_revision']
                    or actual['status']!='needs_review' or actual.get('human_confirmed') is not False
                    or stored['batch_id']!=read_json(run/'result.json')['batch_id']):
                raise ValueError('Current result/review identity differs')
            raw={**deepcopy(record['candidate']['original']),**deepcopy(record['candidate']['edited'])}
            raw['screen']={**deepcopy(record['candidate']['original'].get('screen') or {}),
                **deepcopy(record['candidate']['edited'].get('screen') or {})}
            prep=__import__('json').loads(stored['preparation']);evidence=__import__('json').loads(stored['evidence'])
            captured=actual['diagnostics']
            replay=module.build_screen_diagnostics(raw,prep,evidence,
                remaining_seconds=captured['budget']['remaining_seconds'])
            if (any(captured.get(k)!=replay.get(k) for k in STATIC_DIAGNOSTIC_FIELDS)
                    or captured.get('grid_localization')!=replay['grid_localization']
                    or len(captured['items'])!=len(replay['items'])
                    or any(a.get(k)!=b.get(k) for a,b in zip(captured['items'],replay['items']) for k in STATIC_ITEM_FIELDS)):
                raise ValueError('Captured native geometry differs from independently replayed source pixels')
            proof=replay['grid_localization'];direct=module._observe_grid_cells(raw,prep,evidence)
            localized=[part for part in replay['items'] if part['location']['level']=='cell']
            local_issues+=len(localized)
            decisions.append({'id':record['id'],'before_cells':direct['cell_count'],'after_cells':proof['cell_count'],
                'verified':bool(proof['cell_count']),'source_sha256':record['image_sha256'],
                'input_version_id':record['input_version_id'],'result_id':record['result_id'],
                'result_revision':record['result_revision'],'candidate_table_sha256':fingerprint(raw['tables']),
                'direct':direct,'proof':proof})
            details.append({'id':record['id'],'detail':bound(path),'grid_cells':proof['cell_count'],
                'cell_issue_items':len(localized),'all_reviewed':record['review_image'],
                'captured_geometry_exactly_reproduced':True})
    if {p.name:digest(p) for p in independent.glob('workbench.sqlite3*') if p.is_file()}!=db_files:
        raise ValueError('Prior independent evidence changed')
    frozen_json(output/'decisions.json',{'decisions':decisions})
    report={'format':'actual-native-source-rectified-grid-review-v1','policy':bound(capsule/'policy.json'),
        'observations':bound(observations_path),'decisions':bound(output/'decisions.json'),
        'actual_native_result':bound(run/'result.json'),'independent_readback':bound(independent/'review.json'),
        'scope_inputs':len(decisions),'before_images':sum(bool(r['before_cells']) for r in decisions),
        'after_images':sum(r['verified'] for r in decisions),'before_cells':sum(r['before_cells'] for r in decisions),
        'after_cells':sum(r['after_cells'] for r in decisions),'localized_issue_items':local_issues,
        'details':details,'all_captured_geometry_exactly_reproduced':True,
        'truth_read':False,'original_database_opened':False,'values_changed':False,'review_changed':False,
        'actual_model_evidence_reviewed':True,'holdout_read':False,'final_agent_qualification':False,'adoption_qualified':False}
    frozen_json(output/'result.json',report)
    print({k:report[k] for k in ('scope_inputs','before_images','after_images','before_cells','after_cells','localized_issue_items')})


if __name__=='__main__':main()
