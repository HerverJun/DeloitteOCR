"""Optional DSH connections. Private server credentials use the existing vault."""
from __future__ import annotations

import json
from workbench_platform.supervisor.processes import require_f_path
from .credentials import CredentialVault
from .store import canonical, Conflict

SETTING = 'agent-general-integrations-v1'


from packages.contracts.agent_integrations import McpServer, Integrations


def validate_server(server):
    if server.transport != 'stdio':
        return
    path = require_f_path(server.command)
    if not path.is_file():
        raise ValueError('MCP可执行文件不存在')
    server.command = str(path)
    denied = {'HOME', 'CODEX_HOME', 'TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA',
              'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'PIP_CACHE_DIR',
              'PIP_TARGET', 'HF_HOME', 'TORCH_HOME', 'NPM_CONFIG_CACHE', 'DSH_HOME'}
    if any(name.upper() in denied for name in server.env):
        raise ValueError('MCP环境不能改写宿主存储边界')


def saved(runtime):
    rows = runtime.store.rows('SELECT value FROM settings WHERE key=?', (SETTING,))
    return json.loads(rows[0]['value']) if rows else {'revision': 0, 'subagents': True, 'mcp_servers': []}


def view(runtime):
    value = saved(runtime)
    return {**value, 'mcp_servers': [{key: item for key, item in server.items() if key != 'secret_ref'} |
        {'credential_configured': bool(server.get('secret_ref'))} for server in value['mcp_servers']],
        'web': {'fetch': True, 'search': False, 'search_reason': '尚未配置搜索服务'},
        'storage': '任务专用F盘目录；本地MCP进程使用DSH写入边界'}


def save(runtime, draft):
    value = Integrations.model_validate(draft)
    previous = saved(runtime)
    if value.revision != previous['revision']:
        raise Conflict('扩展配置已改变，请重新读取')
    old = {server['name']: server for server in previous['mcp_servers']}
    vault = CredentialVault(runtime.store.root / 'integration-credentials')
    servers = []
    for server in value.mcp_servers:
        validate_server(server)
        data = server.model_dump(exclude={'env', 'headers'}, exclude_none=True)
        # Empty secret maps preserve a saved same-transport connection. To
        # replace/revoke it, remove that server and save or provide new values.
        prior = old.get(server.name, {})
        ref = prior.get('secret_ref') if prior.get('transport') == server.transport else None
        if server.env or server.headers:
            ref = vault.save(canonical({'env': server.env, 'headers': server.headers}))
        if ref:
            data['secret_ref'] = ref
        servers.append(data)
    with runtime.store.transaction() as db:
        current = db.execute('SELECT value FROM settings WHERE key=?', (SETTING,)).fetchone()
        revision = json.loads(current[0])['revision'] if current else 0
        if revision != value.revision:
            raise Conflict('扩展配置已改变，请重新读取')
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (SETTING, canonical({
            'revision': revision + 1, 'subagents': value.subagents, 'mcp_servers': servers})))
    # No secret values enter the model, event journal or public settings.
    return view(runtime)


def resolved(runtime):
    value = saved(runtime)
    vault = CredentialVault(runtime.store.root / 'integration-credentials')
    servers = []
    for server in value['mcp_servers']:
        if not server.get('enabled', True):
            continue
        config = {key: item for key, item in server.items() if key not in {'name', 'enabled', 'secret_ref'}}
        if server.get('secret_ref'):
            config.update(json.loads(vault.read(server['secret_ref'])))
        validated = McpServer.model_validate({'name': server['name'], **config})
        validate_server(validated)
        excluded = {'name', 'enabled'} | ({'url', 'headers'} if validated.transport == 'stdio' else {'command', 'args', 'env'})
        servers.append({**validated.model_dump(exclude=excluded, exclude_none=True), 'serverName': server['name']})
    return {**value, 'mcp_servers': servers}
