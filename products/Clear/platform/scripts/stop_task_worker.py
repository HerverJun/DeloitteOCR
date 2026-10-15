"""Explicit graceful shutdown of a verified private execution service."""
import argparse
import json
import httpx
from workbench_platform.tasks.database import f_path
from workbench_platform.tasks.store import digest

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--private-config',required=True);parser.add_argument('--port',type=int,default=8890);args=parser.parse_args()
    private=json.loads(f_path(args.private_config).read_text('utf-8-sig'));headers={'Authorization':'Bearer '+private['worker_token']};base=f'http://127.0.0.1:{args.port}'
    status=httpx.get(base+'/internal/status',headers=headers,timeout=5);status.raise_for_status()
    if status.json()['root_identity']!=digest(str(f_path(args.data))):raise ValueError('Execution service belongs to another data directory')
    result=httpx.post(base+'/internal/shutdown',headers=headers,timeout=5);result.raise_for_status();print('Graceful execution service shutdown requested.')
