"""Fingerprint executable platform source at worker startup and reject drift."""
import hashlib
from pathlib import Path

def fingerprint(root):
    result=hashlib.sha256()
    for directory in ('src','modules','adapters','packages'):
        for path in sorted((root/directory).rglob('*.py')):
            if '__pycache__' in path.parts:continue
            result.update(path.relative_to(root).as_posix().encode());result.update(b'\0');result.update(path.read_bytes());result.update(b'\0')
    return 'sha256:'+result.hexdigest()
