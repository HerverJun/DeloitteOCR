"""Independent validation of an externally recorded, version-bound human review.

An actor label or model-authored confirmation is never sufficient. The trusted
test operator's actual user answer must bind a persisted decision, its exact
event, a native review receipt, and unchanged old/new immutable snapshots.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def fpath(value):
    path=Path(value).absolute()
    if path.drive.upper()!='F:' or path.resolve().drive.upper()!='F:' or any(
            part.is_symlink() or part.is_junction() for part in (path,*path.parents)):
        raise ValueError('Human review evidence must stay on F: without links')
    return path


def verify_external_review(data,workspace,session,evidence_root):
    root=fpath(evidence_root)
    names=('actual-human-reply.json','review-command.json','native-review-receipt.json',
           'approved-snapshot.json','decision-reply-command.json','decision-reply-receipt.json')
    documents={name:json.loads((root/name).read_text(encoding='utf-8-sig')) for name in names}
    actual,command,receipt,approved,reply,reply_receipt=(documents[name] for name in names)
    call=actual.get('question_call_id','')
    if (actual.get('source')!='human user message in this Codex conversation' or not call.startswith('call_')
            or not actual.get('answer') or not actual.get('approved_fields')
            or set(actual['approved_fields'])-{'subject','number'}):
        raise ValueError('An actual scoped human answer is required')
    expected_request='uSealReview-'+call.replace('call_','call',1)
    if (command.get('request_id')!=expected_request or command.get('assessment')!='confirmed'
            or command.get('expected_revision')!=actual.get('expected_revision')
            or call not in command.get('actor','') or actual.get('snapshot_id') not in command.get('note','')
            or len(command.get('edits',[]))!=1 or command['edits'][0].get('index')!=0
            or any(command['edits'][0].get(key)!=value for key,value in actual['approved_fields'].items())):
        raise ValueError('Native review command differs from the actual answer and result version')
    if (reply_receipt.get('accepted') is not True or reply_receipt.get('request_id')!=reply.get('request_id')
            or reply.get('request_id')!='uSealReply-'+call.replace('call_','call',1)):
        raise ValueError('Actual decision reply receipt is missing')
    data=fpath(data)
    with closing(sqlite3.connect((data/'agent/operations.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        decision=db.execute('SELECT d.*,r.session_id,r.workspace_id FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=?',
                            (reply_receipt['decision_id'],)).fetchone()
        event=db.execute('SELECT * FROM events WHERE session_id=? AND identity=? AND kind=?',
                         (session,reply['request_id'],'message')).fetchone()
    if (decision is None or decision['session_id']!=session or decision['workspace_id']!=workspace
            or decision['reply']!=reply.get('content') or decision['request_id']!=reply['request_id']
            or decision['revision']!=reply.get('revision') or event is None):
        raise ValueError('Human answer is not the persisted reply to this session decision')
    payload=json.loads(event['payload'])
    if (event['run_id']!=decision['run_id'] or payload!={'role':'user','content':reply['content'],'decision_id':decision['id']}):
        raise ValueError('Human decision event does not match the persisted answer')
    with closing(sqlite3.connect((data/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT body FROM manual_review WHERE workspace_id=? AND request_id=?',
                       (workspace,command['request_id'])).fetchone()
        if row is None or json.loads(row[0])!=receipt:
            raise ValueError('Native review receipt was not persisted for this workspace')
        snapshots=[]
        for identifier in (actual['snapshot_id'],approved['snapshot_id']):
            row=db.execute('SELECT body FROM manual_result_snapshot WHERE workspace_id=? AND job_id=? AND snapshot_id=?',
                           (workspace,actual['job_id'],identifier)).fetchone()
            if row is None:
                raise ValueError('A human review snapshot is missing')
            snapshot=json.loads(row[0]); recorded=snapshot.pop('snapshot_hash',None)
            if recorded!=digest(snapshot):
                raise ValueError('Human review immutable snapshot hash changed')
            snapshot['snapshot_hash']=recorded;snapshots.append(snapshot)
    before,after=snapshots
    review=receipt.get('review') or {}
    if (before['snapshot_hash']!=actual.get('snapshot_hash') or after!=approved
            or before['revision']!=actual['expected_revision'] or after['revision']!=before['revision']+1
            or after['review_revision']!=after['revision'] or after['generation']!=before['generation']
            or before['result_hash']!=after['result_hash'] or before['sources']!=after['sources']
            or before['analysis']['result']!=after['analysis']['result']
            or after['analysis']!=receipt or receipt.get('kind')!='seal' or receipt.get('job_id')!=actual['job_id']
            or receipt.get('workspace_id')!=workspace or review.get('request_id')!=command['request_id']
            or review.get('assessment')!='confirmed' or review.get('expected_revision')!=before['revision']
            or review.get('actor')!=command['actor'] or review.get('note')!=command['note']
            or any(review['edits'][0].get(key)!=value for key,value in actual['approved_fields'].items())):
        raise ValueError('Reviewed version does not preserve the original result and actual corrections')
    return {'passed':True,'evidence_root':str(root),'job_id':actual['job_id'],
        'snapshot_id':after['snapshot_id'],'snapshot_hash':after['snapshot_hash'],'revision':after['revision'],
        'original_snapshot_id':before['snapshot_id'],'original_snapshot_hash':before['snapshot_hash'],
        'original_revision':before['revision'],'raw_rows':before['analysis']['result']['seals'],
        'approved_fields':actual['approved_fields'],'review_request_id':command['request_id'],
        'allowed_user_event':{'identity':reply['request_id'],'decision_id':decision['id'],
                              'content':reply['content'],'run_id':decision['run_id'],'session_id':session},
        'evidence_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}}


def allowed_review_event(event,proof):
    if not proof or proof.get('passed') is not True:
        return False
    allowed=proof.get('allowed_user_event') or {}
    payload=event.get('payload') or {}
    return (event.get('identity')==allowed.get('identity') and event.get('run_id')==allowed.get('run_id')
            and event.get('session_id')==allowed.get('session_id') and payload.get('role')=='user'
            and payload.get('decision_id')==allowed.get('decision_id') and payload.get('content')==allowed.get('content'))


def delivery_checks(proof,files,delivery,jobs):
    def check(name,passed,detail):return {'name':name,'passed':bool(passed),'detail':detail}
    approved=proof.get('approved_fields') or {}
    text='\n'.join(file.get('text','') for file in files if Path(file['name']).suffix.casefold()!='.log')
    refs=delivery.get('result_refs',[])
    bound=any(ref.get('application')=='multimodal' and ref.get('kind')=='result'
              and ref.get('object_id')==proof.get('snapshot_id') and ref.get('sha256')==proof.get('snapshot_hash') for ref in refs)
    verified=any(job.get('human_receipt_verified') is True for job in jobs if job.get('job_id')==proof.get('job_id'))
    raw=proof.get('raw_rows') or []
    return [check('external_human_review',proof.get('passed') is True,proof.get('review_request_id')),
            check('formal_reviewed_version',bound and verified,{'snapshot_id':proof.get('snapshot_id'),'revision':proof.get('revision')}),
            check('formal_corrected_fields',bool(approved) and all(str(value) in text for value in approved.values()),list(approved)),
            check('formal_raw_ocr_preserved',bool(raw) and all(row.get('text') and row['text'] in text for row in raw),len(raw))]
