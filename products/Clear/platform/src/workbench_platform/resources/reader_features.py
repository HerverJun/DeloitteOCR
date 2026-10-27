"""Mark data only when a new-format operation is written, never on startup."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

LOCK=threading.RLock()


def require_features(root, *features):
    from packages.runtime import require_path
    root=require_path(root, 'Reader marker', role='write')
    path=root/'product-reader-requirements.json'
    with LOCK:
        value=json.loads(path.read_text('utf-8')) if path.exists() else {'format':'clear-product-reader-requirements-v1','reader_features':[]}
        if value.get('format')!='clear-product-reader-requirements-v1':raise ValueError('Unknown reader requirements marker')
        if set(features) <= set(value.get('reader_features', [])):
            return
        value['reader_features']=sorted(set(value['reader_features'])|set(features))
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.new')
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        for attempt in range(20):
            try:
                os.replace(temporary,path)
                return
            except PermissionError:
                if attempt==19:raise
                time.sleep(.05*(attempt+1))
