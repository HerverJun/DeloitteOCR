"""Audit archived real operation receipts through the production CPU reader.

Only an independent copied store receives new read operations. No controller,
native model, graph, original receipt rewrite or broader task qualification.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from common import digest, fpath, frozen_json, read_json
from s07_agent_native_followup import platform_binding
from s07_native_read import cold_copy, inventory


async def execute(args):
    source, original, root = map(fpath, (args.platform_source, args.original_run, args.output_root))
    if root.exists():
        raise ValueError('Receipt audit needs a new F: evidence root')
    root.mkdir(parents=True); temp = root/'temp'; temp.mkdir()
    for name in ('TEMP','TMP','TMPDIR','APPDATA','LOCALAPPDATA','XDG_CACHE_HOME'):
        os.environ[name] = str(temp)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    sys.path[:0] = [str(source), str(source/'src')]
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.store import digest as value_digest
    from workbench_platform.store import WorkspaceStore
    before, bound = inventory(original), platform_binding(source)
    clone = root/'agent'; clone.mkdir()
    cold_copy(original/'platform/agent/operations.sqlite3', clone/'operations.sqlite3')
    cold_copy(original/'platform/workspaces.sqlite3', root/'workspaces.sqlite3')
    services = SimpleNamespace(workspace_store=WorkspaceStore(root/'workspaces.sqlite3'))
    runtime = AgentRuntime(clone, services); services.agent = runtime
    original_run = runtime.store.run(args.run_id)
    run, _ = runtime.store.start_run(original_run['session_id'], 'CPU audit of archived actual tool receipts',
        'cpuReceiptReadbackAudit', original_run['connection_revision'])
    state = {key: run[key] for key in ('workspace_id','generation')}
    state.update(run_id=run['id'], session_id=run['session_id'], step=0)
    observations = []; counter = 0
    try:
        for operation in read_json(original/'actual-agent-operations.json'):
            receipt = operation['receipt']
            if receipt is None:
                continue
            chunks = []; offset = 0; proof = None
            while True:
                counter += 1
                result = await runtime.registry.dispatch({'call_id':'cpuReceiptPage'+str(counter),
                    'tool':'session_read_history','arguments':{'call_id':operation['call_id'],
                    'run_id':args.run_id,'offset':offset,'chars':701}}, runtime.context(state))
                if result['status'] != 'success':
                    raise ValueError('Actual archived receipt could not be read: '+operation['call_id'])
                data = result['data']
                if data['result_sha256'] != value_digest(receipt) or data['id'] != operation['id']:
                    raise ValueError('Readback receipt hash or actual operation identity differs')
                if proof is not None and proof != data['content_sha256']:
                    raise ValueError('Receipt content identity changed across pages')
                proof = data['content_sha256']; chunks.append(data['content'])
                if data['next_offset'] is None:
                    break
                offset = data['next_offset']
            if json.loads(''.join(chunks)) != {'arguments':operation['arguments'],'result':receipt}:
                raise ValueError('Paged original arguments/result were changed')
            observations.append({'operation_id':operation['id'],'call_id':operation['call_id'],
                'tool':operation['tool'],'result_sha256':value_digest(receipt),
                'content_sha256':proof,'pages':len(chunks),'exact_reassembled_receipt':True})
        if runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?',(run['id'],)):
            raise ValueError('Receipt audit unexpectedly invoked a controller')
        runtime.store.status(run['id'],run['generation'],'succeeded')
    finally:
        await runtime.close()
    if inventory(original) != before or platform_binding(source) != bound:
        raise ValueError('Original actual run or fixed production source changed')
    report = {'format':'actual-operation-receipt-cpu-readback-audit-v1','status':'pass',
        'original_run':str(original),'original_run_id':args.run_id,'original_run_unchanged':True,
        'source_files':bound,'actual_original_receipts':observations,'original_receipt_count':len(observations),
        'readback_operations':counter,'controller_calls':0,'gpu_calls':0,'native_model_calls':0,
        'real_agent_chain_verified':False,'helper_sha256':digest(__file__),
        'scope':'Production call_id reader on an isolated actual store copy; all original arguments and receipts exactly restored by pages and SHA; not a new Agent task'}
    frozen_json(root/'readback-audit.json', report)
    print('actual_receipt_readback_verified', len(observations), 'pages',counter)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('platform-source','original-run','output-root','run-id'):
        parser.add_argument('--'+flag,required=True)
    parser.add_argument('--agent-layer',default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    asyncio.run(execute(parser.parse_args()))
