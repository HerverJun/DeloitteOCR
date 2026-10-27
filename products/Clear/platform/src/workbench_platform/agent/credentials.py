"""DPAPI and URL normalization adapted from evaluated OCR helpers."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit, urlunsplit
from workbench_platform.supervisor.processes import require_f_path


def write_json(path, value, durable=False):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())
    os.replace(temporary, path)


def normalize_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('请输入有效的 API URL')
    value = value.strip()
    if any(ord(char) < 33 for char in value) or '\\' in value:
        raise ValueError('API URL 不能含空白或反斜杠')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError('API URL 须为 HTTP/HTTPS 地址，不能包含账号、查询参数或锚点') from None
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/messages', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path or '/v1', '', ''))


class CredentialVault:
    """Current-user Windows DPAPI, platform storage, excluded from exports."""
    def __init__(self, root):
        from packages.runtime import current_install, require_path
        configured = current_install()
        requested = require_path(Path(root), 'Credential namespace', role='read')
        if configured:
            if requested.is_relative_to(configured.data):
                requested = configured.roots['secrets'] / 'vaults' / requested.relative_to(configured.data)
            elif not requested.is_relative_to(configured.roots['secrets']):
                raise ValueError('Credentials must belong to this installation secrets root')
        self.root = require_path(requested, 'Platform controller credentials', role='write')

    @staticmethod
    def _crypt(data, decrypt=False):
        if os.name != 'nt':
            raise ValueError('外部 API 凭据保存需要 Windows DPAPI')
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError('无法读取本机 API Key，请重新填写' if decrypt else '无法加密保存 API Key')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('API 凭据引用无效，请重新配置')
        return self.root / (identifier + '.json')

    def save(self, key):
        encrypted = self._crypt(key.encode('utf-8'))
        self.root.mkdir(parents=True, exist_ok=True)
        identifier = secrets.token_hex(16)
        write_json(self._path(identifier), {'dpapi': base64.b64encode(encrypted).decode()}, durable=True)
        return identifier

    def read(self, identifier):
        try:
            data = json.loads(self._path(identifier).read_text('utf-8'))
            return self._crypt(base64.b64decode(data['dpapi'], validate=True), True).decode('utf-8')
        except (OSError, KeyError, UnicodeError, ValueError):
            raise ValueError('本机 API Key 不可用，请重新填写；拷贝项目不会迁移 Key') from None

    def delete(self, identifier):
        self._path(identifier).unlink(missing_ok=True)

