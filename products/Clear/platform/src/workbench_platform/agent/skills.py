"""Registered Agent Skills with immutable, session-scoped instruction snapshots.

Skill sources are operator-registered F-drive directories, never project assets.
Only metadata enters the initial prompt. Activation fixes every file's bytes;
resource reads are paged and logged, including after a process restart.
"""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from workbench_platform.supervisor.processes import require_f_path
from .store import Conflict, RunSuperseded, canonical, digest, now


NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
TOOL_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
MAX_FILES = 80
MAX_FILE_BYTES = 256 * 1024
MAX_SKILL_BYTES = 1024 * 1024
MAX_BODY_BYTES = 24 * 1024


@dataclass(frozen=True)
class SkillRequestSnapshot:
    """One request's fixed instructions, declarations and live source view."""
    active: list
    catalog: dict
    instructions: str
    allowed_tool_sets: tuple

    def allowed_tools(self, names, core=()):
        allowed = set(names)
        for declared in self.allowed_tool_sets:
            if declared is not None:
                allowed.intersection_update(declared)
        return allowed | (set(core) & set(names))


def _sha(content: bytes):
    return hashlib.sha256(content).hexdigest()


def _resource_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\x00' in value:
        raise ValueError('技能资源路径无效')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '.' in path.parts or str(path) != value:
        raise ValueError('技能资源必须位于固定技能目录内')
    return path


def _kind(path):
    if path == 'SKILL.md':
        return 'instructions'
    if path.startswith('scripts/'):
        return 'script'
    if path.startswith('references/'):
        return 'reference'
    if path.startswith('assets/'):
        return 'template'
    return 'resource'


def _frontmatter(content: bytes, directory_name):
    # Platform's base runtime intentionally has no Agent packages. Load YAML
    # only through the registered, verified F-drive Agent dependency layer.
    from .dependencies import load_support_dependencies
    load_support_dependencies()
    import yaml

    text = content.decode('utf-8-sig')
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise ValueError('SKILL.md 缺少 YAML frontmatter')
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == '---'), None)
    if end is None:
        raise ValueError('SKILL.md frontmatter 未闭合')
    try:
        metadata = yaml.safe_load(''.join(lines[1:end]))
    except yaml.YAMLError:
        raise ValueError('技能 YAML 元数据无效') from None
    if not isinstance(metadata, dict):
        raise ValueError('技能元数据必须是映射')
    name, description = metadata.get('name'), metadata.get('description')
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) or name != directory_name:
        raise ValueError('技能名称需与目录名一致，使用小写字母、数字和单连字符')
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError('技能说明需为 1 至 1024 字符')
    compatibility = metadata.get('compatibility', '')
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise ValueError('compatibility 需为最多 500 字符的环境说明')
    allowed = metadata.get('allowed-tools')
    if allowed is not None:
        allowed = allowed.split() if isinstance(allowed, str) else allowed
        if not isinstance(allowed, list) or len(allowed) > 128 or not all(
                isinstance(item, str) and TOOL_NAME.fullmatch(item) for item in allowed):
            raise ValueError('allowed-tools 只接受已注册工具的准确名称，不支持 shell 或通配符')
        allowed = sorted(set(allowed))
    extra = metadata.get('metadata', {})
    if not isinstance(extra, dict):
        raise ValueError('metadata 必须为映射')
    required = extra.get('requires-tools', [])
    if isinstance(required, str):
        required = required.split()
    if not isinstance(required, list) or not all(isinstance(item, str) and TOOL_NAME.fullmatch(item) for item in required):
        raise ValueError('requires-tools 只接受准确工具名称')
    body = ''.join(lines[end + 1:]).strip()
    if not body or len(body.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('技能正文为空或超过 24 KiB；请把详细资料放入按需资源')
    return {'name': name, 'description': description.strip(), 'compatibility': compatibility,
            'allowed_tools': allowed, 'required_tools': sorted(set(required)),
            'host': extra.get('host', 'clear-workbench'), 'body': body}


def registration(config_root=None):
    """Read bounded, server-owned root registration, with no request paths."""
    config = require_f_path(Path(config_root) if config_root is not None else
                            Path(__file__).resolve().parents[3] / 'config', 'Skill registration directory')
    manifest = require_f_path(config / 'skills-registry.json', 'Skill registration manifest')
    if not manifest.is_relative_to(config):
        raise ValueError('技能登记文件越出配置目录')
    if not manifest.exists():
        raise ValueError('服务器尚未登记技能根目录')
    if manifest.stat().st_size > 65536:
        raise ValueError('技能登记文件超过 64 KiB')
    try:
        value = json.loads(manifest.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ValueError('技能登记 JSON 无效') from None
    if not isinstance(value, dict) or set(value) != {'format', 'platform_roots', 'workspace_roots'} or value['format'] != 'clear-skill-roots-v1':
        raise ValueError('技能登记格式无效')
    workspace = value['workspace_roots']
    if not isinstance(workspace, dict) or len(workspace) > 32 or not all(
            isinstance(key, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key) for key in workspace):
        raise ValueError('工作区技能根登记无效')

    def checked(values):
        if not isinstance(values, list) or len(values) > 8 or not all(
                isinstance(item, str) and item and len(item) <= 512 and '\x00' not in item for item in values):
            raise ValueError('每项登记最多 8 个明确技能根目录')
        paths = [require_f_path(config / item, 'Registered skill root') for item in values]
        if len(set(paths)) != len(paths):
            raise ValueError('技能根登记重复')
        return paths

    return {'roots': checked(value['platform_roots']),
            'workspace_roots': {identifier: checked(paths) for identifier, paths in workspace.items()}}


class SkillService:
    def __init__(self, store, roots=None, workspace_roots=None, registry=None, scripts_supported=False):
        self.store, self.registry = store, registry
        self.scripts_supported = scripts_supported
        defaults = [Path(__file__).resolve().parents[3] / 'config' / 'skills']
        self.roots = tuple(require_f_path(Path(root), 'Skill source') for root in (defaults if roots is None else roots))
        self.workspace_roots = {identifier: tuple(require_f_path(Path(root), 'Workspace skill source') for root in values)
                                for identifier, values in (workspace_roots or {}).items()}
        # Snapshots reside in the existing F-drive Agent DB, not in user assets.
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS skill_versions(
                    version TEXT PRIMARY KEY,name TEXT NOT NULL,source TEXT NOT NULL,
                    metadata TEXT NOT NULL,files TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_skills(
                    session_id TEXT NOT NULL,name TEXT NOT NULL,version TEXT NOT NULL,
                    activated TEXT NOT NULL,actor TEXT NOT NULL,request_id TEXT NOT NULL,
                    PRIMARY KEY(session_id,name));
                CREATE TABLE IF NOT EXISTS skill_requests(
                    request_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,response TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS skill_resource_reads(
                    session_id TEXT NOT NULL,name TEXT NOT NULL,version TEXT NOT NULL,
                    path TEXT NOT NULL,sha256 TEXT NOT NULL,actor TEXT NOT NULL,
                    request_id TEXT NOT NULL,offset INTEGER NOT NULL,chars INTEGER NOT NULL,
                    created TEXT NOT NULL,
                    PRIMARY KEY(session_id,name,version,path,request_id,offset));
            ''')

    def _scope(self, workspace_id, session_id=None):
        if session_id is not None:
            session = self.store.session(session_id)
            if session['workspace_id'] != workspace_id:
                raise ValueError('技能会话不属于当前项目')
            return session
        if workspace_id is None:
            # The platform directory can be browsed before creating a project.
            # No workspace roots or session mutations are available here.
            return None
        if not workspace_id:
            raise ValueError('技能目录需要项目身份')
        return None

    def _compatible(self, metadata):
        if metadata['host'] != 'clear-workbench':
            return False, '此技能声明的宿主与 Clear Workbench 不兼容'
        names = set(self.registry.tools) if self.registry else None
        if names is not None and self.scripts_supported:
            names.update({'read', 'read_image', 'write', 'edit', 'glob', 'grep', 'pwsh', 'skill'})
        missing = sorted(set(metadata['required_tools']) - names) if names is not None else []
        return (False, '所需工具尚未注册：' + ', '.join(missing)) if missing else (True, '')

    def _load(self, directory, root, source):
        real = require_f_path(directory, 'Skill directory')
        if not real.is_relative_to(root) or real == root or directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
            raise ValueError('技能目录越出登记的根目录')
        # Reject links even inside the root: snapshots may only contain ordinary
        # files and there is no path through a junction to another registered root.
        paths = []
        for path in sorted(directory.rglob('*')):
            checked = require_f_path(path, 'Skill resource')
            if not checked.is_relative_to(real) or path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('技能资源包含链接或越出技能目录')
            if path.is_file():
                paths.append(path)
            if len(paths) > MAX_FILES:
                raise ValueError('技能文件超过 80 个')
        files, resources, total = {}, [], 0
        for path in paths:
            relative = path.relative_to(directory).as_posix()
            _resource_path(relative)
            size = path.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_SKILL_BYTES:
                raise ValueError('技能文件超过单文件 256 KiB 或总计 1 MiB 的限制')
            content = path.read_bytes()
            # A resource can change while it is read. The exact bytes below are
            # the version identity and snapshot; live sizes are not its identity.
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_SKILL_BYTES:
                raise ValueError('技能资源在读取时超过容量限制')
            files[relative] = base64.b64encode(content).decode('ascii')
            resources.append({'path': relative, 'kind': _kind(relative), 'sha256': _sha(content), 'bytes': len(content)})
        if 'SKILL.md' not in files:
            raise ValueError('技能目录缺少 SKILL.md')
        metadata = _frontmatter(base64.b64decode(files['SKILL.md']), directory.name)
        compatible, reason = self._compatible(metadata)
        identity = {'name': metadata['name'], 'source': source,
                    'files': {item['path']: item['sha256'] for item in resources}}
        if self.scripts_supported:
            identity['host_contract'] = 'general-skills-v1'
        version = digest(identity)
        metadata.update({'source': source, 'version': version, 'resources': resources,
                         'compatible': compatible, 'reason': reason,
                         'scripts_supported': self.scripts_supported, 'instruction_origin': 'registered_skill'})
        return metadata, files

    def _scan(self, workspace_id):
        result, rejected, duplicate_names = {}, [], set()
        roots = [('platform', root) for root in self.roots] + [
            ('workspace', root) for root in self.workspace_roots.get(workspace_id, ())]
        for origin, root in roots:
            # Validate the real root again, including a replaced junction.
            try:
                checked = require_f_path(root, 'Skill source')
                if checked != root:
                    raise ValueError('技能根目录真实位置已改变，需重新登记')
            except ValueError:
                # A changed live source cannot prevent restoring a previously
                # fixed version. Refuse to scan it and mark its sources changed.
                rejected.append({'source': origin + ':' + root.name, 'reason': '技能根目录真实位置已改变，需重新登记'})
                continue
            if not root.exists():
                continue
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                source = origin + ':' + directory.name
                try:
                    item = self._load(directory, root, source)
                    name = item[0]['name']
                    if name in result or name in duplicate_names:
                        result.pop(name, None)
                        duplicate_names.add(name)
                        raise ValueError('技能名称在登记目录中重复，不能静默覆盖')
                    result[name] = item
                except (ValueError, OSError, UnicodeError) as error:
                    rejected.append({'source': source, 'reason': str(error)[:300]})
        return result, rejected

    def _public(self, metadata, active_version=None, source_changed=False):
        return {key: value for key, value in metadata.items() if key not in {'body', 'host', 'required_tools'}} | {
            'active': active_version is not None, 'active_version': active_version, 'source_changed': source_changed}

    def _fixed(self, row):
        version = self.store.rows('SELECT * FROM skill_versions WHERE version=?', (row['version'],))
        if not version:
            raise Conflict('已激活技能固定版本丢失，不能改用当前源版本')
        metadata = json.loads(version[0]['metadata'])
        files = json.loads(version[0]['files'])
        actual = {path: _sha(base64.b64decode(content, validate=True)) for path, content in files.items()}
        identity = {'name': metadata['name'], 'source': metadata['source'], 'files': actual}
        if metadata.get('scripts_supported'):
            identity['host_contract'] = 'general-skills-v1'
        if digest(identity) != row['version']:
            raise Conflict('技能固定版本校验失败，不能继续执行')
        parsed = _frontmatter(base64.b64decode(files['SKILL.md'], validate=True), metadata['name'])
        if any(parsed[key] != metadata[key] for key in parsed):
            raise Conflict('技能固定正文与其资源版本不一致')
        return metadata, files

    def _active_snapshot(self, session_id, live):
        result, fixed = [], []
        rows = self.store.rows('SELECT * FROM session_skills WHERE session_id=? ORDER BY name', (session_id,)) if session_id else []
        for row in rows:
            metadata, files = self._fixed(row)
            fixed.append((row, metadata, files))
            current = live.get(row['name']) if live is not None else None
            changed = live is not None and (not current or current[0]['version'] != row['version'])
            value = self._public(metadata, row['version'], changed)
            value.update({'activated': row['activated'], 'actor': row['actor'],
                          'read_resources': self.store.rows(
                              'SELECT path,sha256,request_id,offset,chars,created FROM skill_resource_reads '
                              'WHERE session_id=? AND name=? AND version=? ORDER BY created DESC LIMIT 50',
                              (session_id, row['name'], row['version'])),
                          'resource_read_count': self.store.rows(
                              'SELECT COUNT(*) total FROM skill_resource_reads WHERE session_id=? AND name=? AND version=?',
                              (session_id, row['name'], row['version']))[0]['total']})
            result.append(value)
        return result, fixed

    def active_manifest(self, session_id):
        session = self.store.session(session_id)
        live, _ = self._scan(session['workspace_id'])
        return self._active_snapshot(session_id, live)[0]

    def _catalog(self, scanned, rejected, active, keyword='', offset=0, limit=20):
        fixed = {item['name']: item for item in active}
        items = []
        for name, (metadata, _) in sorted(scanned.items()):
            if keyword.casefold() not in (name + ' ' + metadata['description']).casefold():
                continue
            old = fixed.get(name)
            items.append(self._public(metadata, old['version'] if old else None,
                                      bool(old and old['version'] != metadata['version'])))
        return {'items': items[offset:offset + limit], 'total': len(items),
                'next_offset': offset + limit if offset + limit < len(items) else None,
                'active': active, 'rejected': rejected}

    def catalog(self, workspace_id, session_id=None, keyword='', offset=0, limit=20):
        self._scope(workspace_id, session_id)
        if offset < 0 or limit < 1 or limit > 50 or len(keyword) > 100:
            raise ValueError('技能分页参数无效')
        scanned, rejected = self._scan(workspace_id)
        active, _ = self._active_snapshot(session_id, scanned)
        return self._catalog(scanned, rejected, active, keyword, offset, limit)

    def catalog_prompt(self, workspace_id, session_id=None, limit=4):
        page = self.catalog(workspace_id, session_id, limit=min(max(limit, 1), 4))
        return self._catalog_prompt(page)

    @staticmethod
    def _catalog_prompt(page):
        return {'items': [{key: item[key] for key in ('name', 'description', 'compatible', 'active')}
                          for item in page['items']], 'total': page['total'], 'next_offset': page['next_offset']}

    def request_snapshot(self, workspace_id, session_id, *, scan_sources=True):
        """Scan at most once; recovery only validates immutable activated bytes."""
        self._scope(workspace_id, session_id)
        live, rejected = self._scan(workspace_id) if scan_sources else (None, [])
        active, fixed = self._active_snapshot(session_id, live)
        catalog = self._catalog_prompt(self._catalog(live or {}, rejected, active, limit=4))
        parts, declarations = [], []
        for row, metadata, _ in fixed:
            parts.append(self._instruction(row, metadata))
            declared = metadata.get('allowed_tools')
            declarations.append(frozenset(declared) if declared is not None else None)
        return SkillRequestSnapshot(active, catalog, '\n\n'.join(parts), tuple(declarations))

    def _fence(self, db, session_id, workspace_id, context):
        row = db.execute('SELECT workspace_id FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row or row['workspace_id'] != workspace_id:
            raise ValueError('技能会话项目范围无效')
        if context is None:
            if db.execute("SELECT 1 FROM runs WHERE workspace_id=? AND status IN "
                          "('running','waiting_job','waiting_user','needs_verification')", (workspace_id,)).fetchone():
                raise Conflict('项目正在执行 Agent；请先停止或人工接管，再修改技能')
        else:
            if context.session_id != session_id or context.workspace_id != workspace_id:
                raise ValueError('技能操作上下文范围无效')
            current = db.execute('SELECT generation,status,session_id FROM runs WHERE id=?', (context.run_id,)).fetchone()
            if not current or current['generation'] != context.generation or current['status'] == 'cancelled' or current['session_id'] != session_id:
                raise RunSuperseded('技能操作的执行代际已失效')

    def _request(self, db, request_id, fingerprint):
        old = db.execute('SELECT * FROM skill_requests WHERE request_id=?', (request_id,)).fetchone()
        if old and old['fingerprint'] != fingerprint:
            raise Conflict('技能请求身份与原参数不同')
        return json.loads(old['response']) if old else None

    def _active_in_transaction(self, db, session_id):
        values = []
        for row in db.execute('SELECT s.name,s.version,v.metadata FROM session_skills s JOIN skill_versions v ON '
                              'v.version=s.version WHERE session_id=? ORDER BY s.name', (session_id,)):
            values.append(self._public(json.loads(row['metadata']), row['version']))
        return values

    def _mutation(self, workspace_id, session_id, name, request_id, *, version=None, context=None, activate=True):
        self._scope(workspace_id, session_id)
        if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
            raise ValueError('技能名称无效')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError('技能操作需要稳定请求身份')
        fingerprint = digest([workspace_id, session_id, name, version, activate])
        live, _ = self._scan(workspace_id) if activate else ({}, [])
        with self.store.transaction() as db:
            # Identical committed replies are replayed without reapplying a
            # mutation to a session that has since changed.
            old = self._request(db, request_id, fingerprint)
            if old:
                return old
            self._fence(db, session_id, workspace_id, context)
            active = db.execute('SELECT * FROM session_skills WHERE session_id=? AND name=?', (session_id, name)).fetchone()
            if activate:
                if active and (version is None or version == active['version']):
                    fixed = db.execute('SELECT metadata FROM skill_versions WHERE version=?', (active['version'],)).fetchone()
                    if not fixed:
                        raise Conflict('已激活技能版本丢失')
                    metadata, changed = json.loads(fixed['metadata']), False
                else:
                    if name not in live:
                        raise ValueError('技能未登记或校验未通过')
                    metadata, files = live[name]
                    if version is not None and version != metadata['version']:
                        raise Conflict('技能目录版本已改变，请重新读取')
                    if not metadata['compatible']:
                        raise ValueError(metadata['reason'])
                    # Keep the instruction part bounded even with multiple active
                    # Skills. Full resources remain only in the persisted store.
                    other = db.execute('SELECT v.metadata FROM session_skills s JOIN skill_versions v '
                                       'ON v.version=s.version WHERE session_id=? AND s.name!=?', (session_id, name)).fetchall()
                    if len(other) >= 4 or sum(len(json.loads(row['metadata'])['body'].encode('utf-8')) for row in other) + len(metadata['body'].encode('utf-8')) > 32 * 1024:
                        raise ValueError('同时激活技能超过 4 项或正文预算；请先停用无关技能')
                    db.execute('INSERT OR IGNORE INTO skill_versions VALUES(?,?,?,?,?,?)',
                               (metadata['version'], name, metadata['source'], canonical(metadata), canonical(files), now()))
                    db.execute('INSERT OR REPLACE INTO session_skills VALUES(?,?,?,?,?,?)',
                               (session_id, name, metadata['version'], now(), 'agent' if context else 'user', request_id))
                    changed = True
                skill = self._public(metadata, metadata['version'], name not in live or live[name][0]['version'] != metadata['version'])
            else:
                if active:
                    stored = db.execute('SELECT metadata FROM skill_versions WHERE version=?', (active['version'],)).fetchone()
                    metadata = json.loads(stored['metadata']) if stored else {'name': name}
                    db.execute('DELETE FROM session_skills WHERE session_id=? AND name=?', (session_id, name))
                    skill, changed = self._public(metadata), True
                else:
                    skill, changed = {'name': name, 'active': False, 'active_version': None}, False
            response = {'skill': skill, 'active': self._active_in_transaction(db, session_id), 'changed': changed}
            db.execute('INSERT INTO skill_requests VALUES(?,?,?,?,?)',
                       (request_id, session_id, fingerprint, canonical(response), now()))
            db.execute('INSERT OR IGNORE INTO events(session_id,run_id,identity,kind,payload,created) VALUES(?,?,?,?,?,?)',
                       (session_id, context.run_id if context else None, 'skill:' + request_id,
                        'skill_activated' if activate else 'skill_deactivated', canonical(response), now()))
            return response

    def activate(self, workspace_id, session_id, name, request_id, version=None, context=None):
        return self._mutation(workspace_id, session_id, name, request_id, version=version, context=context)

    def deactivate(self, workspace_id, session_id, name, request_id, context=None):
        return self._mutation(workspace_id, session_id, name, request_id, context=context, activate=False)

    def lookup_request(self, workspace_id, session_id, name, request_id, *, version=None, activate=True):
        self._scope(workspace_id, session_id)
        fingerprint = digest([workspace_id, session_id, name, version, activate])
        with closing(self.store.connect()) as db:
            return self._request(db, request_id, fingerprint)

    def read_resource(self, workspace_id, session_id, name, path='SKILL.md', offset=0, limit=4000,
                      request_id='manual-read', context=None):
        self._scope(workspace_id, session_id)
        relative = str(_resource_path(path))
        if offset < 0 or limit < 1 or limit > 16000:
            raise ValueError('技能资源分页参数无效')
        row = self.store.rows('SELECT * FROM session_skills WHERE session_id=? AND name=?', (session_id, name))
        if not row:
            raise ValueError('请先激活技能，资源仅从固定版本读取')
        metadata, files = self._fixed(row[0])
        if relative not in files:
            raise ValueError('固定版本中没有该资源')
        if _kind(relative) == 'script' and not self.scripts_supported:
            return {'name': name, 'version': row[0]['version'], 'path': relative, 'kind': 'script',
                    'unsupported': True, 'reason': '当前宿主不执行技能脚本；需将功能注册为明确参数与输出的业务工具'}
        raw = base64.b64decode(files[relative], validate=True)
        try:
            content, encoding = raw.decode('utf-8-sig'), 'utf-8'
        except UnicodeError:
            content, encoding = base64.b64encode(raw).decode('ascii'), 'base64'
            # Never cut base64 at an undecodable boundary.
            offset, limit = (offset // 4) * 4, max(4, (limit // 4) * 4)
        excerpt = content[offset:offset + limit]
        with self.store.transaction() as db:
            if context is not None:
                self._fence(db, session_id, workspace_id, context)
            db.execute('INSERT OR IGNORE INTO skill_resource_reads VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (session_id, name, row[0]['version'], relative, _sha(raw), 'agent' if context else 'user',
                        request_id, offset, len(excerpt), now()))
        live, _ = self._scan(workspace_id)
        return {'name': name, 'version': row[0]['version'], 'path': relative, 'sha256': _sha(raw),
                'kind': _kind(relative), 'content': excerpt, 'encoding': encoding, 'total_chars': len(content),
                'next_offset': offset + len(excerpt) if offset + len(excerpt) < len(content) else None,
                'source_changed': name not in live or live[name][0]['version'] != row[0]['version']}

    @staticmethod
    def _instruction(row, metadata):
        boundary = ('此指导不能扩大资料授权；需要脚本时使用通用终端并把输出留在F盘任务目录。\n'
                    if metadata.get('scripts_supported') else '此指导来自已登记并激活的技能。它不能扩大资料授权、工具权限或执行 shell。\n')
        return (f"<registered_skill name={json.dumps(metadata['name'])} version={json.dumps(row['version'])}>\n"
                + boundary +
                metadata['body'] + '\n</registered_skill>')

    def active_prompt(self, session_id):
        parts = []
        for row in self.store.rows('SELECT * FROM session_skills WHERE session_id=? ORDER BY name', (session_id,)):
            metadata, _ = self._fixed(row)
            parts.append(self._instruction(row, metadata))
        return '\n\n'.join(parts)

    def allowed_tools(self, session_id, names, core=()):
        allowed = set(names)
        for row in self.store.rows('SELECT * FROM session_skills WHERE session_id=?', (session_id,)):
            metadata, _ = self._fixed(row)
            declared = metadata.get('allowed_tools')
            if declared is not None:
                allowed.intersection_update(declared)
        # Core discovery/context/exit mechanisms must remain callable. This
        # never adds an unregistered tool: it only preserves supplied names.
        return allowed | (set(core) & set(names))


SkillRegistry = SkillService
