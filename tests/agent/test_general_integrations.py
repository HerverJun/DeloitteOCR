"""Real official tools and protocol fixtures; no model success is fabricated."""
import asyncio
import csv
import json
from pathlib import Path
import re

import test_general_agent as base
from workbench_platform.agent.integrations import save as save_integrations


class GeneralIntegrationTests(base.GeneralAgentTests):
    async def test_cold_restore_moves_cwd_python_and_write_boundary_without_rewriting_history(self):
        from types import SimpleNamespace
        from scripts.backup_workspace import create, restore
        from workbench_platform.store import WorkspaceStore
        from workbench_platform.assets.registry import AssetRegistry
        from workbench_platform.assets.sets import AssetSetStore
        from workbench_platform.artifacts.center import ArtifactCenter
        from workbench_platform.agent.runtime import AgentRuntime
        root = Path(self.temporary.name)
        original, backup, recovered = (root / name for name in ('cold-source', 'cold-backup', 'cold-restored'))
        def services_at(directory):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'asset-blobs').mkdir(exist_ok=True)
            workspaces = WorkspaceStore(directory / 'workspaces.sqlite3')
            if directory == original:
                workspaces.create_workspace('Cold restore', workspace_id='wtest')
            registry = AssetRegistry(directory / 'assets.sqlite3', directory / 'asset-blobs', {'files': directory / 'asset-blobs'})
            sets = AssetSetStore(registry)
            return SimpleNamespace(workspace_store=workspaces, asset_library=(registry, sets, None),
                data_workbench=self.native,
                artifact_center=ArtifactCenter(directory / 'artifact-center', registry, sets, self.native))
        async def provider(state, schemas):
            if state['step'] == 0:
                return base.response(state, 'write', {'file_path': 'initial.txt', 'content': '42'})
            if state['step'] == 1:
                return base.response(state, 'ask_user', {'question': '现在继续恢复检查？', 'options': ['继续']})
            if state['step'] == 2:
                self.assertTrue(Path(state['working_directory']).is_relative_to(recovered))
                self.assertTrue(Path(state['python_executable']).is_relative_to(recovered))
                code = ('from pathlib import Path\n'
                    'assert Path("initial.txt").read_text() == "42"\n'
                    'old = Path(' + repr(str(original / 'agent/workspaces' / state['session_id'] / 'forbidden.txt')) + ')\n'
                    'try:\n    old.write_text("wrong directory")\n'
                    'except PermissionError:\n    print("old workspace denied")\n'
                    'else:\n    raise RuntimeError("old workspace remained writable")\n'
                    'Path("restored.txt").write_text("42")\n')
                return base.response(state, 'write', {'file_path': 'check.py', 'content': code})
            if state['step'] == 3:
                return base.response(state, 'pwsh', {'description': '核对恢复目录与写入边界',
                    'command': '& "' + state['python_executable'] + '" -B check.py'})
            result = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            self.assertIn('old workspace denied', result['data']['text'])
            return base.response(state, content='已在恢复目录读取并生成结果，旧目录写入被拒绝。')
        services = services_at(original)
        runtime = AgentRuntime(original / 'agent', services, model_provider=provider)
        services.agent = runtime
        await runtime.start()
        session = runtime.store.create_session('wtest', 'Cold task', [], 'cold-session')
        run = await runtime.submit(session['id'], '保存42，等待确认后检查恢复目录', 'cold-first')
        await self.wait_status(runtime, run['id'], {'waiting_user', 'failed'})
        self.assertEqual(runtime.store.run(run['id'])['status'], 'waiting_user')
        await runtime.close()
        log = next((original / 'agent/harness/sessions').rglob('session.v4.jsonl'))
        prefix = log.read_bytes()
        create(original, backup)
        restore(backup, recovered)
        services = services_at(recovered)
        restored = AgentRuntime(recovered / 'agent', services, model_provider=provider)
        services.agent = restored
        await restored.start()
        try:
            decision = restored.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))[0]
            await restored.reply(decision['id'], '继续', 1, 'cold-answer')
            await self.wait_status(restored, run['id'], {'succeeded', 'failed'})
            self.assertEqual(restored.store.run(run['id'])['status'], 'succeeded', restored.store.run(run['id']))
            new_log = recovered / log.relative_to(original)
            self.assertTrue(new_log.read_bytes().startswith(prefix))
            events = [json.loads(line) for line in new_log.read_text('utf-8').splitlines()[1:]]
            self.assertTrue(any(event['type'] == 'working-directory/change' and
                Path(event['data']['cwd']).is_relative_to(recovered) for event in events))
            self.assertFalse((original / 'agent/workspaces' / session['id'] / 'forbidden.txt').exists())
            self.assertEqual((recovered / 'agent/workspaces' / session['id'] / 'restored.txt').read_text(), '42')
        finally:
            await restored.close()

    async def test_failed_third_file_continues_without_repeating_successful_files(self):
        import hashlib
        import subprocess
        from unittest.mock import patch
        assets = []
        for index in range(3):
            path = base.fixtures.RUN / 'fixtures' / f'retry-clean-{index}.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow([f'名称{i}' for i in range(7)] + ['编号', '金额'])
                writer.writerow([' 甲 '] * 7 + ['000731', '100.20'])
            assets.append(self.assets.register('wtest', 'fixtures', path.name, media_type='text/csv', storage_mode='managed'))
        async def provider(state, schemas):
            if state['step'] in {0, 2}:
                requested = assets if state['step'] == 0 else assets[2:]
                return base.response(state, 'data_clean_files', {'files': [{'asset_id': item.asset_id} for item in requested],
                    'steps': [{'operator': 'trim_columns', 'columns': [f'c{i}' for i in range(1, 8)]}]})
            result = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            self.assertEqual(result['status'], 'success', result)
            self.assertEqual(len(result['data']['files']), 2 if state['step'] == 1 else 1)
            return base.response(state, content='前两份可下载，第三份待恢复。' if state['step'] == 1 else '已补齐第三份，前两份成果保持原样。')
        runtime, session = await self.runtime(provider)
        runtime.tasks.update(session['id'], {'request_id': 'retry-select',
            'expected_revision': runtime.tasks.get(session['id'])['revision'],
            'selected_assets': [item.asset_id for item in assets]})
        popen = subprocess.Popen
        injected = []
        def fail_once(argv, *arguments, **options):
            if isinstance(argv, list) and any('modules.data_workbench.interactive_worker' in str(value) for value in argv):
                request = json.loads(Path(argv[-2]).read_text('utf-8'))
                native_session = Path(request['session_dir']).name
                if request['action'] == 'step' and 'retry-clean-2.csv' in self.native.get(native_session)['name'] and not injected:
                    injected.append(request['job_id'])
                    # A real failed child process, with the original native
                    # request and parameters retained in its durable journal.
                    argv = [argv[0], '-B', '-c', 'raise RuntimeError("controlled one-time worker fault")']
            return popen(argv, *arguments, **options)
        try:
            with patch('subprocess.Popen', fail_once):
                run = await runtime.submit(session['id'], '三份文件前七列去空格，保留编号金额', 'retry-first')
                await self.wait_status(runtime, run['id'], {'paused', 'failed', 'succeeded'})
                self.assertEqual(runtime.store.run(run['id'])['status'], 'paused', runtime.store.run(run['id']))
                self.assertEqual(len(injected), 1)
                first = json.loads(runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='data_clean_files' ORDER BY created", (run['id'],))[0]['result'])
                retained = {}
                for file in first['data']['files']:
                    artifact = self.center.file('wtest', file['bundle']['bundle_id'], 'cleaned.csv')
                    retained[file['asset_id']] = (file['data_session_id'], artifact.blob_hash, file['bundle']['bundle_id'])
                continued = await runtime.submit(session['id'], '只补齐失败的第三份，前两份结果保持不变', 'retry-continue')
                self.assertEqual(continued['id'], run['id'])
                await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
                self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
                self.assertEqual(runtime.store.run(run['id'])['rounds'], 4)
                for asset_id, (native_session, checksum, bundle_id) in retained.items():
                    self.assertEqual(self.center.file('wtest', bundle_id, 'cleaned.csv').blob_hash, checksum)
                    self.assertEqual(len(self.native.get(native_session)['jobs']), 3)
                self.assertEqual(len(self.native.list('wtest')), 4)
        finally:
            await runtime.close()

    async def test_python_imports_local_project_and_installed_xlsx_library(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                return base.response(state, 'write', {'file_path': 'helper.py', 'content': 'value = 42\n'})
            if state['step'] == 1:
                return base.response(state, 'pwsh', {'description': '验证本地模块与XLSX库', 'command':
                    '& "' + state['python_executable'] + '" -B -c "import helper, openpyxl; w=openpyxl.Workbook(); w.active.append([str(helper.value)]); w.save(\'result.xlsx\'); print(helper.value)"'})
            previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            self.assertIn('42', previous['data']['text'])
            self.assertNotIn('[stderr]', previous['data']['text'])
            return base.response(state, content='已验证本地导入，XLSX生成成功。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '创建模块并实际运行，生成Excel', 'imports-xlsx')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
            state = runtime.graph.load(run['id'])
            self.assertTrue((Path(state['working_directory']) / 'result.xlsx').is_file())
        finally:
            await runtime.close()

    async def test_background_job_survives_turn_and_can_be_collected_next_turn(self):
        job = None
        async def provider(state, schemas):
            nonlocal job
            if state['request_id'] == 'background-first':
                if state['step'] == 0:
                    return base.response(state, 'pwsh', {'description': '稍后输出结果',
                        'command': 'Start-Sleep -Seconds 3; Write-Output 42', 'run_in_background': True})
                previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
                text = previous['data']['text']
                match = re.search(r'(?:job|Job)[ :]+([A-Za-z0-9_-]+)', text)
                self.assertIsNotNone(match, text)
                job = match.group(1)
                return base.response(state, content='后台作业已启动。')
            if state['step'] == 0:
                return base.response(state, 'job_output', {'job_id': job, 'wait': True, 'timeout_ms': 10000})
            previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            self.assertIn('42', previous['data']['text'])
            return base.response(state, content='后台结果42。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '后台启动', 'background-first')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
            for _ in range(100):
                if session['id'] in runtime.graph.idle_processes:
                    break
                await asyncio.sleep(.05)
            self.assertIn(session['id'], runtime.graph.idle_processes)
            pid = runtime.graph.idle_processes[session['id']].process.pid
            followup = await runtime.submit(session['id'], '读取后台结果', 'background-second')
            await self.wait_status(runtime, followup['id'], {'answered', 'succeeded', 'failed'})
            self.assertIn(runtime.store.run(followup['id'])['status'], {'answered', 'succeeded'}, runtime.store.run(followup['id']))
            self.assertEqual(runtime.graph.idle_processes.get(session['id'], runtime.graph.processes.get(followup['id'])).process.pid, pid)
        finally:
            await runtime.close()

    async def test_three_files_seven_columns_use_three_native_trim_jobs(self):
        assets = []
        for number in range(3):
            path = base.fixtures.RUN / 'fixtures' / f'general-clean-{number}.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow([f'名称{i}' for i in range(7)] + ['编号', '金额'])
                writer.writerow([' 甲 ' for _ in range(7)] + ['000731', '100.20'])
                writer.writerow([' 乙 ' for _ in range(7)] + ['000732', '118.75'])
            assets.append(self.assets.register('wtest', 'fixtures', path.name, media_type='text/csv', storage_mode='managed'))
        async def provider(state, schemas):
            if state['step'] == 0:
                self.assertIn('data_clean_files', schemas)
                return base.response(state, 'data_clean_files', {'files': [{'asset_id': asset.asset_id} for asset in assets],
                    'steps': [{'operator': 'trim_columns', 'columns': [f'c{i}' for i in range(1, 8)]}]})
            previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            self.assertEqual(previous['status'], 'success', previous)
            self.assertEqual(len(previous['data']['files']), 3, previous)
            self.assertEqual(previous['data']['failures'], [])
            return base.response(state, content='三份文件的前七列已清洗，编号和金额原文保留。')
        runtime, session = await self.runtime(provider)
        try:
            runtime.tasks.update(session['id'], {'request_id': 'clean-select',
                'expected_revision': runtime.tasks.get(session['id'])['revision'],
                'selected_assets': [asset.asset_id for asset in assets]})
            run = await runtime.submit(session['id'], '清洗三份文件前七列，保留编号金额', 'clean-three')
            await self.wait_status(runtime, run['id'], {'succeeded', 'paused', 'failed'})
            final = runtime.store.run(run['id'])
            self.assertEqual(final['status'], 'succeeded', final)
            self.assertEqual(final['rounds'], 2)
            sessions = self.native.list('wtest')
            self.assertEqual(len(sessions), 3)
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(self.native.db_path)) as db:
                steps = db.execute("SELECT COUNT(*) FROM jobs WHERE action='step'").fetchone()[0]
            self.assertEqual(steps, 3)
            operation = runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='data_clean_files'", (run['id'],))[0]
            result = json.loads(operation['result'])
            for item in result['data']['files']:
                bundle = item['bundle']
                csv_file = next(file for file in bundle['files'] if file['name'].endswith('.csv'))
                artifact = self.center.file('wtest', bundle['bundle_id'], csv_file['name'])
                with self.center.store.stage_download('wtest', artifact.artifact_id, expected_revision=artifact.revision,
                                                      expected_hash=artifact.blob_hash) as downloaded:
                    with downloaded.open('r', encoding='utf-8-sig', newline='') as stream:
                        rows = list(csv.reader(stream))
                headers = [header.split(' | ', 1)[-1] for header in rows[0]]
                positions = [headers.index(f'名称{i}') for i in range(7)] + [headers.index('编号'), headers.index('金额')]
                self.assertEqual([rows[1][index] for index in positions], ['甲'] * 7 + ['000731', '100.20'])
                self.assertEqual([rows[2][index] for index in positions], ['乙'] * 7 + ['000732', '118.75'])
        finally:
            await runtime.close()

    async def test_child_session_uses_own_model_state_and_reports_to_parent(self):
        parent_steps = []
        child_steps = []
        async def provider(state, schemas):
            child = state.get('agent_session_id')
            if child:
                child_steps.append(state['step'])
                self.assertNotIn('data_create_session', schemas)
                if state['step'] == 0:
                    return base.response(state, 'pwsh', {'description': '核算乘积', 'command': 'Write-Output (6 * 7)'})
                if state['step'] == 1:
                    return base.response(state, 'send_message', {'agent_id': session['id'], 'message': '已实际运行，6乘7=42。'})
                return base.response(state, content='核算完成。')
            parent_steps.append(state['step'])
            if state['step'] == 0:
                self.assertIn('subagent', schemas)
                return base.response(state, 'subagent', {'description': '核算六乘七', 'prompt': '实际执行6乘7并把结果发回父助手。'})
            if any('6乘7=42' in m.get('content', '') for m in state['messages'] if isinstance(m.get('content'), str)):
                return base.response(state, content='子助手已实际运行并返回42。')
            return base.response(state, content='等待子助手返回。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], '请交给子助手核算六乘七并核实结果', 'child-product')
            await self.wait_status(runtime, run['id'], {'succeeded', 'answered', 'failed'})
            self.assertIn(runtime.store.run(run['id'])['status'], {'succeeded', 'answered'}, runtime.store.run(run['id']))
            self.assertEqual(child_steps, [0, 1, 2])
            self.assertEqual(parent_steps, list(range(len(parent_steps))))
            rows = runtime.store.rows('SELECT * FROM harness_child_sessions WHERE run_id=?', (run['id'],))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['model_calls'], 3)
            events = runtime.store.rows('SELECT DISTINCT session_id FROM harness_events WHERE run_id=?', (run['id'],))
            self.assertEqual(len(events), 2)
            final = runtime.store.rows("SELECT content FROM public_responses WHERE run_id=? AND status='confirmed'", (run['id'],))
            self.assertIn('42', final[-1]['content'])
        finally:
            await runtime.close()

    async def test_real_stdio_mcp_and_write_confinement(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                self.assertIn('mcp__calculator__Multiply', schemas)
                return base.response(state, 'mcp__calculator__Multiply', {'a': 6, 'b': 7})
            previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            self.assertEqual(previous['status'], 'success', previous)
            self.assertIn('42', previous['data']['text'])
            self.assertIn('outside denied', previous['data']['text'])
            return base.response(state, content='MCP实际计算42，写入边界已验证。')
        runtime, session = await self.runtime(provider)
        fixture = Path(self.temporary.name) / 'mcp-server.py'
        fixture.write_text('import sys,json\nfrom pathlib import Path\n'
            'for line in sys.stdin:\n'
            '    request=json.loads(line)\n'
            '    if "id" not in request: continue\n'
            '    method=request["method"]\n'
            '    if method=="initialize": result={"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"fixture-calculator","version":"1"}}\n'
            '    elif method=="tools/list": result={"tools":[{"name":"Multiply","description":"Multiply two integers","inputSchema":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},"required":["a","b"],"additionalProperties":False}}]}\n'
            '    elif method=="tools/call":\n'
            '        a=request["params"]["arguments"]\n'
            '        try: (Path.cwd().parent/"mcp-outside-denied.txt").write_text("forbidden")\n'
            '        except PermissionError: boundary="outside denied"\n'
            '        else: boundary="BOUNDARY FAILURE"\n'
            '        result={"content":[{"type":"text","text":str(a["a"]*a["b"])+" "+boundary}]}\n'
            '    else: result={}\n'
            '    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)\n', encoding='utf-8')
        from workbench_platform.agent.windows_workspace import prepare_owner_access
        prepare_owner_access(Path(self.temporary.name), writable=False, inherit=True)
        save_integrations(runtime, {'mcp_servers': [{'name': 'calculator', 'transport': 'stdio',
            'command': 'F:/OCR/release/Clear/versions/0.10.0/program/runtimes/data/python.exe',
            'args': ['-B', '-u', str(fixture)]}]})
        try:
            run = await runtime.submit(session['id'], '用MCP计算6乘7', 'mcp-real')
            await self.wait_status(runtime, run['id'], {'succeeded', 'failed'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'succeeded', runtime.store.run(run['id']))
            directory = Path(runtime.graph.load(run['id'])['working_directory'])
            self.assertFalse((directory.parent / 'mcp-outside-denied.txt').exists())
        finally:
            await runtime.close()
