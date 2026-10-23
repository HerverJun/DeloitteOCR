"""DATA-owned CPU evidence; sealed program inventories are read-only."""
from contextlib import contextmanager
import hashlib
import json
import os

from workbench_platform.resources.reader_features import require_features
from workbench_platform.resources.recovery import local_path, atomic_json


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def validate_inventory(program,kind,runtime):
    program,runtime=map(local_path,(program,runtime))
    try:
        inventory=json.loads((program/'models/multimodal/validation.json').read_text('utf-8'))
        binding=inventory[kind]
        if not isinstance(binding,dict) or not binding.get('runtime_sha256') or not isinstance(binding.get('resources'),dict) or not binding['resources']:
            raise ValueError('incomplete inventory')
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise ValueError('随程序模型资源清单缺失或版本记录无效，不能跳过身份校验准备运行') from error
    if not runtime.is_relative_to(program) or digest(runtime)!=binding['runtime_sha256']:
        raise ValueError('Native runtime changed since the sealed inventory')
    resources=binding['resources']
    for name,identity in resources.items():
        model=local_path(program/'models/multimodal'/name)
        if not model.is_relative_to(program/'models/multimodal'):
            raise ValueError('Model identity escapes the sealed inventory')
        if model.stat().st_size!=identity['bytes'] or digest(model)!=identity['sha256']:
            raise ValueError('Model resource changed since bundled inventory: '+name)
    return resources


@contextmanager
def _locked(path):
    with path.open('a+b') as stream:
        if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


class ReadinessRecordError(ValueError):
    """Inference was verified, but the optional readiness registry was not saved."""


def record_cpu_validation(source,program,data,kind,runtime,output,receipt,*,path=None):
    source,program,data,runtime,output=map(local_path,(source,program,data,runtime,output))
    path=local_path(path or data/'multimodal-cpu-validation.json')
    if not path.is_relative_to(data):raise ValueError('CPU evidence must remain in the selected DATA')
    result_path=output/'result.json'
    result=json.loads(result_path.read_text('utf-8'))
    if (result.get('device')!='cpu' or receipt.get('actual_device')!='cpu'
            or receipt.get('cleanup_confirmed') is not True or receipt.get('outcome',{}).get('status')!='succeeded'
            or result.get('network_policy')!='all_outbound_connections_denied'):
        raise ValueError('CPU qualification requires genuine successful offline inference and confirmed owned-tree exit')
    resources=validate_inventory(program,kind,runtime)
    record={'cpu':True,'gpu':False,'resources':resources,
        'adapter_sha256':digest(source/'modules/manual_multimodal/worker.py'),
        'source_hashes':{item.relative_to(source).as_posix():digest(item) for item in sorted((source/'modules/manual_multimodal').glob('*.py'))},
        'runtime_sha256':digest(runtime),'record':{**receipt,'exit_code':0,'result_sha256':digest(result_path)},
        'scope':'Owned offline CPU engineering inference; business accuracy and independent-site qualification remain pending'}
    try:
        require_features(data,'agent-multimodal-v1')
        path.parent.mkdir(parents=True,exist_ok=True)
        with _locked(local_path(path.with_suffix(path.suffix+'.lock'))):
            records=json.loads(path.read_text('utf-8')) if path.is_file() else {}
            if not isinstance(records,dict):raise ValueError('Existing CPU validation registry is malformed; preserve and verify it')
            records[kind]=record;atomic_json(path,records)
    except (OSError,ValueError) as error:
        raise ReadinessRecordError('Verified CPU result retained; readiness registry was not updated') from error
    return record
