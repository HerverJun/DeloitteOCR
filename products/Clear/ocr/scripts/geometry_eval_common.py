"""Immutable evaluation locks and hashed artifact receipts (evaluation only)."""
import hashlib
import json
from pathlib import Path
import platform
import sys

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),'utf-8');temp.replace(path)


def file_lock(paths):return {str(Path(p).resolve()):sha(p) for p in sorted(map(Path,paths))}


def mapping_code_lock():
    dependencies=[*(ROOT/'src/ocr_workbench').rglob('*.py')]
    dependencies += [ROOT/'scripts'/name for name in ('replay_geometry_v2.py','report_geometry_v2.py',
        'geometry_eval_common.py','geometry_reference_identity.py','infer_geometry_real_results.py',
        'evaluate_geometry_gpu.py','evaluate_rapid_reference.py','evaluate_geometry_pages.py')]
    dependencies += [ROOT/'config/geometry-matching-policy.json',ROOT/'config/geometry-evaluation.json',
        ROOT/'src/ocr_workbench/_vendor/tableformer/upstream-lock.json']
    return file_lock(dependencies)


def verify_test_seal(manifest, path):
    if manifest.get('split') != 'test':return None
    if not path:raise ValueError('Seal code, policy and adoption rule before test inference')
    sealed=json.loads(Path(path).read_text('utf-8'))
    if sealed['code']!=mapping_code_lock():raise ValueError('Test code differs from sealed selection')
    return sha(path)


def ensure_lock(path,lock):
    path=Path(path)
    if path.exists():
        if json.loads(path.read_text('utf-8'))!=lock:raise ValueError('Run lock mismatch: create a NEW output run; never overwrite a lock and reuse results')
    else:
        if path.parent.exists() and any(path.parent.iterdir()):raise ValueError('Unsealed output directory is not reusable')
        write_json(path,lock)
    return sha(path)


def verify_inputs(manifest_path):
    manifest_path=Path(manifest_path);manifest=json.loads(manifest_path.read_text('utf-8'))
    if manifest.get('protocol_version')==2:
        if any('targets' in s or 'box' in s for s in manifest['samples']):raise ValueError('Annotations must not enter v2 inference input')
        split_path=manifest_path.parent.parent/'split-lock.json'
        lock=json.loads(split_path.read_text('utf-8'))
        relative=str(manifest_path.relative_to(split_path.parent)).replace('\\','/')
        declared={k.replace('\\','/'):v for k,v in lock['files'].items()}
        if declared.get(relative)!=sha(manifest_path):raise ValueError('Frozen input manifest hash mismatch')
    ids=set()
    for sample in manifest['samples']:
        if sample['id'] in ids or Path(sample['id']).name!=sample['id']:raise ValueError('Duplicate or unsafe sample ID')
        ids.add(sample['id'])
        if sha(manifest_path.parent/sample['image'])!=sample['sha256']:raise ValueError('Frozen image hash mismatch')
    return manifest


def verified_marker(folder,lock_hash,artifact_names):
    marker=folder/'evaluation.json'
    if not marker.exists():return None
    value=json.loads(marker.read_text('utf-8'))
    if value.get('run_lock_sha256')!=lock_hash:raise ValueError('Unverified evaluation marker')
    receipts=value.get('artifact_sha256',{})
    if value['status']=='success' and not set(artifact_names)<=set(receipts):raise ValueError('Missing artifact receipt')
    for name,digest in receipts.items():
        if Path(name).name!=name or not (folder/name).exists() or sha(folder/name)!=digest:raise ValueError('Evaluation artifact missing or changed')
    return value


def runtime_info():
    return {'python':sys.version,'platform':platform.platform(),'processor':platform.processor()}
