"""Exercise copied native libraries, OCR inference, Data and forensic workers."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    business = Path(sys.argv[2]).resolve()
    source, program = root / 'worktrees/platform', root / 'program'
    output = root / 'checks/physical-dependencies'
    output.mkdir(parents=True, exist_ok=True)
    resume = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    if resume and not resume.is_relative_to(output):
        raise ValueError('Resumed verification must use isolated development outputs')
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(output / 'validation-data'))
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    bundle = program / 'bundles/ocr'
    runtime_audits = {}
    probe = output / 'probe_runtime.py'
    probe.write_text('''import json,sys
from pathlib import Path
from PIL import Image
from pillow_heif import register_heif_opener
from ocr_workbench.runtime_audit import inspect_runtime
register_heif_opener()
result=inspect_runtime(Path(sys.argv[1]))
assert result['non_system_modules_inside_bundle'] is True
assert result['verified_runtime_source_modules']==[]
Path(sys.argv[2]).write_text(json.dumps(result,indent=2),encoding='utf-8')
''', encoding='utf-8')
    for engine in ('ppocr', 'paddlevl', 'glm', 'hunyuan'):
        audit = output / f'{engine}-physical-dll-audit.json'
        if not resume:
            subprocess.run([str(bundle / f'runtimes/{engine}/python.exe'), '-I', '-B', str(probe),
                            str(bundle), str(audit)], cwd=root, env=env, check=True)
        runtime_audits[engine] = json.loads(audit.read_text())
        assert runtime_audits[engine]['non_system_modules_inside_bundle'] is True
        assert Path(runtime_audits[engine]['executable']).is_relative_to(bundle)
        print(f'{engine}: real Pillow/HEIF libraries loaded entirely inside centralized bundle', flush=True)
    with sqlite3.connect((business / 'native/ocr/workbench.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        input_path, = db.execute('SELECT path FROM versions WHERE id=?',
                                ('1058d9401b444967889e4475d1694522',)).fetchone()
    original = business / 'native/ocr' / input_path
    expected = 'ccb2316c21033e03f1dccf5a8fa95e9612ccf14df775943fb7335da54ce12742'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == expected
    image = output / 'hunyuan-input.png'
    shutil.copyfile(original, image)
    inference = resume or output / ('hunyuan-inference-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    if not resume:
        print('Starting a fresh Hunyuan inference with isolated output and copied local models', flush=True)
        with (output / 'hunyuan-worker.log').open('wb') as log:
            subprocess.run([str(bundle / 'runtimes/hunyuan/python.exe'), '-I', '-B', '-m', 'ocr_workbench.worker',
                '--bundle', str(bundle), '--engine', 'hunyuan', '--image', str(image), '--output', str(inference)],
                cwd=root, env=env, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    result = json.loads((inference / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'success' and result['text'].strip()
    assert result['image']['sha256'] == expected
    assert result['runtime_audit']['non_system_modules_inside_bundle'] is True
    assert result['runtime_audit']['verified_runtime_source_modules'] == []
    assert Path(result['runtime_audit']['executable']).is_relative_to(bundle)
    assert (inference / 'result.txt').read_text(encoding='utf-8').strip()
    print(f"Fresh Hunyuan inference passed: {len(result['text'])} characters; TXT and JSON verified", flush=True)
    forensic_output = output / 'forensics-worker'
    forensic_output.mkdir(exist_ok=True)
    if not resume:
        subprocess.run([str(program / 'runtimes/forensics/python.exe'), '-I', '-B',
            str(source / 'modules/image_forensics/worker.py'), str(image), str(forensic_output),
            json.dumps(['metadata', 'noise', 'frequency']), 'hunyuan-input.png'], cwd=root, env=env, check=True)
    forensic_report = json.loads((forensic_output / 'report.json').read_text(encoding='utf-8'))
    assert forensic_report['input']['sha256'] == expected
    assert len(forensic_report['evidence']) == 3
    assert all(item['status'] == 'completed' for item in forensic_report['evidence'])
    assert (forensic_output / 'review.html').is_file()
    print('Centralized forensic Python completed three classic tools and report export', flush=True)
    data_probe = output / 'probe_data.py'
    data_probe.write_text('''import hashlib,json,sys
from pathlib import Path
import duckdb
from openpyxl import Workbook,load_workbook
from modules.data_workbench.importing.parquet import import_parquet
root=Path(sys.argv[1])
rows=[('A',3),('B',7)]
book=Workbook(); sheet=book.active
sheet.append(['key','amount'])
for row in rows: sheet.append(row)
book.save(root/'data-probe.xlsx')
assert list(load_workbook(root/'data-probe.xlsx',read_only=True).active.values)[1:]==rows
db=duckdb.connect()
db.sql("select 'A' AS item_id,3 AS amount union all select 'B',7").write_parquet(str(root/'data-probe.parquet'))
assert db.execute("select sum(amount) from read_parquet(?)",[str(root/'data-probe.parquet')]).fetchone()==(10,)
parsed=import_parquet(root/'data-probe.parquet',source_sha256=hashlib.sha256((root/'data-probe.parquet').read_bytes()).hexdigest())
assert len(parsed.rows)==2 and len(parsed.columns)==2
(root/'data-runtime.json').write_text(json.dumps({'python':sys.executable,'xlsx_readback':True,'parquet_sum':10,'application_parquet_import_rows':len(parsed.rows)}),encoding='utf-8')
''', encoding='utf-8')
    subprocess.run([str(program / 'runtimes/data/python.exe'), '-I', '-B', str(data_probe), str(output)],
                   cwd=root, env=env, check=True)
    print('Centralized Data Python completed XLSX readback and Parquet SQL aggregation', flush=True)
    receipt = {'completed_at': datetime.now(timezone.utc).isoformat(), 'program': str(program),
        'real_runtime_audits': {engine: {'all_libraries_inside_bundle': value['non_system_modules_inside_bundle'],
            'externally_registered_modules': value['verified_runtime_source_modules']} for engine,value in runtime_audits.items()},
        'fresh_hunyuan': {'status': result['status'], 'text_characters': len(result['text']),
            'elapsed_seconds': result['elapsed_seconds'], 'input_sha256': expected, 'output': str(inference),
            'txt_verified': True, 'json_verified': True},
        'forensics': {'runtime': str(program / 'runtimes/forensics/python.exe'), 'tools': 3, 'report_verified': True},
        'data': json.loads((output / 'data-runtime.json').read_text()),
        'business_data_policy': 'Read-only input copy; test outputs and caches are isolated in development checks'}
    (output / 'runtime-verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
