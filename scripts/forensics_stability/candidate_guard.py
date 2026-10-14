"""Acceptance-only guard for candidate app and its actual Python descendants.

The guard is diagnostic defense in depth, not an OS firewall. Standard-library
bootstrap imports precede the audit hook. Python application imports must come
from the candidate; writes are limited to explicit run-owned F-drive roots.
"""
from __future__ import annotations
import json
import ipaddress
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

ENV_KEY = 'CLEAR_FORENSICS_ACCEPT_GUARD'


def normalized(value):
    return os.path.normcase(os.path.abspath(os.fsdecode(value))).replace('\\','/').rstrip('/').casefold()


def contains(path, root):
    return path == root or path.startswith(root+'/')


def install(config=None):
    config = config or json.loads(os.environ[ENV_KEY])
    if getattr(sys, '_clear_forensics_accept_guard', False):
        return getattr(sys, '_clear_forensics_accept_blocked', [])
    candidate = normalized(config['candidate'])
    forbidden = [normalized(value) for value in config['forbidden_roots']]
    exceptions = {normalized(value) for value in config.get('harness_files', [])}
    writes = [normalized(value) for value in config['write_roots']]
    violation_log = Path(config['violation_log'])
    blocked = []

    def forbidden_path(path):
        return path not in exceptions and any(contains(path, root) for root in forbidden)

    def writable(path):
        return any(contains(path, root) for root in writes)

    def reject(event, path):
        item = {'event':event, 'path':path, 'pid':os.getpid(), 'time':time.time()}
        blocked.append(item)
        try:
            with violation_log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(item,ensure_ascii=False)+'\n')
        except OSError:
            pass
        raise RuntimeError('Candidate acceptance blocked '+event+': '+path)

    def path_value(value):
        if isinstance(value,(str,bytes,os.PathLike)):
            text=os.fsdecode(value)
            if text.startswith('file:'):
                text=unquote(urlsplit(text).path)
                if len(text)>2 and text[0]=='/' and text[2]==':':text=text[1:]
            if text.casefold() in {'nul','nul:',r'\\.\nul'}:return None
            return normalized(text)
        return None

    def is_loopback(address):
        host=address[0] if isinstance(address,tuple) and address else address
        if host=='localhost':return True
        try:return ipaddress.ip_address(host).is_loopback
        except (ValueError,TypeError):return False

    def audit(event, args):
        if event in {'socket.connect','socket.getaddrinfo','socket.sendto'}:
            address=args[0] if event=='socket.getaddrinfo' else args[-1] if args else None
            # Windows asyncio creates its internal socketpair over loopback.
            # Candidate in-process API control remains local; external sockets
            # are still rejected, and inference workers also deny networking.
            if not is_loopback(address):reject(event, 'external_network')
        if event in {'open','os.listdir','os.scandir','ctypes.dlopen'} and args:
            bare_dll = event == 'ctypes.dlopen' and isinstance(args[0],str) and not any(mark in args[0] for mark in ('/','\\',':'))
            path = None if bare_dll else path_value(args[0])
            if path and forbidden_path(path):reject('development_read',path)
            if event == 'open' and path:
                mode = args[1] if len(args)>1 else ''
                flags = args[2] if len(args)>2 else 0
                writing = (isinstance(mode,str) and any(character in mode for character in 'wax+')) or (
                    isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
                if writing and not writable(path):reject('write_outside_run',path)
        if event in {'os.mkdir','os.remove','os.rmdir','os.chmod','os.utime'} and args:
            path=path_value(args[0])
            if path and not writable(path):reject('mutation_outside_run',path)
        if event in {'os.rename','os.replace'}:
            for value in args[:2]:
                path=path_value(value)
                if path and not writable(path):reject('rename_outside_run',path)
        if event in {'os.link','os.symlink'} and len(args)>1:
            source,target=path_value(args[0]),path_value(args[1])
            if source and forbidden_path(source):reject('development_read',source)
            if target and not writable(target):reject('link_outside_run',target)
        if event == 'sqlite3.connect' and args:
            path=path_value(args[0])
            if path and path != normalized(':memory:') and not writable(path):reject('database_outside_run',path)

    for value in sys.path:
        if value and forbidden_path(normalized(value)):
            raise RuntimeError('Interpreter search path still refers to development source: '+value)
    sys.addaudithook(audit)
    sys._clear_forensics_accept_guard = True
    sys._clear_forensics_accept_blocked = blocked
    original_popen = subprocess.Popen
    guard_path = str(Path(__file__).resolve())

    class GuardedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            command=args
            if isinstance(args,(list,tuple)) and args:
                values=[os.fspath(value) if isinstance(value,os.PathLike) else value for value in args]
                executable=normalized(kwargs.get('executable') or values[0])
                if str(values[0]).casefold().endswith(('python.exe','pythonw.exe')):
                    if not contains(executable,candidate):reject('python_outside_candidate',executable)
                    if '--code' not in values and '--script' not in values and '--module' not in values:
                        if '-c' in values:
                            index=values.index('-c')
                            command=[*values[:index],guard_path,'--code',*values[index+1:]]
                        elif '-m' in values:
                            index=values.index('-m')
                            command=[*values[:index],guard_path,'--module',*values[index+1:]]
                        else:
                            index=next((i for i,value in enumerate(values[1:],1)
                                if isinstance(value,str) and value.casefold().endswith('.py')),None)
                            if index is None:reject('unwrapped_python_command',repr(values))
                            command=[*values[:index],guard_path,'--script',*values[index:]]
            super().__init__(command,*positional,**kwargs)
    subprocess.Popen=GuardedPopen
    return blocked


def main():
    install()
    if len(sys.argv)<3:raise ValueError('Guard requires --script, --code or --module')
    mode,value=sys.argv[1:3]
    if mode=='--script':
        sys.argv=sys.argv[2:]
        runpy.run_path(value,run_name='__main__')
    elif mode=='--code':
        sys.argv=['-c',*sys.argv[3:]]
        exec(compile(value,'<candidate-acceptance-bootstrap>','exec'),{'__name__':'__main__'})
    elif mode=='--module':
        sys.argv=[value,*sys.argv[3:]]
        runpy.run_module(value,run_name='__main__',alter_sys=True)
    else:raise ValueError('Unknown guard mode')


if __name__=='__main__':main()
