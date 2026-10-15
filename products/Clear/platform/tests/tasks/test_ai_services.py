from types import SimpleNamespace
import pytest
from sqlalchemy import select
from workbench_platform.agent.credentials import CredentialVault
from workbench_platform.tasks.ai_services import capture_current,activate
from workbench_platform.tasks import schema_v1 as s
from workbench_platform.tasks.store import LedgerError


def test_connection_rotation_and_deletion_never_reactivate_old_credentials(task_case,tmp_path):
    ledger,*_=task_case
    vault=CredentialVault(tmp_path/'controller-credentials')
    config={'revision':1,'protocol':'openai_chat_completions','base_url':'http://127.0.0.1:9999/v1',
            'model':'local-test','tested_at':'2026-10-15T01:00:00Z','credential_ref':vault.save('first')}
    connection=SimpleNamespace(_read=lambda:dict(config),vault=vault)
    legacy=SimpleNamespace(state=SimpleNamespace(agent=SimpleNamespace(connection=connection)))
    first=capture_current(ledger.engine,legacy,tmp_path,make_current=True)
    assert activate(ledger.engine,legacy,tmp_path,first['id'])['id']==first['id']
    config.update(revision=2,credential_ref=vault.save('second'))
    # Even before the publication hook, a stale enabled profile cannot change
    # the shared controller back to its former credentials.
    with pytest.raises(LedgerError):activate(ledger.engine,legacy,tmp_path,first['id'])
    second=capture_current(ledger.engine,legacy,tmp_path,make_current=True)
    assert second['id']!=first['id']
    assert CredentialVault(tmp_path/'ai-credentials').read(first['credential_ref'])=='first'
    with pytest.raises(LedgerError):activate(ledger.engine,legacy,tmp_path,first['id'])
    assert activate(ledger.engine,legacy,tmp_path,second['id'])['id']==second['id']
    config.update(revision=3,deleted=True)
    assert capture_current(ledger.engine,legacy,tmp_path,make_current=True) is None
    with ledger.engine.connect() as db:
        assert not db.execute(select(s.ai_service_profiles.c.id).where(s.ai_service_profiles.c.enabled.is_(True))).first()
    with pytest.raises(LedgerError):activate(ledger.engine,legacy,tmp_path,second['id'])
