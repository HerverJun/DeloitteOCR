"""Keep executable sensitive parameters encrypted, outside task metadata."""
import json
import os
import hashlib
import hmac
import secrets
from workbench_platform.agent.credentials import CredentialVault

FIELDS={'password','api_key','authorization','access_token','secret','client_secret','token'}

def request_fingerprint(value):
    """Compare complete requests without persisting secrets or guessable hashes."""
    from .database import f_path
    configured=os.environ.get('WORKBENCH_DATA_DIR') or os.path.join(os.environ['CLEAR_TASK_ROOT'],'runs','workspace')
    from packages.runtime import current_install
    installation=current_install()
    root=f_path(installation.roots['secrets'] if installation else configured);root.mkdir(parents=True,exist_ok=True)
    path=root/'task-fingerprint.key'
    if not path.exists():
        temporary=root/('fingerprint-'+secrets.token_hex(10)+'.new')
        temporary.write_bytes(CredentialVault._crypt(secrets.token_bytes(32)))
        try:os.link(temporary,path)
        except FileExistsError:pass
        finally:temporary.unlink()
    signing_key=CredentialVault._crypt(path.read_bytes(),decrypt=True)
    canonical=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    return hmac.new(signing_key,canonical,hashlib.sha256).hexdigest()
def contains_private(value):
    if isinstance(value,dict):return any(name.casefold() in FIELDS or any(part in name.casefold() for part in ('password','secret','api_key','apikey','authorization','credential','access_token','bearer_token')) or contains_private(item) for name,item in value.items())
    if isinstance(value,list):return any(contains_private(item) for item in value)
    return False

def seal(root,command):
    if not contains_private(command):return command
    if root is None:
        from .store import LedgerError
        raise LedgerError('执行参数的加密存储尚未配置',503)
    vault=CredentialVault(root/'task-command-secrets')
    reference=vault.save(json.dumps(command,ensure_ascii=False,allow_nan=False))
    return {name:command[name] for name in ('tool_id','action','method','path','groups','transport','resource_class','resolved_ai_service') if name in command}|{'private_payload_ref':reference}

def unseal(root,command):
    if not command.get('private_payload_ref'):return command
    try:value=json.loads(CredentialVault(root/'task-command-secrets').read(command['private_payload_ref']))
    except (ValueError,OSError):
        from .store import LedgerError
        raise LedgerError('加密执行参数不可读取，请重新填写后发起任务',409) from None
    return {**value,**{name:item for name,item in command.items() if name!='private_payload_ref'}}
