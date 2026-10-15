"""Immutable tested AI profiles with independent DPAPI credential references."""
import json
from sqlalchemy import select,insert,update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from . import schema_v1 as s
from .store import digest,key,LedgerError,safe

def capture_current(engine,legacy,root,make_current=False):
    agent=getattr(legacy.state,'agent',None)
    if not agent:return None
    config=agent.connection._read()
    if config.get('deleted') or not config.get('credential_ref'):
        if make_current:
            with engine.begin() as db:db.execute(update(s.ai_service_profiles).values(enabled=False))
        return None
    # A credential rotation creates a new immutable connection revision, even
    # when the endpoint and model stay the same.
    configuration={k:v for k,v in config.items() if k!='credential_ref'}
    identity='ai'+digest(configuration)[:32]
    with engine.connect() as db:
        saved=db.execute(select(s.ai_service_profiles).where(s.ai_service_profiles.c.id==identity)).mappings().first()
    if saved:
        if make_current:
            with engine.begin() as db:
                db.execute(update(s.ai_service_profiles).values(enabled=False))
                db.execute(update(s.ai_service_profiles).where(s.ai_service_profiles.c.id==identity).values(enabled=True))
            return {**saved,'enabled':True}
        return dict(saved)
    from workbench_platform.agent.credentials import CredentialVault
    vault=CredentialVault(root/'ai-credentials')
    reference=vault.save(agent.connection.vault.read(config['credential_ref']))
    from urllib.parse import urlsplit
    label=config['model']+' · '+str(urlsplit(config['base_url']).hostname or config['protocol'])
    profile=dict(id=identity,name=label[:200],provider=config['protocol'],model=config['model'],credential_ref=reference,configuration=configuration)
    with engine.begin() as db:
        if make_current:db.execute(update(s.ai_service_profiles).values(enabled=False))
        inserted=db.execute(pg_insert(s.ai_service_profiles).values(**profile).on_conflict_do_nothing(index_elements=['id']).returning(s.ai_service_profiles.c.id)).first()
        if make_current:db.execute(update(s.ai_service_profiles).where(s.ai_service_profiles.c.id==identity).values(enabled=True))
        saved=db.execute(select(s.ai_service_profiles).where(s.ai_service_profiles.c.id==identity)).mappings().one()
    if not inserted:vault.delete(reference)
    return dict(saved)

def activate(engine,legacy,root,profile_id):
    with engine.connect() as db:profile=db.execute(select(s.ai_service_profiles).where(s.ai_service_profiles.c.id==profile_id,s.ai_service_profiles.c.enabled.is_(True))).mappings().first()
    if not profile:raise LedgerError('AI 服务不存在或已停用',409)
    agent=getattr(legacy.state,'agent',None)
    if not agent:raise LedgerError('Agent 执行环境未启用',503)
    current=agent.connection._read()
    if current.get('deleted') or {k:v for k,v in current.items() if k!='credential_ref'}!=profile['configuration']:
        raise LedgerError('AI 服务配置已变化，请选择管理员当前连接并新建任务',409)
    return dict(profile)
