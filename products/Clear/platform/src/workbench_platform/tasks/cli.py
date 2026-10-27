"""Explicit migration, bootstrap, execution, backup and verification commands."""
import argparse
import getpass
import json
import os
from pathlib import Path
from .database import engine_for, migrate, check_schema, f_path

def main():
    parser=argparse.ArgumentParser(prog='clear-tasks')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('migrate')
    admin=sub.add_parser('create-admin'); admin.add_argument('--username',required=True); admin.add_argument('--name',required=True); admin.add_argument('--password-file',type=Path)
    for name,port in [('serve',8889),('worker',8890)]:
        p=sub.add_parser(name);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=port)
    legacy=sub.add_parser('import-legacy');legacy.add_argument('--root',type=Path,required=True)
    for name in ('backup','verify-backup','restore','retention','benchmark'):
        p=sub.add_parser(name);p.add_argument('--root',type=Path,required=True)
        if name=='restore':p.add_argument('--destination',type=Path,required=True)
        if name=='benchmark':p.add_argument('--count',type=int,default=1_000_000)
        if name=='retention':p.add_argument('--apply',action='store_true')
    args=parser.parse_args(); engine=engine_for()
    if args.command=='migrate': migrate(engine); print('Central task schema migrated.');return
    check_schema(engine)
    if args.command=='create-admin':
        from .store import Ledger
        password=f_path(args.password_file).read_text('utf-8').strip() if args.password_file else getpass.getpass('Admin password: ')
        user=Ledger(engine).create_user(args.username,args.name,password,admin=True)
        print(json.dumps(user,ensure_ascii=False));return
    if args.command in {'serve','worker'}:
        import uvicorn
        if args.command=='serve':
            from .server import create_app
            app=create_app(engine)
        else:
            if args.host!='127.0.0.1':raise ValueError('The private worker must listen only on 127.0.0.1')
            if not 1<=args.port<=65535:raise ValueError('Invalid private worker port')
            os.environ['CLEAR_WORKER_URL']=f'http://127.0.0.1:{args.port}'
            from .worker import create_worker_app
            app=create_worker_app(engine)
        uvicorn.run(app,host=args.host,port=args.port,access_log=False);return
    if args.command=='import-legacy':
        from .legacy import import_legacy
        print(json.dumps(import_legacy(engine,f_path(args.root)),ensure_ascii=False));return
    if args.command=='benchmark':
        from .benchmark import run
        print(json.dumps(run(engine,f_path(args.root),args.count),ensure_ascii=False));return
    from . import maintenance
    result=getattr(maintenance,args.command.replace('-','_'))(engine,f_path(args.root),**({'destination':f_path(args.destination)} if args.command=='restore' else {'apply':args.apply} if args.command=='retention' else {}))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
