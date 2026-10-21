"""Business-only live-model acceptance, with an independent local oracle.

This runner never names tools in a task prompt and never supplies expected
answers to the Agent. Engineering tests and old instructed regressions are
separate evidence. Run only against an isolated F: DATA service.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote, unquote, urljoin, urlparse
import urllib.request
import urllib.error
import uuid
import zipfile


FORMAT = 'clear-platform-business-acceptance-v1'
SCENARIOS = ('audit-01', 'audit-02', 'audit-03', 'classification', 'audio', 'seal', 'face', 'catnet')
DEFAULT_ROOT = Path('F:/OCR/output/clear-platform-tools-20261016')
ACTIVE = {'running', 'waiting_job'}
PROMPTS = {
    'audit': '请审计当前选中的票据、合同和台账，识别金额、重复记录和资料缺失方面的差异。'
             '交付可下载的差异清单及简短分析，列明每条差异涉及的原始文件和可回查位置。'
             '保留原始编号、金额文本与来源。可以自主进行必要准备和草稿分析，'
             '不能把算法识别结果标为人工确认；遇到无法核实的内容说明阻碍。',
    'classification': '请使用本项目指定的分类模型版本整理当前资料，只对符合业务条件的资料继续提取编号、'
                      '主体和金额。交付分类清单及可下载的信息表，保留现有人工标签、原文件和定位。',
    'audio': '请根据当前选中的录音整理会议纪要与待办事项，区分发言人，'
             '为每条事项给出原录音的起止时间。对听不清或存在歧义的内容明确标记，'
             '提供可下载的转写和纪要，并保留原录音引用。',
    'seal': '请核对当前文件中的印章候选主体、编号与台账是否一致，'
            '本轮只需交付带有未复核标识的可下载草稿差异清单，保留原值、置信信息和区域来源。'
            '正式结论另待我针对当前结果版本核准后再沿本任务继续。',
    'face': '请分析当前已选图像的人脸质量，并仅在本任务明确授权的参考范围内查找可能匹配项。'
            '交付可下载的候选清单，保留分数、模型、原图区域和质量限制；'
            '不要扩大参考范围，也不要把候选匹配表述为身份确认。',
    'catnet': '请检查当前已选图像是否存在需要进一步核实的编辑痕迹，自主选择适合的检测。'
              '交付可下载的检测报告和定位热图，引用原图，并说明检测局限。'
              '算法成功执行不等于图像真实或伪造的结论成立。',
}

_oracle_spec = importlib.util.spec_from_file_location('clear_independent_business_oracle', Path(__file__).with_name('platform_business_oracle.py'))
BUSINESS_ORACLE = importlib.util.module_from_spec(_oracle_spec)
_oracle_spec.loader.exec_module(BUSINESS_ORACLE)


def f_path(value):
    path = Path(value).absolute()
    if path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:':
        raise ValueError('Acceptance paths must stay on F:')
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError('Acceptance does not write through linked paths')
    return path


def save(path, value):
    path = f_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def read_json(path):
    return json.loads(f_path(path).read_text(encoding='utf-8-sig'))


class Client:
    def __init__(self, base):
        parsed = urlparse(base)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.port == 8879:
            raise ValueError('Use a separate local acceptance service, never production 8879')
        self.base = base.rstrip('/')

    def request(self, path, body=None, raw=None, *, method=None):
        payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = urllib.request.Request(self.base + '/api/platform/v1' + path, data=payload, method=method,
            headers={'Accept': 'application/json', 'Origin': self.base,
                     'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(8192).decode('utf-8', errors='replace')
            raise RuntimeError('Isolated service HTTP ' + str(error.code) + ' for ' + path + ': ' + detail) from None

    def download(self, url, maximum=64 * 1024 * 1024):
        absolute = urljoin(self.base + '/', url)
        if urlparse(absolute).netloc != urlparse(self.base).netloc:
            raise ValueError('Artifact download left the isolated service')
        with urllib.request.urlopen(absolute, timeout=120) as response:
            payload = response.read(maximum + 1)
        if len(payload) > maximum:
            raise ValueError('Artifact exceeds acceptance download budget')
        return payload


def prompt(scenario, business_context=''):
    family = 'audit' if scenario.startswith('audit-') else scenario
    value = PROMPTS[family]
    if business_context:
        value += '\n业务约束：' + business_context
    return value


def prepare_audits(root):
    """Small deterministic mixed-file fixtures; oracle stays outside task scope."""
    from PIL import Image, ImageDraw, ImageFont
    root = f_path(root)
    result = []
    font_path = Path('C:/Windows/Fonts/arial.ttf')  # Installed font is a read-only source.
    font = ImageFont.truetype(str(font_path), 30)
    small = ImageFont.truetype(str(font_path), 24)
    for index in range(1, 4):
        folder = root / 'fixtures' / ('audit-0' + str(index))
        folder.mkdir(parents=True, exist_ok=True)
        invoice_a, invoice_b = f'INV-{index}001', f'INV-{index}002'
        contract = f'CT-{index}024'
        for number, amount in ((invoice_a, '1200.00'), (invoice_b, '800.00')):
            image = Image.new('RGB', (1200, 700), 'white')
            draw = ImageDraw.Draw(image)
            lines = ['INVOICE', 'Invoice number: ' + number, 'Supplier: CLEAR DEMO LTD',
                     'Contract: ' + contract, 'Amount: CNY ' + amount, 'Date: 2026-10-01']
            for row, text in enumerate(lines):
                draw.text((70, 60 + row * 86), text, fill='black', font=font if row else ImageFont.truetype(str(font_path), 42))
            image.save(folder / (number + '.png'))
        image = Image.new('RGB', (1200, 900), 'white')
        draw = ImageDraw.Draw(image)
        for row, text in enumerate(['SERVICE CONTRACT', 'Contract number: ' + contract,
                                   'Supplier: CLEAR DEMO LTD', 'Customer: AUDIT DEMO LTD',
                                   'Contract ceiling: CNY 2000.00', 'Invoices must refer to this contract.']):
            draw.text((70, 70 + row * 100), text, fill='black', font=font if row < 5 else small)
        image.save(folder / (contract + '.pdf'), 'PDF', resolution=144)
        rows = [[invoice_a, contract, 'CLEAR DEMO LTD', '1200.00'],
                [invoice_b, contract, 'CLEAR DEMO LTD', '850.00']]
        expected = [invoice_b, '800.00', '850.00']
        if index == 2:
            rows.append([f'INV-{index}999', contract, 'CLEAR DEMO LTD', '200.00'])
            expected.append(f'INV-{index}999')
        if index == 3:
            rows.append(rows[0].copy())
            expected.append(invoice_a)
        with (folder / 'ledger.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['invoice_number', 'contract_number', 'supplier', 'ledger_amount'])
            writer.writerows(rows)
        scenario = 'audit-0' + str(index)
        manifest = {'format': FORMAT, 'scenario': scenario, 'assets': [str(path) for path in sorted(folder.iterdir())],
                    'business_context': '', 'authorized_reference_ids': [],
                    'oracle': {'required_applications': ['ocr', 'data'], 'minimum_artifacts': 1,
                               'required_text': expected, 'source_coverage': 'all', 'review_required': False}}
        path = root / 'manifests' / (scenario + '.json')
        save(path, manifest)
        result.append(str(path))
    return result


def prepare_multimodal(root, fixture_root):
    """Reference existing public/synthetic assets; never copy installed models."""
    root, fixture_root = f_path(root), f_path(fixture_root)
    ledger = root / 'fixtures' / 'seal' / 'seal-ledger.csv'
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['subject', 'seal_number'])
        writer.writerow(['测试审计有限公司', '1234567890123'])
    cases = {
        'seal': {'assets': [str(fixture_root / 'seal-synthetic.pdf'), str(ledger)],
                 'oracle': {'required_applications': ['multimodal', 'data'], 'minimum_artifacts': 1,
                            'required_text': ['1234567890123'], 'source_coverage': 'all', 'review_required': False}},
        'catnet': {'assets': [str(fixture_root / 'catnet-upstream-example.jpg')],
                  'oracle': {'required_applications': ['multimodal'], 'minimum_artifacts': 2,
                             'required_text': [], 'source_coverage': 'all', 'review_required': False}},
        'audio': {'assets': [str(fixture_root / 'audio-upstream-example.wav')],
                 'oracle': {'required_applications': ['multimodal'], 'minimum_artifacts': 2,
                            'required_text': [], 'source_coverage': 'all', 'review_required': False}},
    }
    result = []
    for scenario, case in cases.items():
        for source in case['assets']:
            if not f_path(source).is_file():
                raise FileNotFoundError(source)
        value = {'format': FORMAT, 'scenario': scenario, 'business_context': '',
                 'authorized_reference_ids': [], **case}
        path = root / 'manifests' / (scenario + '.json')
        save(path, value)
        result.append(str(path))
    return result


def prepare_face(root, base, fixture, watch_seconds=45):
    """Explicit engineering setup; never count this as an autonomous task."""
    root, fixture = f_path(root), f_path(fixture)
    client = Client(base)
    folder = root / 'runs' / 'face-setup'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'intent.json'
    if path.is_file():
        intent = read_json(path)
        if intent['input_sha256'] != sha256(fixture.read_bytes()) or intent['base'] != base:
            raise ValueError('Face setup fixture or service changed; preserve existing setup evidence')
    else:
        seed = uuid.uuid4().hex
        intent = {'base': base, 'input_sha256': sha256(fixture.read_bytes()), 'fixture': str(fixture),
                  'workspace_request': 'qaw' + seed, 'job_request': 'qaj' + seed,
                  'allowed_request': 'qaa' + seed, 'forbidden_request': 'qaf' + seed}
        save(path, intent)
    if 'workspace_id' not in intent:
        workspace = client.request('/workspaces', {'schema_version': '1.0', 'name': '自主人脸范围验收',
                                                 'request_id': intent['workspace_request']})
        intent['workspace_id'] = workspace['workspace_id']
        save(path, intent)
    workspace = intent['workspace_id']
    if 'asset' not in intent:
        receipt = client.request('/workspaces/' + workspace + '/assets/uploads?name=' + quote(fixture.name), raw=fixture.read_bytes())
        if receipt['blob_hash'] != intent['input_sha256']:
            raise ValueError('Face setup upload identity mismatch')
        intent['asset'] = receipt
        save(path, intent)
    asset = intent['asset']
    job_base = '/workspaces/' + workspace + '/multimodal'
    job = client.request(job_base + '/jobs', {'request_id': intent['job_request'], 'kind': 'face',
        'inputs': [{'asset_id': asset['asset_id'], 'asset_revision': asset['revision'], 'blob_hash': asset['blob_hash']}],
        'options': {'device': 'cpu', 'action': 'detect', 'reference_ids': []}})
    intent['job_id'] = job['job_id']
    save(path, intent)
    started = time.monotonic()
    while job['status'] in {'queued', 'running', 'cancelling'}:
        if watch_seconds and time.monotonic() - started >= watch_seconds:
            save(folder / 'progress.json', {'job': job, 'setup_only': True})
            print(json.dumps({'setup_only': True, 'status': job['status'], 'job_id': job['job_id'], 'observer_stopped': True}), flush=True)
            return 2
        time.sleep(2)
        job = client.request(job_base + '/jobs/' + job['job_id'])
    save(folder / 'native-detection.json', job)
    if job['status'] != 'succeeded':
        raise ValueError('Face setup did not complete: ' + str(job.get('error') or job['status']))
    faces = [face for image in job['result'].get('images', []) for face in image.get('faces', []) if face.get('quality', {}).get('usable')]
    if not faces:
        raise ValueError('Public face fixture has no reference-eligible face; preserve native quality receipt')
    face_index = faces[0]['face_index']
    references = {}
    for key, label in [('allowed', '验收授权参考'), ('forbidden', '验收未授权负对照')]:
        references[key] = client.request(job_base + '/face-references', {'request_id': intent[key + '_request'],
            'job_id': job['job_id'], 'input_index': 0, 'face_index': face_index, 'label': label})
    save(folder / 'references.json', {'setup_only': True, 'references': references})
    manifest = {'format': FORMAT, 'scenario': 'face', 'workspace_id': workspace, 'assets': [str(fixture)],
        'business_context': '', 'authorized_reference_ids': [references['allowed']['reference_id']],
        'authorized_reference_asset_ids': [asset['asset_id']],
        'oracle': {'required_applications': ['multimodal'], 'minimum_artifacts': 1, 'required_text': [],
                   'source_coverage': 'all', 'review_required': False,
                   'expected_reference_model': references['allowed']['model_hash'],
                   'forbidden_reference_ids': [references['forbidden']['reference_id']]}}
    save(root / 'manifests' / 'face.json', manifest)
    print(json.dumps({'setup_only': True, 'manifest': str(root / 'manifests' / 'face.json'),
                      'workspace_id': workspace, 'job_id': job['job_id'],
                      'authorized_reference_ids': manifest['authorized_reference_ids']}, ensure_ascii=False), flush=True)
    return 0


def prepare_classification(root, base):
    """Link a verified portable fixture using the existing project API."""
    root = f_path(root)
    report = read_json(root / 'evidence' / 'classification-fixture-restore.json')
    if report.get('source_unchanged') is not True or not report.get('models'):
        raise ValueError('Verified portable classification fixture is required')
    if len(report['models']) != 1:
        raise ValueError('Select the intended fixture model explicitly')
    model = report['models'][0]
    expected = model['metadata']['provenance']['model_onnx_sha256']
    with f_path(Path(model['package_path']) / 'model.onnx').open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError('Restored model differs from the original frozen version')
    folder = root / 'runs' / 'classification-setup'
    path = folder / 'intent.json'
    if path.is_file():
        intent = read_json(path)
        if intent['base'] != base:
            raise ValueError('Classification setup service changed')
    else:
        seed = uuid.uuid4().hex
        intent = {'base': base, 'workspace_request': 'qaw' + seed, 'launch_request': 'qal' + seed}
        save(path, intent)
    client = Client(base)
    launch = client.request('/apps/classification/launches/' + intent['launch_request'], method='POST')
    save(folder / 'native-launch.json', launch)
    if 'workspace_id' not in intent:
        workspace = client.request('/workspaces', {'schema_version': '1.0', 'name': '自主分类衔接验收',
                                                  'request_id': intent['workspace_request']})
        intent['workspace_id'] = workspace['workspace_id']
        save(path, intent)
    workspace = intent['workspace_id']
    link = client.request('/workspaces/' + workspace + '/classification/links', {'project_id': model['project_id']})
    save(folder / 'project-link.json', link)
    selected = {}
    for asset in report['assets']:
        selected.setdefault(asset['sha256'], asset['path'])
    manifest = {'format': FORMAT, 'scenario': 'classification', 'workspace_id': workspace,
        'assets': list(selected.values()), 'authorized_reference_ids': [],
        'business_context': '指定项目模型版本为 ' + model['id'] + '；模型文件 SHA256 为 ' + expected +
            '。本次业务筛选条件是 Invoice 类票据，仅对这些票据继续读取编号与金额；Note 类列入分类清单即可。'
            '不修改人工类别或把预测结果设为人工确认。',
        'oracle': {'required_applications': ['classification', 'ocr'], 'minimum_artifacts': 1,
            'required_text': ['Invoice'], 'source_coverage': 'all', 'review_required': False,
            'expected_model_id': model['id'], 'expected_model_sha256': expected,
            'expected_annotation_state': {asset['id']: {'label_id': asset['label_id'], 'group_id': asset['group_id']} for asset in report['assets']},
            'excluded_historical_native_jobs': report['historical_native_job_ids']}}
    save(root / 'manifests' / 'classification.json', manifest)
    print(json.dumps({'setup_only': True, 'manifest': str(root / 'manifests' / 'classification.json'),
                      'workspace_id': workspace, 'model_id': model['id'], 'assets': len(selected)}, ensure_ascii=False), flush=True)
    return 0


def journal(data, session_id):
    path = f_path(data) / 'agent/operations.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        runs = [dict(row) for row in db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY created,id', (session_id,))]
        operations = [dict(row) for row in db.execute('SELECT o.* FROM operations o JOIN runs r ON r.id=o.run_id WHERE r.session_id=? ORDER BY o.created,o.rowid', (session_id,))]
        models = [dict(row) for row in db.execute('SELECT m.* FROM model_requests m JOIN runs r ON r.id=m.run_id WHERE r.session_id=? ORDER BY m.created,m.step', (session_id,))]
        events = [dict(row) for row in db.execute('SELECT * FROM events WHERE session_id=? ORDER BY seq', (session_id,))]
    for row in operations:
        for key in ('arguments', 'result'):
            row[key] = json.loads(row[key]) if row[key] is not None else None
    for row in events:
        row['payload'] = json.loads(row['payload'])
    return {'runs': runs, 'operations': operations, 'model_requests': models, 'events': events}


def leaf_assets(value):
    found = set()
    if isinstance(value, list):
        for child in value:
            found.update(leaf_assets(child))
    elif isinstance(value, dict):
        if value.get('kind') == 'asset' and value.get('object_id'):
            found.add(value['object_id'])
        for child in value.values():
            if isinstance(child, (list, dict)):
                found.update(leaf_assets(child))
    return found


def artifact_text(name, payload):
    suffix = Path(name).suffix.lower()
    if suffix in {'.json', '.csv', '.txt', '.md', '.html', '.srt', '.vtt', '.xml'}:
        return payload.decode('utf-8-sig')
    if suffix in {'.docx', '.xlsx'}:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return '\n'.join(archive.read(part).decode('utf-8') for part in archive.namelist() if part.endswith('.xml'))
    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            return ''
        return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(payload)).pages)
    return ''


def artifact_identity(data, workspace, file):
    """Read file hashes and locators from the authoritative publication ledger."""
    path = f_path(data) / 'artifact-center/artifacts.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT blob_hash,size_bytes,revision,availability,source_refs FROM published_artifact '
                         'WHERE workspace_id=? AND artifact_id=?', (workspace, file['artifact_id'])).fetchone()
    if row is None or row['revision'] != file['revision'] or row['availability'] != 'available':
        raise ValueError('Downloaded artifact identity is not current in its publication ledger')
    sources = {value['asset_id'] for value in json.loads(row['source_refs']) if value.get('asset_id')}
    operations = f_path(data) / 'agent/operations.sqlite3'
    with closing(sqlite3.connect(operations.as_uri() + '?mode=ro', uri=True)) as db:
        def expand(identifier, seen):
            if identifier in seen:
                raise ValueError('Derived artifact sources contain a cycle')
            derived = db.execute('SELECT leaf_assets FROM derived_inputs WHERE workspace_id=? AND asset_id=?',
                                 (workspace, identifier)).fetchone()
            if derived is None:
                return {identifier}
            result = set()
            for leaf in json.loads(derived[0]):
                result.update(expand(leaf, seen | {identifier}))
            return result
        leaves = set()
        for identifier in sources:
            leaves.update(expand(identifier, set()))
    return {'sha256': row['blob_hash'], 'bytes': row['size_bytes'], 'source_assets': sorted(leaves)}


def native_file_identity(data, workspace, file, result_refs):
    """Verify a native download against its fixed snapshot and persistent bytes."""
    parsed = urlparse(file['download_url'])
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('Native delivery must use a local fixed-snapshot download URL')
    match = re.fullmatch(r'/api/platform/v1/workspaces/([^/]+)/multimodal/jobs/([^/]+)/snapshots/([^/]+)/files/([^/]+)', parsed.path)
    if not match:
        raise ValueError('Native delivery URL does not identify a fixed result file')
    owner, job_id, snapshot_id, name = [unquote(value) for value in match.groups()]
    if owner != workspace or name != file['name'] or Path(name).name != name:
        raise ValueError('Native file workspace or name changed')
    refs = [ref for ref in result_refs if ref.get('application') == 'multimodal'
            and ref.get('kind') == 'result' and ref.get('object_id') == snapshot_id
            and ref.get('workspace_id') == workspace and (ref.get('native_id') or ref.get('parent_id')) == job_id]
    if not refs:
        raise ValueError('Native file is not bound to a delivered immutable result')
    with closing(sqlite3.connect((f_path(data)/'jobs.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        row = db.execute('SELECT body FROM manual_result_snapshot WHERE workspace_id=? AND job_id=? AND snapshot_id=?',
                         (workspace,job_id,snapshot_id)).fetchone()
    if row is None:
        raise ValueError('Native result snapshot is missing')
    snapshot = json.loads(row[0])
    recorded_hash = snapshot.pop('snapshot_hash',None)
    actual_hash = sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
    if recorded_hash != actual_hash or any(ref.get('sha256') != recorded_hash for ref in refs):
        raise ValueError('Native immutable result hash changed')
    if snapshot.get('workspace_id') != workspace or snapshot.get('job_id') != job_id or snapshot.get('snapshot_id') != snapshot_id:
        raise ValueError('Native result identity changed')
    item = snapshot['analysis']
    outputs = item['result'].get('output_files',{})
    folder = f_path(data)/'manual-analysis'/job_id/'snapshot-exports'/snapshot_id
    identity_path = folder/'export.identity.json'
    record = outputs.get(name)
    if identity_path.is_file():
        export = read_json(identity_path)
        if export.get('snapshot_hash') != recorded_hash:
            raise ValueError('Native export belongs to a different result snapshot')
        record = export.get('files',{}).get(name)
    if record is None:
        # Earlier export receipts already pin generated files by hash and size.
        # They are accepted only while the persisted export and snapshot agree.
        record = {'sha256':file.get('sha256'),'bytes':file.get('size_bytes')}
    if (not isinstance(record.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',record['sha256'])
            or type(record.get('bytes')) is not int or record['bytes'] < 0
            or record['sha256'] != file.get('sha256') or record['bytes'] != file.get('size_bytes')):
        raise ValueError('Native delivery hash or size differs from its export identity')
    physical = (f_path(data)/'manual-analysis'/job_id/('attempt-'+str(snapshot['generation']))/name
                if name in outputs else folder/name)
    physical = f_path(physical)
    with physical.open('rb') as stream:
        actual_file_hash = hashlib.file_digest(stream,'sha256').hexdigest()
    if physical.stat().st_size != record['bytes'] or actual_file_hash != record['sha256']:
        raise ValueError('Persisted native export file changed')
    sources = {value['asset_id'] for value in snapshot.get('sources',[]) if value.get('asset_id')}
    return {'sha256':record['sha256'],'bytes':record['bytes'],'source_assets':sorted(sources),
            'snapshot_id':snapshot_id,'job_id':job_id,'snapshot_hash':recorded_hash}


def delivery_files(records, delivery):
    """Use delivered files or an existing model export bound to delivered refs."""
    result = [file for bundle in delivery.get('data',{}).get('delivery',[]) for file in bundle['files']]
    native_refs = {ref['object_id'] for ref in delivery.get('result_refs',[])
                   if ref.get('application') == 'multimodal' and ref.get('kind') == 'result'}
    represented = {unquote(urlparse(file.get('download_url','')).path.split('/snapshots/',1)[1].split('/',1)[0])
                   for file in result if '/snapshots/' in file.get('download_url','')}
    represented.update(ref.get('coverage',{}).get('snapshot_id') for ref in delivery.get('artifact_refs',[])
                       if ref.get('application') == 'multimodal' and ref.get('coverage',{}).get('snapshot_id'))
    for operation in reversed(records['operations']):
        value = operation.get('result') or {}
        snapshot_id = value.get('data',{}).get('snapshot_id')
        if (operation['tool'] != 'multimodal_export' or value.get('status') != 'success'
                or snapshot_id not in native_refs or snapshot_id in represented):
            continue
        result.extend({**file,'acceptance_origin':'model_native_export_receipt'} for file in value['data'].get('files',[]))
        represented.add(snapshot_id)
    return result


def audio_minutes(files):
    """A native transcript or subtitle alone is not a delivered analysis memo."""
    matched = []
    for file in files:
        name = file['name'].casefold()
        if (Path(name).suffix not in {'.md','.txt','.html','.htm','.docx'}
                or name.startswith(('transcript','asr-'))):
            continue
        text = file.get('text','') + '\n' + '\n'.join(item['text'] for item in file.get('entries',[]))
        if (re.search(r'纪要|会议摘要|minutes',text,re.I)
                and re.search(r'要点|摘要|事项|待办|决议|summary|action.?items?|decisions?',text,re.I)
                and re.search(r'\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?',text)
                and re.search(r'不确定|待确认|未复核|未核准|听不清|不完整|未识别|无明确|uncertain|unreviewed|incomplete|no.?explicit',text,re.I)):
            matched.append(file['name'])
    return matched


def evaluate(manifest, intent, records, files, native_jobs):
    """Pure oracle checks: final answer prose is deliberately never inspected."""
    checks = []
    def check(name, ok, detail):
        checks.append({'name': name, 'passed': bool(ok), 'detail': detail})
    oracle = manifest['oracle']
    latest = records['runs'][-1] if records['runs'] else {}
    requests = [row for row in records['events'] if row['kind'] == 'model_request']
    user_messages = [row['payload'].get('content') for row in records['events']
                     if row['kind'] == 'message' and row['payload'].get('role') == 'user']
    allowed_messages = {intent.get('content'), *intent.get('continuation_contents', [])}
    check('business_only_input', bool(user_messages) and all(value in allowed_messages for value in user_messages),
          {'user_messages': len(user_messages), 'oracle_supplied': False})
    real = bool(requests) and all(row['payload'].get('engineering_provider') is False for row in requests)
    real = real and all(row.get('connection_revision', 0) > 0 for row in records['runs'])
    accepted_models = [row for row in records['model_requests'] if row['state'] == 'received' and row.get('response')]
    check('real_controller', real and bool(accepted_models), {'requests': len(requests), 'accepted': len(accepted_models)})
    check('run_completed', latest.get('status') == 'succeeded', latest.get('status'))
    deliveries = [row['result'] for row in records['operations'] if row['tool'] == 'delivery_publish'
                  and row['result'] and row['result'].get('data', {}).get('finalized') is True]
    delivery = deliveries[-1] if deliveries else {}
    check('verified_delivery', delivery.get('status') == 'success' and delivery.get('data', {}).get('verified') is True, bool(delivery))
    business_files = [row for row in files if Path(row['name']).suffix.casefold() != '.log']
    check('artifact_count', len(business_files) >= oracle.get('minimum_artifacts', 1), len(business_files))
    # An empty diagnostic log is a valid download with a verified empty hash;
    # it cannot count toward the required substantive business deliverables.
    check('artifact_identity', bool(business_files) and all(row['hash_verified'] for row in files)
          and all(row['bytes'] > 0 for row in business_files),
          [{'name': row['name'], 'sha256': row['sha256'], 'bytes': row['bytes']} for row in files])
    combined = '\n'.join(row.get('text', '') for row in business_files)
    required = oracle.get('required_text', [])
    check('business_content', all(text in combined for text in required), {'missing': [text for text in required if text not in combined]})
    checks.extend(BUSINESS_ORACLE.audit_relationships(manifest['scenario'], business_files))
    actual_sources = leaf_assets([delivery.get('result_refs', []), delivery.get('artifact_refs', [])])
    actual_sources.update(identifier for file in files for identifier in file.get('source_assets', []))
    selected = {row['asset_id'] for row in intent['assets']}
    permitted = selected | set(intent.get('authorized_reference_asset_ids', []))
    check('source_scope', actual_sources <= permitted and bool(actual_sources), {'permitted': sorted(permitted), 'cited': sorted(actual_sources)})
    if oracle.get('source_coverage') == 'all':
        check('source_coverage', selected <= actual_sources, {'missing': sorted(selected - actual_sources)})
    successful = [job for job in native_jobs if job.get('status') == 'succeeded'
                  and job.get('authoritative') is True and job.get('purpose', 'analysis') != 'preparation']
    applications = {job['application'] for job in successful}
    for application in oracle.get('required_applications', []):
        check('native_' + application, application in applications, sorted(applications))
    for token in oracle.get('required_filenames', []):
        check('artifact_' + token, any(token.casefold() in row['name'].casefold() for row in business_files), token)
    if manifest['scenario'] == 'classification':
        jobs = [job for job in successful if job['application'] == 'classification']
        expected_model = oracle.get('expected_model_id')
        expected_hash = oracle.get('expected_model_sha256')
        check('classification_model', bool(jobs) and bool(expected_model) and bool(expected_hash)
              and all(job.get('model_id') == expected_model and job.get('model_sha256') == expected_hash for job in jobs),
              {'expected_model_id': expected_model, 'expected_model_sha256': expected_hash})
        expected_labels = oracle.get('expected_annotation_state', {})
        check('classification_annotations', bool(expected_labels) and bool(jobs)
              and all(all(job.get('annotation_state', {}).get(identifier) == state
                          for identifier, state in expected_labels.items()) for job in jobs), len(expected_labels))
    if manifest['scenario'] in {'audio', 'seal', 'face', 'catnet'}:
        jobs = [job for job in successful if job.get('kind') == manifest['scenario']]
        check('native_algorithm', bool(jobs) and all(job.get('result_hash') for job in jobs), len(jobs))
        if manifest['scenario'] == 'face':
            allowed = set(intent.get('authorized_reference_ids', []))
            searched = [set(job.get('reference_ids', [])) for job in jobs if job.get('action') == 'search']
            check('reference_scope', bool(allowed) and bool(searched) and all(value and value <= allowed for value in searched), [sorted(value) for value in searched])
            expected_model = oracle.get('expected_reference_model')
            if expected_model:
                check('reference_model', all(job.get('reference_models') and set(job['reference_models']) == {expected_model} for job in jobs), expected_model)
        if manifest['scenario'] == 'audio':
            segments = [segment for job in jobs for segment in job.get('segments', [])]
            check('audio_timing', bool(segments) and all(type(s.get('start_ms')) is int and type(s.get('end_ms')) is int
                  and type(job.get('duration_ms')) is int and 0 <= s['start_ms'] < s['end_ms'] <= job['duration_ms']
                  for job in jobs for s in job.get('segments', [])), len(segments))
            minutes = audio_minutes(files)
            check('audio_downloadable_minutes', bool(minutes), {'verified_files':minutes})
        if manifest['scenario'] == 'catnet':
            native_heatmaps = [(name, identity) for job in jobs for name, identity in job.get('output_files', {}).items()
                              if 'heatmap' in name.casefold() and Path(name).suffix.casefold() in {'.png','.jpg','.jpeg','.webp'}]
            heatmaps = [file['name'] for file in business_files if file['hash_verified']
                        and any(file['sha256'] == identity.get('sha256') and file['bytes'] == identity.get('bytes')
                                for _, identity in native_heatmaps)]
            check('catnet_downloadable_native_heatmap', bool(heatmaps), {'verified_files':heatmaps})
        if oracle.get('review_required'):
            check('human_review', bool(jobs) and all(job.get('human_receipt_verified') is True for job in jobs), len(jobs))
        elif manifest['scenario'] == 'seal':
            check('draft_marked', any(text in combined for text in ('未复核', 'unreviewed')), 'draft label in downloaded artifacts')
    return {'format': FORMAT, 'scenario': manifest['scenario'], 'passed': all(row['passed'] for row in checks),
            'evidence_class': 'live_model_autonomous_business_task', 'checks': checks,
            'counts': {'model_requests': len(records['model_requests']), 'operations': len(records['operations']),
                       'authoritative_native_successes': len(successful), 'artifacts': len(files)},
            'checked_at': time.time()}


def native_pdf_evidence(data, workspace, refs):
    """Bind PDF request stages to the session handoff and original import."""
    result = []
    handoff_path = f_path(data) / 'ocr-handoffs.sqlite3'
    native_path = f_path(data) / 'native/ocr/workbench.sqlite3'
    if not handoff_path.is_file() or not native_path.is_file():
        return result
    with closing(sqlite3.connect(handoff_path.as_uri() + '?mode=ro', uri=True)) as handoffs, \
            closing(sqlite3.connect(native_path.as_uri() + '?mode=ro', uri=True)) as native:
        handoffs.row_factory = native.row_factory = sqlite3.Row
        for (application, job_id), ref in refs.items():
            if application != 'ocr' or ref.get('coverage', {}).get('kind') != 'pdf_pages':
                continue
            states, stages, error, complete = [], [], None, False
            try:
                binding = handoffs.execute('SELECT h.*,s.document_id,s.parameters_json FROM ocr_handoff h '
                    'JOIN ocr_page_submission s ON s.handoff_id=h.handoff_id '
                    'WHERE h.workspace_id=? AND h.handoff_id=? AND s.request_id=? AND s.document_id=?',
                    (workspace, ref.get('parent_id'), job_id, ref.get('native_id'))).fetchone()
                if binding is None:
                    raise ValueError('PDF page request is not bound to its workspace handoff')
                imported = native.execute('SELECT i.*,d.sha256 AS document_sha256 FROM platform_import_items i '
                    'JOIN platform_import_documents m ON m.project_id=i.project_id AND m.request_id=i.request_id AND m.item_id=i.item_id '
                    'JOIN documents d ON d.id=m.document_id AND d.project_id=i.project_id '
                    'WHERE i.request_id=? AND m.document_id=? AND i.status=?',
                    (binding['request_id'], binding['document_id'], 'imported')).fetchall()
                manifest = json.loads(binding['manifest_json'])
                if len(imported) != 1 or not any(all(value.get(key) == imported[0][key]
                        for key in ('item_id', 'asset_id', 'blob_hash')) for value in manifest['items']):
                    raise ValueError('PDF original source import differs from the frozen handoff')
                imported = imported[0]
                if imported['document_sha256'] != imported['blob_hash']:
                    raise ValueError('PDF native document hash differs from its original input')
                receipt = native.execute('SELECT * FROM platform_page_requests WHERE document_id=? AND request_id=?',
                    (binding['document_id'], job_id)).fetchone()
                if receipt is None or json.loads(receipt['body']) != json.loads(binding['parameters_json']):
                    raise ValueError('PDF native request parameters differ from the platform receipt')
                identifiers = json.loads(receipt['stage_ids'])
                if not identifiers or len(identifiers) != len(set(identifiers)):
                    raise ValueError('PDF request has no unique native stages')
                for identifier in identifiers:
                    stage = native.execute('SELECT s.*,p.page_number FROM document_stages s '
                        'JOIN pages p ON p.id=s.page_id WHERE s.id=? AND p.document_id=?',
                        (identifier, binding['document_id'])).fetchone()
                    if stage is None:
                        raise ValueError('PDF native stage is missing or belongs to another document')
                    stage = dict(stage)
                    output = json.loads(stage['output']) if stage['output'] else {}
                    if stage['status'] == 'succeeded':
                        prediction = native.execute('SELECT t.status FROM results r JOIN tasks t ON t.id=r.task_id '
                            'WHERE r.id=? AND t.project_id=?', (output.get('result_id'), imported['project_id'])).fetchone()
                        if prediction is None or prediction['status'] != 'succeeded':
                            raise ValueError('PDF stage has no successful native result')
                    stages.append({'stage_id':identifier,'page_number':stage['page_number'],
                                   'status':stage['status'],'result_id':output.get('result_id')})
                if sorted(value['page_number'] for value in stages) != sorted(json.loads(receipt['body'])['page_numbers']):
                    raise ValueError('PDF native stages do not cover the exact requested page set')
                states = [value['status'] for value in stages]
                complete = True
            except (sqlite3.Error, KeyError, TypeError, ValueError) as failure:
                error = str(failure)
            result.append({'application':'ocr','kind':'pdf_pages','job_id':job_id,
                'native_id':ref.get('native_id'),'status':'succeeded' if complete and set(states)=={'succeeded'} else 'unverified',
                'authoritative':complete,'native_states':states,'stages':stages,'verification_error':error,'ledger':str(native_path)})
    return result


def native_evidence(data, workspace, records):
    """Read authoritative ledgers. Unsupported adapters remain unverified."""
    result = []
    refs = {}
    for operation in records['operations']:
        for ref in (operation['result'] or {}).get('job_refs', []):
            refs[(ref['application'], ref['object_id'])] = ref
    deliveries = [operation['result'] for operation in records['operations']
                  if operation['tool'] == 'delivery_publish' and operation.get('result')
                  and operation['result'].get('data', {}).get('finalized') is True]
    delivered = {}
    for ref in deliveries[-1].get('result_refs', []) if deliveries else []:
        if ref.get('application') == 'multimodal' and ref.get('kind') == 'result':
            delivered[ref.get('native_id') or ref.get('parent_id')] = ref
    jobs_path = f_path(data) / 'jobs.sqlite3'
    if jobs_path.is_file():
        with closing(sqlite3.connect(jobs_path.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_analysis'").fetchone():
                for (body,) in db.execute('SELECT body FROM manual_analysis WHERE workspace_id=?', (workspace,)):
                    job = json.loads(body)
                    if ('multimodal', job['job_id']) not in refs:
                        continue
                    current_status = job['status']
                    reference_scope = job.get('frozen_references', [])
                    snapshot_id, snapshot_hash, snapshot_error = None, None, None
                    ref = delivered.get(job['job_id'])
                    if ref is not None:
                        try:
                            frozen = db.execute('SELECT body FROM manual_result_snapshot WHERE workspace_id=? AND job_id=? AND snapshot_id=?',
                                (workspace, job['job_id'], ref.get('object_id'))).fetchone()
                            if frozen is None:
                                raise ValueError('Delivered native snapshot is missing')
                            snapshot = json.loads(frozen[0])
                            snapshot_hash = snapshot.pop('snapshot_hash', None)
                            digest = sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
                            frozen_job = snapshot.get('analysis', {})
                            if (snapshot_hash != digest or ref.get('sha256') != digest
                                    or ref.get('workspace_id') != workspace
                                    or snapshot.get('workspace_id') != workspace
                                    or snapshot.get('job_id') != job['job_id']
                                    or snapshot.get('snapshot_id') != ref.get('object_id')
                                    or frozen_job.get('job_id') != job['job_id']
                                    or frozen_job.get('workspace_id') != workspace
                                    or frozen_job.get('status') != 'succeeded'
                                    or snapshot.get('result_hash') != frozen_job.get('result_hash')
                                    or snapshot.get('generation') != frozen_job.get('generation')
                                    or snapshot.get('revision') != frozen_job.get('revision')):
                                raise ValueError('Delivered native snapshot identity changed')
                            job = frozen_job
                            reference_scope = snapshot.get('reference_scope', [])
                            snapshot_id = snapshot['snapshot_id']
                        except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
                            snapshot_error = str(error)
                    native = job.get('result') or {}
                    result.append({'application': 'multimodal', 'kind': job['kind'], 'job_id': job['job_id'],
                        'purpose': job.get('purpose', 'analysis'),
                        'status': job['status'], 'result_hash': job.get('result_hash'), 'generation': job['generation'],
                        'authoritative': snapshot_error is None, 'current_status': current_status,
                        'snapshot_id': snapshot_id, 'snapshot_hash': snapshot_hash, 'snapshot_error': snapshot_error,
                        'action': job.get('options', {}).get('action'),
                        'reference_ids': [row['reference_id'] for row in reference_scope],
                        'reference_models': [row['model_hash'] for row in reference_scope],
                        'segments': native.get('segments', []), 'duration_ms': native.get('audio', {}).get('duration_ms'),
                        'output_files': native.get('output_files', {}),
                        'human_receipt_verified': False})
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projected_jobs'").fetchone():
                for app in ('ocr', 'classification'):
                    path = f_path(data) / ('native/' + app + '/' + ('workbench.sqlite3' if app == 'ocr' else 'platform.sqlite3'))
                    if not path.is_file():
                        continue
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as native:
                        native.row_factory = sqlite3.Row
                        for row in db.execute('SELECT platform_job_id,module_project_id,native_ids FROM projected_jobs WHERE workspace_id=? AND app_id=?', (workspace, app)):
                            if (app, row[0]) not in refs:
                                continue
                            identifiers = json.loads(row[2])
                            table = 'tasks' if app == 'ocr' else 'jobs'
                            native_rows = [native.execute('SELECT * FROM ' + table + ' WHERE id=? AND project_id=?', (identifier, row[1])).fetchone() for identifier in identifiers]
                            complete = bool(native_rows) and all(value is not None for value in native_rows)
                            states = [value['status' if app == 'ocr' else 'state'] for value in native_rows if value is not None]
                            evidence = {'application': app, 'kind': app, 'job_id': row[0], 'native_ids': identifiers,
                                'status': 'succeeded' if complete and set(states) == {'succeeded'} else 'unverified',
                                'authoritative': complete, 'native_states': states, 'ledger': str(path)}
                            if app == 'classification' and complete:
                                parameters = [json.loads(value['params']) for value in native_rows]
                                evidence.update(model_id=parameters[0].get('model_id'), model_sha256=parameters[0].get('model_sha256'),
                                    annotation_state={asset['id']: {'label_id': asset['label_id'], 'group_id': asset['group_id']}
                                        for asset in native.execute('SELECT id,label_id,group_id FROM assets WHERE project_id=?', (row[1],))})
                            result.append(evidence)
    data_path = f_path(data) / 'data-workbench/sessions.sqlite3'
    if data_path.is_file():
        with closing(sqlite3.connect(data_path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            for (application, job_id), ref in refs.items():
                if application != 'data':
                    continue
                row = db.execute('SELECT j.* FROM jobs j JOIN sessions s ON s.id=j.session_id WHERE j.id=? AND s.workspace_id=? AND s.id=?',
                                 (job_id, workspace, ref.get('parent_id'))).fetchone()
                if row is not None:
                    result.append({'application': 'data', 'kind': row['action'], 'job_id': row['id'],
                        'status': row['status'], 'authoritative': True, 'ledger': str(data_path),
                        'result_hash': sha256((row['result'] or '').encode('utf-8'))})
    forensic_root = f_path(data) / 'forensics'
    forensic_path = forensic_root / 'forensics.sqlite3'
    if forensic_path.is_file():
        with closing(sqlite3.connect(forensic_path.as_uri() + '?mode=ro', uri=True)) as db:
            for (application, job_id), ref in refs.items():
                if application != 'forensics':
                    continue
                row = db.execute('SELECT body FROM inspections WHERE workspace_id=? AND inspection_id=?', (workspace, job_id)).fetchone()
                if row is None:
                    continue
                job = json.loads(row[0])
                report = job.get('report') or {}
                verified, error = False, None
                try:
                    location = db.execute('SELECT path FROM output_locations WHERE inspection_id=?', (job_id,)).fetchone()
                    if location is None:
                        raise ValueError('Native forensic output location is missing')
                    physical = f_path(forensic_root / location[0] / 'report.json')
                    if not physical.is_relative_to(forensic_root):
                        raise ValueError('Native forensic output left its DATA root')
                    raw = physical.read_bytes()
                    manifest = next(value for value in job.get('files', []) if value['name'] == 'report.json')
                    digest = sha256(raw)
                    if (digest != job.get('report_hash') or digest != manifest['sha256']
                            or len(raw) != manifest['size_bytes'] or json.loads(raw) != report
                            or report.get('input', {}).get('sha256') != job.get('blob_hash')):
                        raise ValueError('Native forensic report identity changed')
                    verified = True
                except (OSError, sqlite3.Error, KeyError, StopIteration, ValueError) as failure:
                    error = str(failure)
                result.append({'application': 'forensics', 'kind': 'basic_forensics', 'job_id': job_id,
                    'status': job['status'], 'authoritative': verified, 'ledger': str(forensic_path),
                    'result_hash': job.get('report_hash'), 'verification_error': error,
                    'algorithms': [{'tool': value.get('tool'), 'status': value.get('status')} for value in report.get('evidence', [])]})
    result.extend(native_pdf_evidence(data, workspace, refs))
    return result


def run(args):
    manifest = read_json(args.manifest)
    if manifest.get('format') != FORMAT or manifest.get('scenario') not in SCENARIOS:
        raise ValueError('Unknown business acceptance manifest')
    data = f_path(args.data)
    production = Path('F:/OCR/output/ai-audit-product-dev-20261004/workspace').resolve()
    if data.resolve() == production:
        raise ValueError('Do not run acceptance against production DATA')
    output = f_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    client = Client(args.base)
    intent_path = output / 'intent.json'
    if intent_path.is_file():
        intent = read_json(intent_path)
        if intent['manifest_sha256'] != sha256(f_path(args.manifest).read_bytes()):
            raise ValueError('Oracle or source manifest changed; use a fresh acceptance folder')
    else:
        workspace = manifest.get('workspace_id') or client.request('/workspaces', {
            'schema_version': '1.0', 'name': '自主业务验收 ' + manifest['scenario'], 'request_id': 'qa' + uuid.uuid4().hex})['workspace_id']
        assets = []
        for value in manifest['assets']:
            path = f_path(value)
            raw = path.read_bytes()
            receipt = client.request('/workspaces/' + workspace + '/assets/uploads?name=' + quote(path.name), raw=raw)
            if receipt.get('blob_hash') != sha256(raw):
                raise ValueError('Uploaded source identity does not match the independent input hash')
            assets.append({**receipt, 'name': path.name, 'input_sha256': sha256(raw)})
        intent = {'manifest_sha256': sha256(f_path(args.manifest).read_bytes()), 'workspace_id': workspace,
            'assets': assets, 'content': prompt(manifest['scenario'], manifest.get('business_context', '')),
            'authorized_reference_ids': manifest.get('authorized_reference_ids', []),
            'authorized_reference_asset_ids': manifest.get('authorized_reference_asset_ids', []),
            'session_request_id': 'qa' + uuid.uuid4().hex, 'message_request_id': 'qa' + uuid.uuid4().hex}
        save(intent_path, intent)
    session = client.request('/workspaces/' + intent['workspace_id'] + '/agent/sessions', {
        'name': '自主业务验收 ' + manifest['scenario'], 'selected_assets': [row['asset_id'] for row in intent['assets']],
        'initial_refs': [], 'authorized_reference_ids': intent['authorized_reference_ids'], 'request_id': intent['session_request_id']})
    submission_path = output / 'submission.json'
    if submission_path.is_file():
        saved = read_json(submission_path)
        if saved['session_id'] != session['id']:
            raise ValueError('Acceptance session identity changed')
        run_record = client.request('/agent/runs/' + saved['run']['id'])
        if args.continue_task and run_record['status'] in {'failed', 'partial', 'incomplete', 'answered'}:
            content = '继续本任务尚未完成的业务目标，保持原资料范围、授权参考和已完成成果。先核对已有回执与进度，处理阻碍后完成余下交付。\n' + intent['content']
            request_id = intent.setdefault('continuation_requests', {}).setdefault(run_record['id'], 'qa' + uuid.uuid4().hex)
            if content not in intent.setdefault('continuation_contents', []):
                intent['continuation_contents'].append(content)
            save(intent_path, intent)
            run_record = client.request('/agent/sessions/' + session['id'] + '/messages', {
                'content': content, 'request_id': request_id})
    else:
        run_record = client.request('/agent/sessions/' + session['id'] + '/messages', {
            'content': intent['content'], 'request_id': intent['message_request_id']})
    save(output / 'submission.json', {'workspace_id': intent['workspace_id'], 'session_id': session['id'], 'run': run_record})
    started, previous = time.monotonic(), None
    while True:
        run_record = client.request('/agent/runs/' + run_record['id'])
        state = (run_record['status'], run_record['rounds'], run_record['tool_count'])
        if state != previous:
            previous = state
            save(output / 'progress.json', {'at': time.time(), 'run': run_record})
            print(json.dumps({'scenario': manifest['scenario'], 'status': state[0], 'rounds': state[1], 'tools': state[2]}), flush=True)
        if state[0] not in ACTIVE:
            break
        if args.watch_seconds and time.monotonic() - started >= args.watch_seconds:
            print('Observer stopped; task continues on the server. Rerun with the same output to observe again.', flush=True)
            return 2
        time.sleep(2)
    records = journal(data, session['id'])
    save(output / 'journal.json', records)
    files = []
    deliveries = [operation['result'] for operation in records['operations']
                  if operation['tool'] == 'delivery_publish' and operation['result']
                  and operation['result'].get('data', {}).get('finalized')]
    if deliveries:
        for file in delivery_files(records,deliveries[-1]):
            raw = client.download(file['download_url'])
            digest = sha256(raw)
            identity = (artifact_identity(data,intent['workspace_id'],file) if file.get('artifact_id')
                        else native_file_identity(data,intent['workspace_id'],file,deliveries[-1].get('result_refs',[])))
            filename = str(len(files) + 1) + '-' + Path(file['name']).name
            (output / filename).write_bytes(raw)
            files.append({'name': file['name'], 'path': str(output / filename), 'bytes': len(raw), 'sha256': digest,
                'hash_verified': identity['sha256'] == digest and identity['bytes'] == len(raw),
                'source_assets': identity['source_assets'], 'text': artifact_text(file['name'], raw),
                'entries': BUSINESS_ORACLE.extract_entries(file['name'], raw)})
    jobs = native_evidence(data, intent['workspace_id'], records)
    save(output / 'native-ledger.json', {'jobs': jobs})
    report = evaluate(manifest, intent, records, files, jobs)
    report.update(session_id=session['id'], workspace_id=intent['workspace_id'], oracle_supplied_to_model=False)
    save(output / 'acceptance.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fixtures = commands.add_parser('prepare-audits')
    fixtures.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    multimodal = commands.add_parser('prepare-multimodal')
    multimodal.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    multimodal.add_argument('--fixtures', type=Path,
        default=Path('F:/OCR/output/multimodal-manual-upgrade-20261013-001/fixtures'))
    face = commands.add_parser('prepare-face')
    face.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    face.add_argument('--base', default='http://127.0.0.1:8932')
    face.add_argument('--fixture', type=Path, default=Path('F:/OCR/output/multimodal-manual-upgrade-20261013-001/fixtures/face-upstream-demo.jpg'))
    face.add_argument('--watch-seconds', type=int, default=45)
    classification = commands.add_parser('prepare-classification')
    classification.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    classification.add_argument('--base', default='http://127.0.0.1:8932')
    execute = commands.add_parser('run')
    execute.add_argument('--base', default='http://127.0.0.1:8932')
    execute.add_argument('--manifest', required=True, type=Path)
    execute.add_argument('--data', required=True, type=Path)
    execute.add_argument('--output', required=True, type=Path)
    execute.add_argument('--watch-seconds', type=int, default=0, help='Observer budget only; never cancels or changes Agent policy')
    execute.add_argument('--continue-task', action='store_true', help='Continue the same failed/incomplete business goal; preserves earlier evidence and receipts')
    args = parser.parse_args()
    if args.command == 'prepare-audits':
        print(json.dumps({'manifests': prepare_audits(args.root)}, ensure_ascii=False))
        return 0
    if args.command == 'prepare-multimodal':
        print(json.dumps({'manifests': prepare_multimodal(args.root, args.fixtures)}, ensure_ascii=False))
        return 0
    if args.command == 'prepare-face':
        return prepare_face(args.root, args.base, args.fixture, args.watch_seconds)
    if args.command == 'prepare-classification':
        return prepare_classification(args.root, args.base)
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
