"""Run the public portal and one private execution host with explicit F storage."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit


def portal_listen_address(args):
    """Validate explicit LAN HTTP separately from loopback development mode."""
    from workbench_platform.tasks.transport import is_lan_address, public_endpoint
    endpoint=public_endpoint();parsed=urlsplit(endpoint.origin)
    if not 1<=args.port<=65535 or not 1<=args.worker_port<=65535 or args.port==args.worker_port:
        raise ValueError('Portal and private worker require distinct ports between 1 and 65535')
    if (parsed.port or (443 if parsed.scheme=='https' else 80))!=args.port:
        raise ValueError('--public-origin port must match --port')
    if args.lan_http:
        if (not args.listen_address or not is_lan_address(args.listen_address)
                or parsed.hostname!=args.listen_address or parsed.scheme!='http'):
            raise ValueError('LAN HTTP requires --listen-address with a private LAN IP and the same exact HTTP --public-origin')
    if args.development_http and (parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1'}):
        raise ValueError('Development HTTP requires a loopback HTTP public origin')
    if endpoint.insecure_http and (args.certificate or args.private_key):
        raise ValueError('HTTP mode must not be combined with TLS certificate options')
    if not endpoint.insecure_http and (not args.certificate or not args.private_key):
        raise ValueError('HTTPS requires an explicitly configured TLS certificate and key')
    address=args.listen_address or ('127.0.0.1' if args.development_http else '0.0.0.0')
    if args.development_http and address!='127.0.0.1':
        raise ValueError('Development HTTP may listen only on 127.0.0.1')
    return address

def main():
    parser=argparse.ArgumentParser()
    for name in ('program','data','private-config','dependency-layer','postgres-bin'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--mode',choices=['portal','worker'],default='portal')
    parser.add_argument('--port',type=int,default=8889);parser.add_argument('--worker-port',type=int,default=8890)
    parser.add_argument('--public-origin',default='https://localhost:8889')
    parser.add_argument('--certificate',type=Path);parser.add_argument('--private-key',type=Path)
    network=parser.add_mutually_exclusive_group()
    network.add_argument('--development-http',action='store_true')
    network.add_argument('--lan-http',action='store_true',help='Explicitly serve plaintext HTTP on a configured private LAN IP')
    parser.add_argument('--listen-address',help='Exact server LAN IP for --lan-http; must match --public-origin')
    parser.add_argument('--agent-layer',type=Path)
    parser.add_argument('--stop-worker-on-exit',action='store_true',help='Explicitly stop the execution service when closing this portal')
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[1]
    def physical(value):
        value=value.absolute()
        if value.drive.upper()!='F:' or value.resolve().drive.upper()!='F:':raise ValueError('All project files must remain on F:')
        return value.resolve()
    for name in ('program','data','private_config','dependency_layer','postgres_bin'):
        setattr(args,name,physical(getattr(args,name)))
    args.data.mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        import getpass
        subprocess.run(['icacls',str(args.data),'/inheritance:r','/grant:r',getpass.getuser()+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'],capture_output=True,check=True)
    cache=args.data/'cache';temporary=args.data/'temp'
    for folder in (cache,temporary,cache/'localappdata',cache/'appdata'):folder.mkdir(parents=True,exist_ok=True)
    os.environ.update(TEMP=str(temporary),TMP=str(temporary),TMPDIR=str(temporary),LOCALAPPDATA=str(cache/'localappdata'),APPDATA=str(cache/'appdata'),
        PYTHONDONTWRITEBYTECODE='1',WORKBENCH_DATA_DIR=str(args.data),WORKBENCH_PORTAL_DIST=str(source/'apps/portal/dist'),
        CLEAR_TASK_LAYER=str(args.dependency_layer),CLEAR_POSTGRES_BIN=str(args.postgres_bin),CLEAR_WORKER_URL=f'http://127.0.0.1:{args.worker_port}',
        CLEAR_PUBLIC_ORIGIN=args.public_origin,CLEAR_ALLOW_HTTP_LOOPBACK='1' if args.development_http else '0',CLEAR_ALLOW_LAN_HTTP='1' if args.lan_http else '0',
        PGPASSFILE=str(cache/'pgpass.conf'),PGSERVICEFILE=str(cache/'pg_service.conf'),PSQL_HISTORY=str(cache/'psql_history'))
    private=json.loads(args.private_config.read_text('utf-8-sig'))
    os.environ.update(CLEAR_DATABASE_URL=private['database_url'],CLEAR_WORKER_TOKEN=private['worker_token'])
    if args.agent_layer:
        os.environ['WORKBENCH_AGENT_LAYER']=str(physical(args.agent_layer))
        os.environ['WORKBENCH_AGENT_ENABLED']='1'
    else:os.environ['WORKBENCH_AGENT_ENABLED']='0'
    sys.path[:0]=[str(source),str(source/'src'),str(args.dependency_layer)]
    import uvicorn
    from workbench_platform.tasks.database import engine_for,check_schema
    engine=engine_for();check_schema(engine)
    if args.mode=='worker':
        from scripts.agent_native_preview import configure
        from workbench_platform.tasks.worker import create_worker_app
        app=create_worker_app(engine,initializer=lambda:configure(source,args.program,args.data))
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.worker_port,access_log=False))
        app.state.shutdown_callback=lambda:setattr(server,'should_exit',True)
        server.run();return
    from workbench_platform.tasks.server import create_app
    from workbench_platform.tasks.source_identity import fingerprint
    from workbench_platform.tasks.store import digest
    listen_address=portal_listen_address(args)
    public_app=create_app(engine)
    for file in (args.certificate,args.private_key):
        if file:physical(file)
    from datetime import datetime
    logs=args.data/'debug-logs';logs.mkdir(exist_ok=True)
    log=logs/('execution-'+datetime.now().strftime('%Y%m%d')+'.log')
    argv=[sys.executable,'-B',str(source/'scripts/task_python.py'),str(Path(__file__)),*sys.argv[1:],'--mode','worker']
    # Replace a supplied portal mode rather than append two mode arguments.
    child_argv=argv
    with log.open('ab') as output:
        import httpx
        child=None
        headers={'Authorization':'Bearer '+private['worker_token']}
        def probe():
            response=httpx.get(f'http://127.0.0.1:{args.worker_port}/internal/status',headers=headers,timeout=2)
            if response.status_code!=200:raise RuntimeError('The private port is occupied by another or older execution service; stop that service explicitly first')
            value=response.json()
            if value['root_identity']!=digest(str(args.data)) or value['database']!=engine.url.database or value['database_port']!=(engine.url.port or 5432):raise RuntimeError('Execution service belongs to another workspace or database')
            if value['source_version']!=fingerprint(source):raise RuntimeError('Execution source changed; drain and restart the execution service explicitly')
            return value
        try:probe()
        except (httpx.ConnectError,httpx.ConnectTimeout):
            child=subprocess.Popen(child_argv,env=os.environ.copy(),cwd=source,stdout=output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            for _ in range(120):
                if child and child.poll() is not None:raise RuntimeError('Execution service failed; inspect its F-drive log')
                try:
                    probe();break
                except httpx.HTTPError:pass
                time.sleep(.5)
            else:raise RuntimeError('Execution service startup timed out')
            uvicorn.run(public_app,host=listen_address,port=args.port,access_log=False,
                ssl_certfile=str(args.certificate) if args.certificate else None,ssl_keyfile=str(args.private_key) if args.private_key else None)
        finally:
            if args.stop_worker_on_exit:
                try:httpx.post(f'http://127.0.0.1:{args.worker_port}/internal/shutdown',headers=headers,timeout=5)
                except httpx.HTTPError:pass
                if child:
                    try:child.wait(timeout=30)
                    except subprocess.TimeoutExpired:raise RuntimeError('Execution service is still stopping; no forced termination was performed')

if __name__=='__main__':main()
