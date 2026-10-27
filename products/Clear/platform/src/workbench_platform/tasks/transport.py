"""Exact public origins and loopback-only service transport configuration."""
from dataclasses import dataclass
import ipaddress
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PublicEndpoint:
    origin: str
    authority: str
    insecure_http: bool


def is_lan_address(host: str) -> bool:
    """Accept private LAN literals, without trusting DNS or wildcard addresses."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
    return any(address.version == network.version and address in network
               for network in map(ipaddress.ip_network, networks))


def public_endpoint() -> PublicEndpoint:
    value = os.environ.get('CLEAR_PUBLIC_ORIGIN', 'https://localhost:8889')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or any(character.isspace() for character in value)
                or any(character in value for character in ('\\', '*', '%'))
                or port == 0):
            raise ValueError
    except ValueError as error:
        raise RuntimeError('CLEAR_PUBLIC_ORIGIN must be one exact HTTP(S) origin without credentials or a path') from error
    host = parsed.hostname.lower()
    insecure = parsed.scheme == 'http'
    if insecure:
        development = (os.environ.get('CLEAR_ALLOW_HTTP_LOOPBACK') == '1'
                       and host in {'localhost', '127.0.0.1', '::1'})
        lan = os.environ.get('CLEAR_ALLOW_LAN_HTTP') == '1' and is_lan_address(host)
        if not (development or lan):
            raise RuntimeError('Public portal requires HTTPS; LAN HTTP requires explicit CLEAR_ALLOW_LAN_HTTP=1 and a private LAN IP')
    authority = f'[{host}]' if ':' in host else host
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        authority += f':{port}'
    return PublicEndpoint(f'{parsed.scheme}://{authority}', authority, insecure)


def private_worker_url() -> str:
    value = os.environ.get('CLEAR_WORKER_URL', 'http://127.0.0.1:8890')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not port or parsed.path not in {'', '/'}
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or any(character.isspace() for character in value)):
            raise ValueError
    except ValueError as error:
        raise RuntimeError('Worker transport must use an explicitly configured loopback endpoint') from error
    return f'http://127.0.0.1:{port}'


def private_worker_headers() -> dict[str, str]:
    origin = private_worker_url()
    return {'Host': urlsplit(origin).netloc, 'Origin': origin}
