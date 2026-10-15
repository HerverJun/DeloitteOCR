"""Provision a limited PostgreSQL login; leave bootstrap credentials untouched."""
import argparse
import getpass
import json
import os
import secrets
import subprocess
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from workbench_platform.tasks.database import f_path,check_schema

def provision(config,output):
    config=f_path(config);output=f_path(output)
    if output.exists():raise ValueError('Runtime configuration already exists')
    private=json.loads(config.read_text('utf-8-sig'))
    engine=create_engine(private['database_url']);check_schema(engine)
    password=secrets.token_urlsafe(48);role='clear_runtime_'+secrets.token_hex(5)
    try:
        with engine.begin() as db:
            # Role names and password are generated from a restricted alphabet.
            db.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'))
            database=engine.dialect.identifier_preparer.quote(engine.url.database)
            db.execute(text(f'GRANT CONNECT ON DATABASE {database} TO "{role}"'))
            db.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            db.execute(text(f'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO "{role}"'))
            db.execute(text(f'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
            db.execute(text(f'REVOKE UPDATE,DELETE ON task_events,audit_events FROM "{role}"'))
            db.execute(text(f'REVOKE INSERT,UPDATE,DELETE ON clear_alembic_version FROM "{role}"'))
            db.execute(text(f'GRANT pg_read_all_settings TO "{role}"'))
        private['database_url']=make_url(private['database_url']).set(username=role,password=password).render_as_string(hide_password=False)
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(private,indent=2),'utf-8')
        if os.name=='nt':subprocess.run(['icacls',str(output),'/inheritance:r','/grant:r',getpass.getuser()+':(F)'],capture_output=True,check=True)
        runtime=create_engine(private['database_url'])
        try:
            check_schema(runtime)
            with runtime.connect() as db:
                assert not db.execute(text('SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname=current_user')).scalar_one()
                assert not db.execute(text("SELECT has_table_privilege(current_user,'audit_events','UPDATE') OR has_table_privilege(current_user,'task_events','DELETE')")).scalar_one()
        finally:runtime.dispose()
        return {'runtime_config':str(output),'privileges_verified':True}
    finally:engine.dispose()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--admin-config',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(provision(args.admin_config,args.output)))
