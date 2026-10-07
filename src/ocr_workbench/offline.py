"""Offline defaults plus a Python socket audit guard (not an OS firewall)."""
import ipaddress
import os
from pathlib import Path
import sys


def _f_cache_path(path: Path):
    requested = path.absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
        raise ValueError('OCR cache paths must remain on the local F drive')
    return resolved


def configure(root: Path):
    root = _f_cache_path(Path(root))
    cache = _f_cache_path(root / 'cache')
    cache_paths = {
        'HF_HOME': cache / 'hf', 'PADDLE_PDX_CACHE_HOME': cache / 'paddlex',
        'PADDLE_HOME': cache / 'paddle', 'XDG_CACHE_HOME': cache,
        'TORCHINDUCTOR_CACHE_DIR': cache / 'torchinductor',
        'MODELSCOPE_CACHE': cache / 'modelscope',
        'MODELSCOPE_HOME': cache / 'modelscope-home',
        'MODELSCOPE_CREDENTIALS_PATH': cache / 'modelscope-home' / 'credentials',
        'AISTUDIO_CACHE': cache / 'aistudio',
        'AISTUDIO_CACHE_HOME': cache / 'aistudio-home',
        'PADDLE_EXTENSION_DIR': cache / 'paddle-extension',
    }
    # Check every target before the first mkdir or environment update, including
    # existing junctions inside an otherwise valid F session cache.
    cache_paths = {key: _f_cache_path(path) for key, path in cache_paths.items()}
    cache.mkdir(parents=True, exist_ok=True)
    settings = {
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
        'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True',
        'PADDLE_PDX_DISABLE_DEVICE_FALLBACK': 'True',
        'PADDLE_PDX_EAGER_INIT': 'False',
        # Managed Windows workers have no username environment variables.
        # Give PyTorch its session cache without falling back to getpass/pwd.
        **{key: str(path) for key, path in cache_paths.items()},
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
