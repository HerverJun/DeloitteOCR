"""Exercise actual frozen platform restoration against isolated SQLite copies.

This is a CPU readiness check, not evidence of actual queue/model recovery.
All cases read preserved asset identities; no registrations are created and
no Agent start/reconcile, native HTTP request, controller or worker is allowed.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import closing
import http.client
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from common import fingerprint, frozen_json, read_json
import controlled_recovery as recovery
import controlled_recovery_run as runner


def copy_scope(source,target,inventory,directories):
    recovery.copy_bytes(source,target,inventory,directories)
    for name in inventory:
        if Path(name).suffix in {'.sqlite3','.sqlite','.db'}:
            recovery.consolidate_database(target/name,target)
            recovery.migrate_database_paths(target/name,source,target)
    for path in sorted(target.rglob('*.json')):
        if 'credentials' in path.parts:continue
        value=path.read_text('utf-8')
        changed=recovery.rewrite_text(value,source,target)
        if changed!=value:path.write_text(changed+'\n','utf-8')
    recovery.scan_runtime_paths(target,source)


def assets_state(root):
    with closing(recovery.connect(root/'assets.sqlite3',readonly=True)) as db:
        return {name:[dict(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('registered_asset','asset_set_versions','asset_set_members')}


def agent_state(root):
    with closing(recovery.connect(root/'agent/operations.sqlite3',readonly=True)) as db:
        return {name:[dict(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('runs','operations','model_requests')}


def check(prepared_path,output):
    recovery.require_environment()
    prepared_path=recovery.plain_path(prepared_path);prepared=read_json(prepared_path)
    source=recovery.plain_path(prepared['runtime_root'])
    output=recovery.plain_path(output)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('A new independent CPU evidence scope is required')
    source_files=recovery.file_inventory(source);source_dirs=recovery.directory_inventory(source)
    output.mkdir(parents=True,exist_ok=False)
    sources=recovery.frozen_sources(prepared['source_binding']['freeze']['path'])
    cases=[]
    for case in ('restore_original_identities','missing_empty_blob_root','changed_trusted_root','missing_registered_source'):
        root=output/case/'runtime'
        copy_scope(source,root,source_files,source_dirs)
        configuration={'output':str(output/case),'runtime_root':str(root),'sources':sources,
                       'agent_layer':'F:/OCR/output/platform-agent-dev-20261004/dependencies/agent',
                       'pdf_layer':'F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/build/public-quality-20260916/tool/site-packages/pdfplumber'}
        runner.configure(configuration)
        from workbench_platform.agent.runtime import AgentRuntime
        from workbench_platform.assets.registry import AssetUnavailable
        before_native=recovery.native_state(root/recovery.NATIVE_DB)
        before_assets=assets_state(root);before_agent=agent_state(root)
        mapping=runner.restored_mapping(root,prepared['batch_id'])
        service=SimpleNamespace(project_id=prepared['native_project_id'],base_url='http://127.0.0.1:1',token='cpu-not-valid')
        if case=='missing_empty_blob_root':(root/'blobs').rmdir()
        if case=='changed_trusted_root':
            missing=root/'wrong-input-root';missing.mkdir()
        if case=='missing_registered_source':
            record=before_assets['registered_asset'][0]
            (root/'inputs'/record['relative_path']).unlink()
        services=keeper=agent=None;error=None;reads=None
        try:
            with (patch.object(http.client.HTTPConnection,'request',side_effect=AssertionError('CPU check cannot dispatch HTTP')),
                  patch.object(subprocess,'Popen',side_effect=AssertionError('CPU check cannot start a process'))):
                services,keeper=runner.restore_services(root,service,mapping)
                if case=='changed_trusted_root':services.asset_library[0].roots={'replay':missing}
                reads=runner.verify_restored_assets(services,mapping,root)
                agent=AgentRuntime(root/'agent',services)
        except BaseException as failure:
            error={'type':type(failure).__name__,'code':getattr(failure,'code',None)}
        finally:
            if agent:asyncio.run(agent.close())
            if keeper:keeper.close()
        after_native=recovery.native_state(root/recovery.NATIVE_DB)
        after_assets=assets_state(root);after_agent=agent_state(root)
        unchanged=before_native==after_native and before_assets==after_assets and before_agent==after_agent
        passed=unchanged and ((error is None and reads['asset_count']==60) if case=='restore_original_identities' else
                             error is not None and error['type'] in {'ValueError','AssetUnavailable'})
        report={'case':case,'passed':passed,'error':error,'assets':reads,
                'native_tasks_and_budgets_unchanged':before_native==after_native,
                'asset_ids_revisions_fixed_sets_unchanged':before_assets==after_assets,
                'agent_identity_operations_requests_unchanged':before_agent==after_agent,
                'primary_task_count':len(before_native['screen_tasks']),
                'context_count':len(before_native['screen_contexts']),
                'assets_before_sha256':fingerprint(before_assets),'assets_after_sha256':fingerprint(after_assets),
                'agent_before_sha256':fingerprint(before_agent),'agent_after_sha256':fingerprint(after_agent),
                'service_or_model_started':False,'native_or_controller_request_sent':False,'agent_started_or_reconciled':False}
        frozen_json(output/case/'result.json',report);cases.append(report)
    intact=recovery.file_inventory(source)==source_files and recovery.directory_inventory(source)==source_dirs
    report={'format':'controlled-recovery-actual-restore-cpu-v1','prepared':recovery.bound(prepared_path),
            'source':sources['freeze'],'cases':cases,'success':intact and all(case['passed'] for case in cases),
            'prepared_runtime_files_and_directories_unchanged':intact,
            'helper':recovery.bound(Path(__file__)),'runner':recovery.bound(Path(runner.__file__)),
            'migration_helper':recovery.bound(Path(recovery.__file__)),
            'original_actual_databases_opened':False,'service_or_model_started':False,
            'actual_recovery_proven':False,'truth_read':False}
    frozen_json(output/'result.json',report)
    print(json.dumps({'success':report['success'],'cases':len(cases),'prepared_runtime_unchanged':intact,
                      'service_or_model_started':False,'actual_recovery_proven':False}))
    if not report['success']:raise SystemExit(1)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();check(args.prepared,args.output)


if __name__=='__main__':main()
