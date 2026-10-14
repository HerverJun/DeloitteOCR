"""Resumable evidence ledger for the complex table programme (no inferred passes)."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = 'complex-tables-20260919-01'
AUDIT = ROOT / 'audit' / RUN
BUILD = ROOT / 'build' / RUN


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    temporary.replace(path)


def initialize():
    plan = ROOT / 'docs/complex-table-agent-tasks-20260919.json'
    identity = {'plan_sha256': sha(plan), 'run': RUN}
    progress = AUDIT / 'progress.json'
    if progress.exists():
        old = json.loads(progress.read_text('utf-8'))
        if old['identity'] != identity:
            raise ValueError('Run identity changed; use a new run')
        return
    for name in ('dataset', 'runs', 'raw', 'derived'):
        (BUILD / name).mkdir(parents=True, exist_ok=True)
    tasks = json.loads(plan.read_text('utf-8'))['tasks']
    save(progress, {'identity': identity, 'started': datetime.now(timezone.utc).isoformat(),
        'tasks': [{**t, 'evidence': [], 'attempts': 0, 'last_error': None,
                   'next_command': None, 'engineering': 'not_measured',
                   'quality': 'not_measured', 'real_service': 'not_measured'} for t in tasks]})
    save(AUDIT / 'gaps.json', [])
    (AUDIT / 'NEXT.md').write_text('# Resume\n\nCurrent: A01 inventory. No jobs started.\n'
        'Next: inspect existing runtimes, freeze contracts, collect public sources.\n'
        'Preserve all pre-existing changes, historical inputs and delivery archives.\n', 'utf-8')
    (AUDIT / 'decisions.md').write_text('# Decisions\n\n'
        '- Run started from dirty 0.12.0rc1 working tree; existing external review changes retained.\n'
        '- Single agent; CPU batches may run in parallel; no GPU process termination.\n'
        '- Research input remains in build; no private projects or credentials in delivery.\n', 'utf-8')


def update(task_id, status, evidence=(), engineering='not_measured', quality='not_measured', real_service='not_measured', next_step=''):
    path = AUDIT / 'progress.json'
    value = json.loads(path.read_text('utf-8'))
    task = next(t for t in value['tasks'] if t['id'] == task_id)
    for item in evidence:
        if not (ROOT / item).exists():
            raise ValueError('Evidence does not exist: ' + item)
    task.update(status=status, evidence=list(evidence), engineering=engineering, quality=quality,
                real_service=real_service, attempts=task['attempts']+1, next_command=next_step)
    save(path, value)
    (AUDIT / 'NEXT.md').write_text(f'# Resume\n\nLast task: {task_id} ({status}).\n\nNext: {next_step}\n\n'
        'Read progress.json and gaps.json before resuming. Do not overwrite old experiments or deliveries.\n', 'utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', action='store_true')
    parser.parse_args()
    initialize()
    print(AUDIT)
