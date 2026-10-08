"""Real registered Agent/native CPU classification on a new F: workspace copy.

Existing published model and historical test inputs only. No training, label
mutation, auto acceptance, provider stub or precomputed tool reply is used.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from common import digest, fingerprint, fpath, frozen_bytes, frozen_json, read_json
from s07_agent_native_followup import DISCOVERY_TOOLS, platform_binding
from s07_classification import readonly_database, source_files
from s07_native_followup import services_for_files
from s07_native_read import cold_copy, inventory

TOOLS = frozenset({'assets_create_set', 'classification_prepare', 'classification_infer',
    'classification_read_results', 'classification_export', 'audit_bind_evidence', 'audit_read_evidence'})


def native_http(base, route):
    port = int(base.rsplit(':', 1)[1])
    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as connection:
        connection.request('GET', route, headers={'Accept': 'application/json', 'X-Platform-Request': '1'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Owned classification HTTP request failed: ' + str(response.status))
        return json.loads(response.read(8 * 1024 * 1024))


def annotations(database):
    with closing(readonly_database(database)) as db:
        return [dict(row) for row in db.execute('SELECT * FROM annotations ORDER BY id')]


def prepare(args):
    root = fpath(args.output_root)
    if root.exists():
        raise ValueError('Real classification needs a new immutable F: evidence root')
    root.mkdir(parents=True)
    temp = root / 'temp'; temp.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR'):
        os.environ[name] = str(temp)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    p = fpath(args.prepared); qualified = read_json(p)
    if qualified['format'] != 'specialized-published-classification-prepared-v1':
        raise ValueError('Existing qualified published-model input is required')
    original = fpath(qualified['workspace_source'])
    before = inventory(original)
    for path, sha in qualified['qualified_readonly_files'].items():
        if digest(path) != sha:
            raise ValueError('Previously qualified model/input source changed')
    source = fpath(qualified['source_root'])
    if source_files(source) != qualified['source_files']:
        raise ValueError('Qualified classification source changed')
    platform = fpath(args.platform_source)
    sys.path[:0] = [str(platform), str(platform / 'src')]
    native = root / 'native/classification'
    shutil.copytree(original, native)
    if inventory(native) != before:
        raise ValueError('Owned native workspace byte copy differs')
    cold_copy(original / 'platform.sqlite3', native / 'platform.sqlite3')
    with closing(sqlite3.connect(native / 'platform.sqlite3')) as db:
        pending = db.execute("SELECT id,kind,state FROM jobs WHERE state NOT IN ('succeeded','completed_with_errors','failed','cancelled')").fetchall()
        if pending:
            raise ValueError('Historical native copy has nonterminal jobs; no worker may resume them')
        relocated = []
        for table, column in (('assets', 'path'), ('models', 'package_path'), ('datasets', 'manifest_path')):
            for identity, path in db.execute('SELECT id,"' + column + '" FROM "' + table + '"').fetchall():
                old = fpath(path)
                if not old.is_relative_to(original):
                    raise ValueError('Registered native file escaped qualified workspace')
                new = fpath(native / old.relative_to(original))
                identity_sha = fingerprint(inventory(old)) if old.is_dir() else digest(old)
                copied_sha = fingerprint(inventory(new)) if new.is_dir() else digest(new)
                if identity_sha != copied_sha:
                    raise ValueError('Relocated registered file differs')
                db.execute('UPDATE "' + table + '" SET "' + column + '"=? WHERE id=?', (str(new), identity))
                relocated.append({'table': table, 'id': identity, 'field': column,
                    'source': str(old), 'owned_copy': str(new), 'sha256': copied_sha})
        db.commit()
    if inventory(original) != before or annotations(native / 'platform.sqlite3') != annotations(original / 'platform.sqlite3'):
        raise ValueError('Native copy preparation changed source or original annotations')
    inputs = read_json(qualified['input_manifest'])['samples']
    for item in inputs:
        frozen_bytes(root / 'platform/inputs' / Path(item['image']).name, fpath(item['image']).read_bytes())
    receipt = {'format': 'actual-agent-classification-prepared-v1', 'prepared_sha256': digest(p),
        'qualified': qualified, 'original_workspace_files': before, 'original_workspace': str(original),
        'owned_native': str(native), 'database_file_relocations': relocated,
        'annotations_sha256': fingerprint(annotations(original / 'platform.sqlite3')),
        'platform_source': str(platform), 'platform_source_files': platform_binding(platform),
        'inputs': inputs, 'model_id': qualified['model_id'], 'project_id': qualified['project_id'],
        'training_calls': 0, 'gpu_calls': 0, 'controller_calls': 0, 'source_unchanged': True,
        'scope': 'Full native workspace copy with registered file path relocation only; original model/package bytes and labels retained; historical two-image engineering inputs'}
    frozen_json(root / 'prepared.json', receipt)
    return root, receipt


def child_command(args, receipt, root, mode, port=0):
    return [str(fpath(args.native_python)), '-B', '-u', '-X', 'utf8', '-c',
        'import runpy,sys;from pathlib import Path;p=Path(sys.argv[2]).resolve();sys.path.insert(0,sys.argv[1]);sys.argv=[str(p),*sys.argv[3:]];runpy.run_path(str(p),run_name="__main__")',
        str(Path(__file__).parent), str(Path(__file__)), '--child-mode', mode,
        '--native-source', receipt['qualified']['source_root'], '--native-data', receipt['owned_native'],
        '--child-port', str(port)]


async def execute(args, root, receipt):
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.jobs.routes import JobService
    from workbench_platform.jobs.projection import JobProjectionStore
    from workbench_platform.jobs.classification_handoff import ClassificationHandoff
    from workbench_platform.artifacts.classification_results import ClassificationResults
    from workbench_platform.catalog.launch import LaunchStatus
    from scripts.evaluate_screen_agent_replay import copy_controller
    processes = []; value = runtime = run = None
    native = fpath(receipt['owned_native']); original = fpath(receipt['original_workspace'])
    start = time.perf_counter(); report = None
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        for mode in ('serve', 'worker'):
            with (root / ('native-' + mode + '.log')).open('wb') as log:
                processes.append(subprocess.Popen(child_command(args, receipt, root, mode, port), cwd=root,
                    stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW))
        deadline = time.perf_counter() + 60
        while time.perf_counter() < deadline:
            if any(process.poll() is not None for process in processes):
                raise ValueError('Owned native classification child exited; inspect F: log')
            try:
                await asyncio.to_thread(native_http, base, '/api/v1/health'); break
            except (OSError, ValueError):
                await asyncio.sleep(.2)
        else:
            raise ValueError('Owned classification service failed to become ready')
        value, _ = services_for_files(root / 'platform', args.data_python)
        workspace = value.workspace_store.get_workspace('s07workspace')
        link = value.workspace_store.add_link(workspace.workspace_id, 'classification', receipt['project_id'],
            role='owner', expected_revision=workspace.revision, link_id='s07ActualClassificationLink')
        def current(family):
            if family != 'classification' or any(process.poll() is not None for process in processes):
                raise LaunchStatus(410, 'APP_UNAVAILABLE')
            native_http(base, '/api/v1/health')
            return SimpleNamespace(instance_id='s07ActualClassificationInstance', base_url=base, pid=processes[0].pid)
        launches = SimpleNamespace(current=current)
        jobs = JobService(JobProjectionStore(root / 'platform/jobs.sqlite3'), value.workspace_store,
            launches, ocr_token_file=None)
        handoff = ClassificationHandoff(root / 'platform/classification-handoff.sqlite3', jobs, *value.asset_library[:2])
        value.job_service = jobs; value.classification_handoff = handoff; value.launch_service = launches
        value.supervisor = SimpleNamespace(registry=SimpleNamespace(installations={
            'classification': SimpleNamespace(installed=True)}))
        value.artifact_center.classification = ClassificationResults(value.artifact_center.store, handoff)
        assets = [value.asset_library[0].register(workspace.workspace_id, 'inputs', Path(item['image']).name,
            media_type='image/png', storage_mode='managed') for item in receipt['inputs']]
        runtime = AgentRuntime(root / 'platform/agent', value); value.agent = runtime
        required = TOOLS | DISCOVERY_TOOLS
        if required - set(runtime.registry.tools):
            raise ValueError('Required registered classification/discovery tool missing')
        runtime.registry.tools = {name: spec for name, spec in runtime.registry.tools.items() if name in required}
        controller = copy_controller(runtime, fpath(args.controller_store))
        await runtime.start()
        session = runtime.store.create_session(workspace.workspace_id, 'S07真实分类CPU推理与交付',
            [asset.asset_id for asset in assets], 's07RealClassificationSession')
        goal = (f'请使用平台已有工具完成仅当前选中的两张图片的分类工程模拟并交付实际结果。已有原生分类项目link_id={link.link_id}，'
            f'已发布模型model_id={receipt["model_id"]}。先查询确认项目模型目录，固定选中图片集合，使用该模型且device=cpu提交真实推理。'
            '读取实际预测、置信度、状态及人工标签；对实际分类结果固定直接审计证据并读回，导出分类结果并发布可下载成果。'
            '原模型及人工标注必须保持原样，禁止训练、改标签、改模型、自动接受、代签人工确认、GPU调用、扩展资料、外部通用工具。'
            '低置信度或needs_review只列为待人工复核，不能称业务分类准确或已人工确认；本任务只验证两张历史合成工程图的真实工具链。')
        frozen_json(root / 'actual-goal.json', {'goal': goal, 'controller': controller,
            'selected_assets': [asset.asset_id for asset in assets], 'scope': 'Actual configured controller; no injected tool sequence'})
        run = await runtime.submit(session['id'], goal, 's07RealClassificationGoal')
        waiting = []; deadline = time.perf_counter() + args.timeout
        while time.perf_counter() < deadline:
            terminal = runtime.store.run(run['id'])
            if terminal['status'] not in {'running', 'waiting_job', 'waiting_user'}:
                break
            if terminal['status'] == 'waiting_user':
                raise ValueError('Explicit fixed classification task unexpectedly requested new input')
            if terminal['status'] == 'waiting_job' and not waiting:
                waiting.append({'status': 'waiting_job'})
            await asyncio.sleep(.2)
        else:
            raise ValueError('Real classification Agent exceeded bounded timeout')
        operations = runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
        traces = [{**{key: row[key] for key in ('id','call_id','tool','state','created','updated')},
            'arguments': json.loads(row['arguments']), 'receipt': json.loads(row['result']) if row.get('result') else None} for row in operations]
        frozen_json(root / 'actual-agent-operations.json', traces)
        successful = {row['tool']: row for row in traces if (row['receipt'] or {}).get('status') == 'success'}
        missing = sorted({'assets_create_set','classification_prepare','classification_infer','classification_read_results',
            'audit_bind_evidence','audit_read_evidence','classification_export','delivery_publish'} - set(successful))
        # Inference submission is pending by design; verify its actual native completion below.
        if 'classification_infer' not in successful and any(row['tool']=='classification_infer' and
                (row['receipt'] or {}).get('status') == 'pending' for row in traces):
            missing.remove('classification_infer')
        exports = {}; checks = {}
        if not missing:
            infer = next(row for row in traces if row['tool'] == 'classification_infer')
            if infer['arguments']['device'] != 'cpu' or infer['arguments']['model_id'] != receipt['model_id']:
                raise ValueError('Actual Agent selected an unauthorized device/model')
            results = successful['classification_read_results']['receipt']['data']
            if results['model_id'] != receipt['model_id'] or results['total'] != len(assets):
                raise ValueError('Actual prediction model/input coverage differs')
            if {row['asset_id'] for row in results['items']} != {asset.asset_id for asset in assets}:
                raise ValueError('Actual prediction expanded or substituted selected sources')
            if any(row['prediction_status'] == 'accepted' or row.get('disposition_state') == 'confirmed' for row in results['items']):
                raise ValueError('Actual engineering predictions unexpectedly accepted or confirmed')
            evidence = successful['audit_read_evidence']['receipt']['data']
            if evidence.get('stale') or evidence['snapshot_hash'] != successful['audit_bind_evidence']['receipt']['data']['snapshot_hash']:
                raise ValueError('Actual classification evidence is stale or differs')
            bundle = successful['classification_export']['receipt']['data']['bundle']
            for item in bundle['files']:
                artifact = value.artifact_center.file(workspace.workspace_id, bundle['bundle_id'], item['name'])
                with value.artifact_center.store.stage_download(workspace.workspace_id, artifact.artifact_id,
                        expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                    frozen_bytes(root / 'delivered' / item['name'], path.read_bytes())
                exports[item['name']] = {'sha256': artifact.blob_hash, 'artifact_id': artifact.artifact_id}
            delivery = successful['delivery_publish']['receipt']['data']
            if not delivery.get('verified') or bundle['bundle_id'] not in {item['bundle_id'] for item in delivery.get('delivery', [])}:
                raise ValueError('Actual Agent did not publish verified classification artifacts')
            checks = {'selected_inputs_verified': len(assets), 'model_id': results['model_id'],
                'cpu': True, 'auto_accept': False, 'evidence_snapshot_hash': evidence['snapshot_hash'], 'verified_delivery': True}
        requests = runtime.store.rows('SELECT * FROM model_requests WHERE run_id=? ORDER BY created', (run['id'],))
        frozen_json(root / 'actual-controller-requests.json', [{key: row[key] for key in
            ('id','step','attempt','state','created','updated') if key in row} for row in requests])
        verified = not missing and bool(requests) and terminal['status'] in {'succeeded','partial','answered'}
        report = {'format':'actual-agent-classification-result-v1','status':'real_agent_chain_verified' if verified else 'real_agent_chain_incomplete',
            'agent_status':terminal['status'],'run_id':run['id'],'actual_controller_requests':len(requests),
            'actual_tool_operations':len(operations),'missing_required_successful_tools':missing,
            'literal_checks':checks,'export_readback':exports,'wall_seconds':time.perf_counter()-start,
            'waiting_observations':waiting,'training_calls':0,'gpu_calls':0,'business_validated':False,
            'scope':'Actual configured Agent plus production native HTTP adapters and CPU published-model inference on two historical synthetic test inputs; not business accuracy'}
    except Exception as error:
        frozen_json(root / 'failure.json', {'error':str(error),'run_id':run['id'] if run else None,
            'scope':'Actual classification harness failure; partial records retained'})
        raise
    finally:
        if runtime:
            if run and runtime.store.run(run['id'])['status'] in {'running','waiting_job','waiting_user'}:
                runtime.cancel(run['id'])
            await runtime.close()
        if value:
            value.batch_service.close(); value.data_workbench.close()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate(); process.wait(timeout=10)
        unchanged = inventory(original) == receipt['original_workspace_files']
        same_annotations = fingerprint(annotations(native/'platform.sqlite3')) == receipt['annotations_sha256']
        frozen_json(root/'owned-native-stop.json', {'original_source_unchanged':unchanged,
            'annotations_unchanged':same_annotations,'owned_processes_exited':all(p.poll() is not None for p in processes),
            'pids':[p.pid for p in processes]})
        if not unchanged or not same_annotations:
            raise ValueError('Actual classification source/annotation isolation failed')
    if platform_binding(fpath(args.platform_source)) != receipt['platform_source_files']:
        raise ValueError('Frozen platform source changed during actual Agent run')
    report['original_source_and_annotations_unchanged'] = True
    report['owned_native_processes_stopped'] = True
    frozen_json(root/'result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-mode', choices=['serve','worker'])
    parser.add_argument('--native-source'); parser.add_argument('--native-data')
    parser.add_argument('--child-port', type=int, default=0)
    for name in ('prepared','platform-source','native-python','data-python','output-root'):
        parser.add_argument('--'+name)
    parser.add_argument('--controller-store',default='F:/OCR/output/ai-audit-product-dev-20261004/workspace/agent/operations.sqlite3')
    parser.add_argument('--agent-layer',default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--run-controller',action='store_true')
    args=parser.parse_args()
    if args.child_mode:
        sys.path.insert(0,str(fpath(args.native_source)/'src'))
        from image_platform.settings import Settings
        settings=Settings(fpath(args.native_data));settings.prepare()
        if args.child_mode=='worker':
            from image_platform.worker import serve_worker
            serve_worker(settings)
        else:
            import uvicorn
            from image_platform.api import create_app
            uvicorn.run(create_app(settings),host='127.0.0.1',port=args.child_port,log_level='warning')
        return
    root,receipt=prepare(args)
    if args.run_controller:
        report=asyncio.run(execute(args,root,receipt));print(report['status'],report['actual_controller_requests'],report['actual_tool_operations'])
    else:
        print('prepared controller_calls=0 training_calls=0 gpu_calls=0')


if __name__=='__main__':
    main()
