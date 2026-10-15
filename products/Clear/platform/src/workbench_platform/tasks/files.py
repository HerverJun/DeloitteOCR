"""File-backed response envelopes and immutable execution artifacts."""
import hashlib
from pathlib import Path
import re
from urllib.parse import unquote
from .database import f_path
from .store import key, LedgerError


def hash_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def response_name(headers):
    disposition = headers.get('content-disposition', '')
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if not match:
        match = re.search(r'filename="?([^";]+)', disposition, re.I)
    name = Path(unquote(match[1]).replace('\\', '/')).name if match else ''
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:180]
    if not name:
        media = headers.get('content-type', '').split(';')[0]
        suffix = {'application/zip': '.zip', 'application/pdf': '.pdf', 'text/csv': '.csv',
                  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                  'text/plain': '.txt', 'application/json': '.json'}.get(media, '.bin')
        name = 'export' + suffix
    return name


def envelope(root, status, headers, data, *, force_file=False):
    value = {'status': status, 'headers': headers}
    if force_file or len(data) > 65536:
        folder = f_path(root) / 'task-response-files'
        folder.mkdir(exist_ok=True)
        target = folder / key('response')
        target.write_bytes(data)
        value.update(response_file=str(target), size_bytes=len(data), sha256=hash_file(target))
    else:
        value['content_hex'] = data.hex()
    return value


def response_path(root, value):
    path = f_path(value['response_file'])
    allowed = f_path(root) / 'task-response-files'
    if not path.is_relative_to(allowed) or not path.is_file() or hash_file(path) != value['sha256']:
        raise LedgerError('执行回执文件不可用或校验失败', 409)
    return path


def response_bytes(root, value):
    return response_path(root, value).read_bytes() if value.get('response_file') else bytes.fromhex(value.get('content_hex', ''))


def public_response(root, value):
    from fastapi.responses import FileResponse, Response
    if value.get('response_file'):
        return FileResponse(response_path(root, value), status_code=value['status'], headers=value['headers'])
    return Response(response_bytes(root, value), status_code=value['status'], headers=value['headers'])
