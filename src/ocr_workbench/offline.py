"""Offline defaults plus a Python socket audit guard (not an OS firewall)."""
import ipaddress
import os
from pathlib import Path
import sys


def configure(root: Path):
    cache = root / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    settings = {
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
        'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True',
        'PADDLE_PDX_DISABLE_DEVICE_FALLBACK': 'True',
        'PADDLE_PDX_EAGER_INIT': 'False',
        'HF_HOME': str(cache / 'hf'), 'PADDLE_PDX_CACHE_HOME': str(cache / 'paddlex'),
        'PADDLE_HOME': str(cache / 'paddle'), 'XDG_CACHE_HOME': str(cache),
        'NO_PROXY': '*', 'TOKENIZERS_PARALLELISM': 'false',
    }
    os.environ.update(settings)
    for key in list(os.environ):
        if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'PYTHONPATH', 'PYTHONHOME'}:
            del os.environ[key]


def install_guard(log: Path):
    def allowed(host):
        if host in {'localhost', None}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def audit(event, args):
        host = None
        if event == 'socket.connect':
            host = args[1][0]
        elif event == 'socket.getaddrinfo':
            host = args[0]
        else:
            return
        if not allowed(host):
            with log.open('a', encoding='utf-8') as out:
                out.write(f'BLOCKED {event} {host}\n')
            raise RuntimeError(f'Offline policy blocked {event}: {host}')
    sys.addaudithook(audit)
