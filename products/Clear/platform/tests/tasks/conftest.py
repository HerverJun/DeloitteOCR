import os
import secrets
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from workbench_platform.tasks.database import migrate
from workbench_platform.tasks.store import Ledger

@pytest.fixture(scope='session')
def task_engine():
    url=make_url(os.environ['CLEAR_DATABASE_URL']).set(drivername='postgresql+psycopg')
    database='clear_task_test_'+secrets.token_hex(6)
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    with admin.connect() as db:db.execute(text(f'CREATE DATABASE "{database}"'))
    engine=create_engine(url.set(database=database),pool_size=25,max_overflow=10)
    migrate(engine)
    yield engine
    engine.dispose()
    with admin.connect() as db:db.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
    admin.dispose()

@pytest.fixture
def task_case(task_engine):
    from sqlalchemy import update
    from workbench_platform.tasks import schema_v1 as s
    with task_engine.begin() as db:db.execute(update(s.task_dispatches).values(state='done'))
    ledger=Ledger(task_engine)
    admin=ledger.create_user('admin-'+secrets.token_hex(5),'项目管理员','correct horse battery staple',admin=True)
    editor=ledger.create_user('editor-'+secrets.token_hex(5),'编制人员','correct horse battery staple')
    viewer=ledger.create_user('viewer-'+secrets.token_hex(5),'项目查看者','correct horse battery staple')
    stranger=ledger.create_user('other-'+secrets.token_hex(5),'其他项目人员','correct horse battery staple')
    workspace=ledger.create_workspace(admin,'事务与权限测试项目')['workspace_id']
    ledger.set_member(admin,workspace,editor['id'],'editor')
    ledger.set_member(admin,workspace,viewer['id'],'viewer')
    return ledger,admin,editor,viewer,stranger,workspace

def submission(workspace,request='request-a'):
    return {'workspace_id':workspace,'tool_id':'data','action':'import','idempotency_key':request,'options':{}}
